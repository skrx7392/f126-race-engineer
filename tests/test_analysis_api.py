"""Tests for the analysis HTTP surface.

Tier 1 (always runs) needs no database: it checks the guards that must hold before a query
is ever reached — the 503 when there is no DB, the 422s that stop junk at the door, and the
project invariant that these routes are GET-only.

Tier 2 puts the real thing end to end. It spins a throwaway ``postgres:18``, backfills a
real 60-second Bahrain P1 capture through ``f126 backfill``, and drives every endpoint over
``httpx.ASGITransport`` against the populated database — the same path uvicorn takes, minus
the socket. It skips cleanly when Docker or the capture is unavailable::

    uv run pytest tests/test_analysis_api.py            # spins its own postgres:18
    F126_TEST_DATABASE_URL=postgresql://... uv run pytest tests/test_analysis_api.py
    F126_TEST_FIXTURE=/path/to/capture.f1raw uv run pytest tests/test_analysis_api.py

The capture covers part of one flying lap, so the lap-count assertions are about
plausibility rather than Bahrain's full 15-turn layout; see `test_corners_on_real_bahrain`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import psycopg
import pytest

from f126.config import Config
from f126.store import db
from f126.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The capture tier 2 analyses. Override with F126_TEST_FIXTURE; without one the tier
#: skips, because a synthetic capture would prove nothing this file is here to prove.
FIXTURE = Path(
    os.environ.get(
        "F126_TEST_FIXTURE",
        "/private/tmp/claude-501/-Users-krishna-workplace-f126-race-engineer"
        "/ea087a5e-85e3-452a-a609-8ee88912b8e8/scratchpad/f1raw/raw"
        "/20260806T223210Z_fixture_0.f1raw",
    )
)

#: The one flying lap the capture holds telemetry for, and a lap number free to derive from
#: it. See `_derive_slower_lap`.
REAL_LAP = 6
SLOWER_LAP = 7
SLOWER_BY = 1.02
PLAYER_CAR = 21

#: A second session at the *same* circuit, derived the same way, so `ref=track_best` and an
#: explicit `ref_session` have somewhere to point. See `_derive_sibling_sessions`.
SIBLING_LAP = 1
SIBLING_FASTER_BY = 0.95
SIBLING_LAP_MS = 85_000
#: ...and a third at a different circuit, for the cross-track 422.
OTHER_TRACK_ID = 7

ANALYSIS_PATHS = (
    "/api/sessions/1/laps/5/telemetry",
    "/api/analysis/compare?session_a=1&lap_a=1&session_b=1&lap_b=2",
    "/api/analysis/corners?session_id=1&lap=5",
    "/api/analysis/stints?session_id=1",
)


# --------------------------------------------------------------------------------------------
# Fakes and helpers
# --------------------------------------------------------------------------------------------


class FakeLive:
    """The narrowest `LiveSource` that lets an app be built without a game attached."""

    def get_snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot"}

    def get_fast(self) -> dict[str, Any] | None:
        return None

    def get_slow(self) -> dict[str, Any] | None:
        return None


def make_cfg(**overrides: Any) -> Config:
    fields: dict[str, Any] = {"static_dir": "/nonexistent/f126-static", "database_url": ""}
    fields.update(overrides)
    return Config(**fields)


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def parallel(payload: dict[str, Any], keys: tuple[str, ...], length: int) -> None:
    for key in keys:
        assert len(payload[key]) == length, f"{key} is not parallel with the rest"


# --------------------------------------------------------------------------------------------
# Tier 1: guards that hold with no database at all
# --------------------------------------------------------------------------------------------


async def test_every_analysis_endpoint_degrades_to_503_without_a_database() -> None:
    app = create_app(make_cfg(), FakeLive())
    async with client_for(app) as client:
        for path in ANALYSIS_PATHS:
            response = await client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "database unavailable"}


async def test_a_broken_connection_is_503_and_never_leaks_internals() -> None:
    """A 500 with a traceback in it would be a gift to anyone scanning this host."""
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        for path in ANALYSIS_PATHS:
            response = await client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "database unavailable"}


async def test_path_parameters_are_validated_before_a_query_can_run() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        for bad in ("x" * 40, "1; DROP TABLE laps", "-1", "abc", "9" * 30):
            response = await client.get(f"/api/sessions/{bad}/laps/5/telemetry")
            assert response.status_code == 422, bad
        for bad_lap in ("-1", "abc", "999999"):
            response = await client.get(f"/api/sessions/1/laps/{bad_lap}/telemetry")
            assert response.status_code == 422, bad_lap


async def test_query_parameters_are_bounded() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        cases = (
            "/api/sessions/1/laps/5/telemetry?car_index=99",
            "/api/sessions/1/laps/5/telemetry?car_index=-1",
            "/api/analysis/compare?session_a=1&lap_a=1&session_b=1",  # lap_b missing
            "/api/analysis/compare?session_a=0&lap_a=1&session_b=1&lap_b=2",
            "/api/analysis/compare?session_a=x&lap_a=1&session_b=1&lap_b=2",
            "/api/analysis/corners?session_id=1&lap=1&ref=fastest",
            "/api/analysis/corners?session_id=1",  # lap missing
            "/api/analysis/stints",  # session_id missing
            "/api/analysis/stints?session_id=1&car_index=22",
        )
        for path in cases:
            assert (await client.get(path)).status_code == 422, path


def all_routes(app: Any) -> list[Any]:
    """Every leaf route, following the wrappers `include_router` leaves in `app.routes`.

    FastAPI does not flatten an included router into the app's route list any more — it
    parks an opaque wrapper there and keeps the real routes on `original_router`. Walking
    only the top level would make the read-only assertion below vacuously true, which is
    the one way this test could fail to do its job.
    """
    found: list[Any] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
            continue
        found.append(route)
        mounted = getattr(route, "routes", None)
        if isinstance(mounted, list):
            stack.extend(mounted)
    return found


def test_the_analysis_routes_are_read_only() -> None:
    """Same invariant `tests/test_web.py` enforces, restated for the routes added here."""
    app = create_app(make_cfg(), FakeLive())
    analysis = [
        route
        for route in all_routes(app)
        if getattr(route, "path", "").startswith(("/api/analysis", "/api/sessions"))
    ]
    assert analysis, "the analysis routes were never registered"
    for route in analysis:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}, route.path


def test_the_router_is_wired_into_the_app() -> None:
    app = create_app(make_cfg(), FakeLive())
    paths = {getattr(route, "path", "") for route in all_routes(app)}
    assert "/api/analysis/compare" in paths
    assert "/api/analysis/corners" in paths
    assert "/api/analysis/stints" in paths
    assert "/api/sessions/{session_id}/laps/{lap_number}/telemetry" in paths
    assert "/api/sessions" in paths, "the Phase 1 routes must survive the Phase 2 include"


# --------------------------------------------------------------------------------------------
# Tier 2: a real capture, a real Postgres
# --------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _url_with_schema(url: str, schema: str) -> str:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params["options"] = f"-csearch_path={schema}"
    return urlunsplit(parts._replace(query=urlencode(params)))


def _await_postgres(url: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            psycopg.connect(url, connect_timeout=3).close()
            return
        except psycopg.OperationalError as exc:  # container still booting
            last = exc
            time.sleep(0.5)
    raise TimeoutError(f"postgres never came up: {last}")


@contextlib.contextmanager
def _postgres() -> Iterator[str]:
    """A database to throw away: an existing server's scratch schema, or a container."""
    existing = os.environ.get("F126_TEST_DATABASE_URL", "")
    if existing:
        schema = f"f126_a_{uuid.uuid4().hex[:12]}"
        admin = psycopg.connect(existing, autocommit=True)
        try:
            admin.execute(f'CREATE SCHEMA "{schema}"')
            yield _url_with_schema(existing, schema)
        finally:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.close()
        return

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("needs docker, or F126_TEST_DATABASE_URL pointing at a Postgres")
    name = f"f126-analysis-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    started = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=f126",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:18",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start postgres:18 ({started.stderr.strip()[:160]})")
    try:
        url = f"postgresql://postgres:f126@127.0.0.1:{port}/postgres"
        _await_postgres(url)
        yield url
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)


def _derive_slower_lap(url: str, session_id: int) -> None:
    """Clone the captured lap as a lap driven `SLOWER_BY` slower, everywhere, by exactly that.

    The capture holds telemetry for one lap, and one lap cannot be compared with another.
    Rather than fake a lap out of nothing, this replays the *real* lap's distances, pedals
    and gears against a stretched clock — so the compare and corners endpoints run against
    genuine Bahrain geometry with an answer that is known to the millisecond.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO telemetry_samples
                (session_id, segment, generation, session_time_s, frame_id, overall_frame_id,
                 lap_number, lap_distance_m, speed_kmh, throttle, brake, steer, gear, rpm,
                 drs_or_aero)
            SELECT t.session_id, t.segment, t.generation,
                   o.t0 + (t.session_time_s - o.t0) * %(stretch)s,
                   t.frame_id, t.overall_frame_id, %(new_lap)s, t.lap_distance_m,
                   t.speed_kmh / %(stretch)s, t.throttle, t.brake, t.steer, t.gear, t.rpm,
                   t.drs_or_aero
              FROM telemetry_samples t,
                   (SELECT min(session_time_s) AS t0 FROM telemetry_samples
                     WHERE session_id = %(sid)s AND lap_number = %(lap)s) o
             WHERE t.session_id = %(sid)s AND t.lap_number = %(lap)s
            """,
            {"sid": session_id, "lap": REAL_LAP, "new_lap": SLOWER_LAP, "stretch": SLOWER_BY},
        )
        conn.execute(
            "INSERT INTO laps (session_id, car_index, lap_number, generation, lap_time_ms,"
            " s1_ms, s2_ms, s3_ms, valid) VALUES (%s, %s, %s, 0, 94000, 29000, 41000, 24000, true)",
            (session_id, PLAYER_CAR, SLOWER_LAP),
        )


def _clone_session(
    conn: Any,
    source_session_id: int,
    *,
    session_uid: str,
    session_type: int,
    session_type_name: str,
    track_id: int | None,
    track_name: str | None,
    started_at_wall: float,
    lap_number: int,
    lap_time_ms: int,
    stretch: float,
) -> int:
    """Insert a new session carrying one lap cloned off `source_session_id`'s real lap.

    `sessions.id` is an identity column, so the row is inserted and the id read back. The
    telemetry is the same genuine Bahrain geometry on a scaled clock, which is what makes a
    cross-session reference meaningful rather than a comparison against noise.
    """
    row = conn.execute(
        """
        INSERT INTO sessions
            (session_uid, segment, packet_format, session_type, session_type_name,
             track_id, track_name, started_at_wall, player_car_index, total_laps)
        VALUES (%s, 0, 2026, %s, %s, %s, %s, %s, %s, 1)
        RETURNING id
        """,
        (
            session_uid,
            session_type,
            session_type_name,
            track_id,
            track_name,
            started_at_wall,
            PLAYER_CAR,
        ),
    ).fetchone()
    assert row is not None
    new_id = int(row[0])

    conn.execute(
        """
        INSERT INTO telemetry_samples
            (session_id, segment, generation, session_time_s, frame_id, overall_frame_id,
             lap_number, lap_distance_m, speed_kmh, throttle, brake, steer, gear, rpm,
             drs_or_aero)
        SELECT %(new_sid)s, t.segment, t.generation,
               o.t0 + (t.session_time_s - o.t0) * %(stretch)s,
               t.frame_id, t.overall_frame_id, %(new_lap)s, t.lap_distance_m,
               t.speed_kmh / %(stretch)s, t.throttle, t.brake, t.steer, t.gear, t.rpm,
               t.drs_or_aero
          FROM telemetry_samples t,
               (SELECT min(session_time_s) AS t0 FROM telemetry_samples
                 WHERE session_id = %(sid)s AND lap_number = %(lap)s) o
         WHERE t.session_id = %(sid)s AND t.lap_number = %(lap)s
        """,
        {
            "new_sid": new_id,
            "sid": source_session_id,
            "lap": REAL_LAP,
            "new_lap": lap_number,
            "stretch": stretch,
        },
    )
    conn.execute(
        "INSERT INTO laps (session_id, car_index, lap_number, generation, lap_time_ms,"
        " s1_ms, s2_ms, s3_ms, valid) VALUES (%s, %s, %s, 0, %s, 27000, 37000, 21000, true)",
        (new_id, PLAYER_CAR, lap_number, lap_time_ms),
    )
    return new_id


def _derive_sibling_sessions(url: str, session_id: int) -> tuple[int, int]:
    """A same-track quali session and a different-track one. Returns `(same, other)`."""
    with psycopg.connect(url, autocommit=True) as conn:
        track = conn.execute(
            "SELECT track_id, track_name FROM sessions WHERE id = %s", (session_id,)
        ).fetchone()
        assert track is not None
        track_id, track_name = track
        same = _clone_session(
            conn,
            session_id,
            session_uid="90000000000000000001",
            session_type=9,
            session_type_name="One-Shot Qualifying",
            track_id=track_id,
            track_name=track_name,
            started_at_wall=1_786_000_000.0,
            lap_number=SIBLING_LAP,
            lap_time_ms=SIBLING_LAP_MS,
            stretch=SIBLING_FASTER_BY,
        )
        other = _clone_session(
            conn,
            session_id,
            session_uid="90000000000000000002",
            session_type=9,
            session_type_name="One-Shot Qualifying",
            track_id=OTHER_TRACK_ID,
            track_name="Elsewhere",
            started_at_wall=1_786_000_100.0,
            lap_number=SIBLING_LAP,
            lap_time_ms=SIBLING_LAP_MS,
            stretch=SIBLING_FASTER_BY,
        )
    return same, other


@pytest.fixture(scope="module")
def analysis_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A Postgres holding the backfilled capture, dropped (or torn down) afterwards."""
    if not FIXTURE.is_file():
        pytest.skip(f"no capture at {FIXTURE}; set F126_TEST_FIXTURE")
    if shutil.which("uv") is None:
        pytest.skip("needs uv on PATH to run `uv run f126 backfill`")

    with _postgres() as url:
        db.init_db(url)
        backfill = subprocess.run(
            ["uv", "run", "f126", "backfill", str(FIXTURE)],
            env=os.environ
            | {
                "F126_DATABASE_URL": url,
                "F126_DATA_DIR": str(tmp_path_factory.mktemp("f126-data")),
            },
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        assert backfill.returncode == 0, f"backfill failed:\n{backfill.stderr[-4000:]}"
        _derive_slower_lap(url, _session_id(url))
        yield url


def _session_id(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute("SELECT min(id) FROM sessions").fetchone()
    assert row is not None and row[0] is not None, "backfill wrote no session"
    return int(row[0])


@pytest.fixture(scope="module")
def real_session(analysis_db: str) -> int:
    return _session_id(analysis_db)


@pytest.fixture(scope="module")
def sibling_sessions(analysis_db: str, real_session: int) -> tuple[int, int]:
    """`(same_track_session_id, other_track_session_id)`, created once for the module."""
    return _derive_sibling_sessions(analysis_db, real_session)


@pytest.fixture
async def api(analysis_db: str) -> AsyncIterator[httpx.AsyncClient]:
    """The app in front of the populated database, driven the way uvicorn drives it."""
    app = create_app(make_cfg(), FakeLive(), {}, lambda: db.connect(analysis_db))
    async with client_for(app) as client:
        yield client


async def _get(api: httpx.AsyncClient, path: str) -> Any:
    response = await api.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:400]}"
    assert "NaN" not in response.text, "NaN is not valid JSON and will break JSON.parse"
    return response.json()


async def test_telemetry_arrays_are_parallel_and_ordered(api: Any, real_session: int) -> None:
    payload = await _get(api, f"/api/sessions/{real_session}/laps/{REAL_LAP}/telemetry")

    points = payload["points"]
    assert points > 500, "60 s at 20 Hz should be about 1200 samples"
    parallel(
        payload,
        (
            "distance_m",
            "speed_kmh",
            "throttle",
            "brake",
            "steer",
            "gear",
            "rpm",
            "drs_or_aero",
            "session_time_s",
        ),
        points,
    )
    assert payload["session_id"] == real_session
    assert payload["lap_number"] == REAL_LAP
    assert payload["car_index"] == PLAYER_CAR

    distance = payload["distance_m"]
    time_s = payload["session_time_s"]
    assert distance == sorted(distance)
    assert all(b > a for a, b in zip(distance, distance[1:], strict=False))
    assert all(b > a for a, b in zip(time_s, time_s[1:], strict=False))

    speed = payload["speed_kmh"]
    assert min(speed) >= 0.0 and max(speed) < 400.0
    assert max(speed) > 250.0, "a flying lap at Bahrain has a straight in it"
    assert set(payload["gear"]) <= set(range(-1, 9))
    assert all(0.0 <= v <= 1.0 for v in payload["throttle"])
    assert all(0.0 <= v <= 1.0 for v in payload["brake"])


async def test_telemetry_404s_for_things_that_are_not_there(api: Any, real_session: int) -> None:
    for path, why in (
        (f"/api/sessions/{real_session}/laps/99/telemetry", "unrecorded lap"),
        (f"/api/sessions/{real_session + 4242}/laps/6/telemetry", "unknown session"),
        (f"/api/sessions/{real_session}/laps/{REAL_LAP}/telemetry?car_index=0", "another car"),
    ):
        assert (await api.get(path)).status_code == 404, why


async def test_compare_of_two_real_laps_returns_a_finite_delta_curve(
    api: Any, real_session: int
) -> None:
    payload = await _get(
        api,
        f"/api/analysis/compare?session_a={real_session}&lap_a={SLOWER_LAP}"
        f"&session_b={real_session}&lap_b={REAL_LAP}",
    )

    points = payload["points"]
    assert points > 500
    parallel(payload, ("grid_m", "delta_ms"), points)
    for side in ("a", "b"):
        parallel(payload[side], ("speed_kmh", "throttle", "brake", "gear"), points)

    assert payload["track_id"] == 3
    assert payload["track_name"] == "Bahrain"
    assert payload["a"]["lap_time_ms"] == 94_000  # the derived lap's row, via queries.lap_row
    assert payload["sectors_a"] == [29_000, 41_000, 24_000]
    assert payload["a"]["car_index"] == PLAYER_CAR

    grid = payload["grid_m"]
    assert all(b > a for a, b in zip(grid, grid[1:], strict=False))
    assert grid[1] - grid[0] == pytest.approx(5.0)

    delta = payload["delta_ms"]
    assert all(value is not None for value in delta), "both laps cover the whole grid"
    assert delta[0] == 0.0
    # `a` is the same lap driven 2% slower, so it falls behind monotonically, and by the end
    # of the window it has lost exactly 2% of the elapsed time.
    assert all(b >= a - 1e-6 for a, b in zip(delta, delta[1:], strict=False))
    assert delta[-1] > 1000.0
    assert delta[-1] == pytest.approx((SLOWER_BY - 1.0) * 59_900.0, rel=0.05)


async def test_compare_of_a_lap_with_itself_is_flat(api: Any, real_session: int) -> None:
    payload = await _get(
        api,
        f"/api/analysis/compare?session_a={real_session}&lap_a={REAL_LAP}"
        f"&session_b={real_session}&lap_b={REAL_LAP}",
    )
    assert set(payload["delta_ms"]) == {0.0}
    assert payload["a"] == payload["b"]


async def test_compare_404s_on_a_lap_with_no_trace(api: Any, real_session: int) -> None:
    response = await api.get(
        f"/api/analysis/compare?session_a={real_session}&lap_a={REAL_LAP}"
        f"&session_b={real_session}&lap_b=3"
    )
    assert response.status_code == 404


async def test_corners_on_real_bahrain(api: Any, real_session: int) -> None:
    """Segmentation of a real flying lap.

    The capture is 60 s long and covers roughly the middle 3.4 km of Bahrain's 5.4 km, so
    the whole 15-turn layout is not on offer; what is asserted is that the corners found in
    the covered window are real ones, correctly ordered, and physically sensible. On the
    reference capture this finds 7, which is right for that stretch: turns 4, 5, 6/7, 8,
    9/10, 11 and 12/13, with the linked complexes merged into one row each by
    `MIN_CORNER_SPACING_M` because they share a braking event.
    """
    payload = await _get(
        api, f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref={REAL_LAP}"
    )
    found = payload["corners"]

    assert payload["session_id"] == real_session
    assert payload["lap_number"] == REAL_LAP
    assert payload["ref_lap_number"] == REAL_LAP
    assert 4 <= len(found) <= 15, f"implausible corner count: {len(found)}"
    assert [corner["n"] for corner in found] == list(range(1, len(found) + 1))

    for corner in found:
        assert corner["entry_m"] < corner["apex_m"] < corner["exit_m"]
        assert corner["entry_m"] > 700.0 and corner["exit_m"] < 4200.0  # inside the capture
        assert 30.0 < corner["min_speed_kmh"] < 320.0
        assert corner["kind"] in {"slow", "medium", "fast"}
        if corner["brake_point_m"] is not None:
            assert corner["brake_point_m"] < corner["apex_m"]

    for earlier, later in zip(found, found[1:], strict=False):
        assert earlier["exit_m"] < later["entry_m"], "corner windows must not overlap"

    slow = [corner for corner in found if corner["kind"] == "slow"]
    assert len(slow) >= 2, "Bahrain's middle sector has more than one slow corner"
    assert min(corner["min_speed_kmh"] for corner in found) < 110.0

    # Against itself, a lap loses no time anywhere.
    assert payload["total_delta_ms"] == 0
    assert payload["straights_time_loss_ms"] == 0
    assert all(corner["time_loss_ms"] == 0 for corner in found)


async def test_corners_against_the_session_best_attribute_the_loss(
    api: Any, real_session: int
) -> None:
    """`ref=best` resolves to the fastest lap that actually has a trace to compare against."""
    payload = await _get(
        api, f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref=best"
    )
    assert payload["ref_lap_number"] == SLOWER_LAP  # the only lap with both a row and a trace

    found = payload["corners"]
    assert found
    # The subject is the real lap; the reference is that lap 2% slower, so the subject is
    # ahead everywhere: every corner is a gain and the total is negative.
    assert payload["total_delta_ms"] < 0
    assert all(corner["time_loss_ms"] <= 0 for corner in found)
    for corner in found:
        assert corner["ref_min_speed_kmh"] < corner["min_speed_kmh"]

    accounted = sum(corner["time_loss_ms"] for corner in found)
    assert payload["straights_time_loss_ms"] == pytest.approx(
        payload["total_delta_ms"] - accounted, abs=2
    )


async def test_corners_are_deterministic_across_requests(api: Any, real_session: int) -> None:
    path = f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref=best"
    assert await _get(api, path) == await _get(api, path)


async def test_corners_404_on_a_lap_with_no_trace(api: Any, real_session: int) -> None:
    response = await api.get(f"/api/analysis/corners?session_id={real_session}&lap=2")
    assert response.status_code == 404


async def test_stints_returns_the_sessions_recorded_stint(api: Any, real_session: int) -> None:
    payload = await _get(api, f"/api/analysis/stints?session_id={real_session}")
    assert payload["session_id"] == real_session
    assert len(payload["stints"]) == 1

    stint = payload["stints"][0]
    assert stint["car_index"] == PLAYER_CAR
    assert stint["stint_no"] == 1
    assert stint["compound_visual"] == 16
    assert stint["derived"] is False
    assert (stint["lap_start"], stint["lap_end"]) == (1, 6)

    assert [lap["lap_number"] for lap in stint["laps"]] == [1, 2, 3, 4, 5]
    assert all(lap["valid"] for lap in stint["laps"])
    assert all(90_000 < lap["lap_time_ms"] < 95_000 for lap in stint["laps"])
    # A single stint has no pit stop either side of it, so nothing is an in- or out-lap, and
    # a five-lap P1 run at Bahrain has no 107% outlier in it.
    assert all(lap["excluded"] is False for lap in stint["laps"])

    fit = stint["fit"]
    assert fit is not None and fit["n_used"] == 5
    assert 88_000 < fit["base_ms"] < 96_000
    assert abs(fit["deg_ms_per_lap"]) < 2_000  # five practice laps: no real degradation signal
    assert -1.0 <= fit["r2"] <= 1.0

    wear = stint["wear_end_pct"]
    assert wear is not None and len(wear) == 4
    assert all(0.0 <= value < 100.0 for value in wear)
    assert max(wear) > 0.0, "the tyres did wear during the run"


async def test_stints_fall_back_to_the_lap_table_for_a_car_with_no_stint_rows(
    api: Any, real_session: int
) -> None:
    """Only the player's stints are recorded, so any AI car exercises the reconstruction."""
    payload = await _get(api, f"/api/analysis/stints?session_id={real_session}&car_index=0")
    assert len(payload["stints"]) == 1

    stint = payload["stints"][0]
    assert stint["derived"] is True
    assert stint["car_index"] == 0
    assert stint["stint_no"] == 1
    assert stint["wear_end_pct"] is None
    assert stint["laps"], "the AI car did record laps"
    assert stint["lap_start"] == stint["laps"][0]["lap_number"]
    assert stint["lap_end"] == stint["laps"][-1]["lap_number"]
    if len(stint["laps"]) < 4:
        assert stint["fit"] is None  # too few laps to fit anything meaningful


async def test_stints_404_on_an_unknown_session(api: Any, real_session: int) -> None:
    response = await api.get(f"/api/analysis/stints?session_id={real_session + 4242}")
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# corners: self-reference, and references from another session at the same circuit
# --------------------------------------------------------------------------------------


async def test_corners_ref_best_never_resolves_to_the_subject_lap(
    api: Any, real_session: int
) -> None:
    """Regression: `ref=best` on the session's own best lap self-compared to ±0.000.

    Asking for corners on the fastest lap used to resolve the reference to that same lap
    and emit a full table of zeros that looked like a real, perfectly-matched comparison.
    """
    # SLOWER_LAP is the derived one; REAL_LAP is quicker, so on the subject REAL_LAP the
    # unfiltered "best" would be REAL_LAP itself.
    payload = await _get(
        api, f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref=best"
    )
    assert payload["ref_lap_number"] != REAL_LAP
    assert payload["self_reference"] is False
    assert payload["total_delta_ms"] != 0


async def test_corners_flag_an_explicit_self_reference(api: Any, real_session: int) -> None:
    """An explicit `ref=<subject lap>` is honoured, but labelled for what it is."""
    payload = await _get(
        api, f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref={REAL_LAP}"
    )
    assert payload["ref_lap_number"] == REAL_LAP
    assert payload["self_reference"] is True


async def test_corners_track_best_reaches_across_sessions(
    api: Any, real_session: int, sibling_sessions: tuple[int, int]
) -> None:
    """`ref=track_best` finds the quickest drawable lap at the circuit, in any session."""
    same, _other = sibling_sessions
    payload = await _get(
        api,
        f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}&ref=track_best",
    )
    assert payload["ref_session_id"] == same
    assert payload["ref_lap_number"] == SIBLING_LAP
    assert payload["self_reference"] is False
    assert payload["ref_session_label"] is not None
    assert "One-Shot Qualifying" in payload["ref_session_label"]
    # The sibling lap is the same geometry on a 5% quicker clock, so the subject loses.
    assert payload["total_delta_ms"] > 0
    assert payload["corners"]


async def test_corners_track_best_excludes_the_subject_lap(
    api: Any, sibling_sessions: tuple[int, int]
) -> None:
    """The circuit's fastest lap cannot be its own reference."""
    same, _other = sibling_sessions
    payload = await _get(
        api, f"/api/analysis/corners?session_id={same}&lap={SIBLING_LAP}&ref=track_best"
    )
    assert not (payload["ref_session_id"] == same and payload["ref_lap_number"] == SIBLING_LAP), (
        "the subject lap resolved as its own track best"
    )


async def test_corners_ref_session_reads_the_reference_from_another_session(
    api: Any, real_session: int, sibling_sessions: tuple[int, int]
) -> None:
    same, _other = sibling_sessions
    payload = await _get(
        api,
        f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}"
        f"&ref={SIBLING_LAP}&ref_session={same}",
    )
    assert payload["ref_session_id"] == same
    assert payload["ref_lap_number"] == SIBLING_LAP
    assert payload["self_reference"] is False


async def test_corners_ref_session_best_reads_the_other_sessions_best(
    api: Any, real_session: int, sibling_sessions: tuple[int, int]
) -> None:
    """`ref=best&ref_session=` means *that* session's best, not the subject's."""
    same, _other = sibling_sessions
    payload = await _get(
        api,
        f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}"
        f"&ref=best&ref_session={same}",
    )
    assert payload["ref_session_id"] == same
    assert payload["ref_lap_number"] == SIBLING_LAP


async def test_corners_refuse_a_reference_from_another_circuit(
    api: Any, real_session: int, sibling_sessions: tuple[int, int]
) -> None:
    """Mirrors compare's guard: same geometry or nothing."""
    _same, other = sibling_sessions
    response = await api.get(
        f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}"
        f"&ref={SIBLING_LAP}&ref_session={other}"
    )
    assert response.status_code == 422
    assert "different tracks" in response.text


async def test_corner_numbering_is_stable_across_reference_sessions(
    api: Any, real_session: int, sibling_sessions: tuple[int, int]
) -> None:
    """Corners are segmented on the SUBJECT lap, so the reference cannot renumber them.

    The guard against a tempting wrong implementation: segmenting both laps and pairing the
    results would make corner `n` depend on which reference was chosen, and a partial
    reference lap would silently shift every number after the gap.
    """
    same, _other = sibling_sessions
    base = f"/api/analysis/corners?session_id={real_session}&lap={REAL_LAP}"
    no_ref = await _get(api, f"{base}&ref={REAL_LAP}")
    same_session = await _get(api, f"{base}&ref=best")
    cross_session = await _get(api, f"{base}&ref=track_best")

    geometry = [
        [(c["n"], c["entry_m"], c["apex_m"], c["exit_m"]) for c in payload["corners"]]
        for payload in (no_ref, same_session, cross_session)
    ]
    assert geometry[0] == geometry[1] == geometry[2], (
        "corner identity must not depend on where the reference lap came from"
    )
    # And the subject-only columns are untouched by the reference too.
    for payload in (same_session, cross_session):
        assert [c["min_speed_kmh"] for c in payload["corners"]] == [
            c["min_speed_kmh"] for c in no_ref["corners"]
        ]


async def test_telemetry_distance_axis_is_strictly_increasing(api: Any, real_session: int) -> None:
    """The emitted distance array must be a genuine function of distance.

    It is rounded to centimetres on the way out, and slow running (a standing start, a
    pit-lane crawl) puts two 20 Hz samples inside the same centimetre. Charts key and
    bisect on this axis, so duplicates are not cosmetic.
    """
    payload = await _get(api, f"/api/sessions/{real_session}/laps/{REAL_LAP}/telemetry")
    distance = payload["distance_m"]
    assert distance == sorted(distance)
    assert len(set(distance)) == len(distance), "duplicate distance samples reached the wire"
    assert payload["points"] == len(distance)
    for name, values in payload.items():
        if isinstance(values, list):
            assert len(values) == len(distance), f"channel {name} is not index-aligned"

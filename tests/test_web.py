"""Tests for the web layer: hub semantics, ops endpoints, read-only API, SPA.

Two styles, on purpose:

* HTTP is exercised through `httpx.ASGITransport` — no socket, no lifespan, so
  the routes are tested exactly as the ASGI server will call them.
* WebSocket behaviour that depends on *back-pressure* cannot go through a test
  client: `TestClient` drains into an unbounded in-memory stream, so a client
  queue would never fill and the drop policy would never fire. Those tests drive
  `WsHub.handle()` with a stub socket whose `send_text` is gated, which is the
  only way to hold a client still while frames pile up behind it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from f126.config import Config
from f126.web import metrics
from f126.web.app import _QUERY_NAMES, create_app
from f126.web.broadcast import WsHub

# ---- fixtures and fakes -------------------------------------------------

# Payloads copied from docs/ws-protocol.md so the tests fail if the contract
# drifts out from under the frames the hub is asked to carry.

FAST_FRAME: dict[str, Any] = {
    "type": "fast",
    "t": 123.456,
    "speed_kmh": 287.0,
    "gear": 7,
    "rpm": 11450,
    "throttle": 1.0,
    "brake": 0.0,
    "steer": -0.12,
    "drs_open": False,
    "aero_mode": 1,
    "ers_deploy_mode": 2,
    "rev_lights_percent": 60,
    "lap_number": 12,
    "lap_distance_m": 3120.5,
    "current_lap_ms": 61234,
    "delta_best_ms": -142,
    "delta_kind": "session_best",
}

SESSION_BLOCK: dict[str, Any] = {
    "session_uid": "1234567890",
    "segment": 0,
    "packet_format": 2026,
    "session_type": 10,
    "session_kind": "race",
    "track_id": 3,
    "track_name": "Suzuka",
    "time_left_s": 1740,
    "duration_s": 3600,
    "total_laps": 53,
    "safety_car": 0,
    "fia_flag": 0,
    "weather": 1,
    "track_temp_c": 31,
    "air_temp_c": 24,
    "forecast": [{"offset_min": 5, "weather": 2, "rain_pct": 30}],
    "stalled": False,
    "joined_in_progress": False,
}

SLOW_FRAME: dict[str, Any] = {
    "type": "slow",
    "session": SESSION_BLOCK,
    "tower": [
        {
            "car_index": 4,
            "position": 1,
            "name": "VERSTAPPEN",
            "team_id": 2,
            "is_player": False,
            "lap_number": 13,
            "last_lap_ms": 92345,
            "gap_ahead_ms": None,
            "gap_leader_ms": 0,
            "compound_visual": 16,
            "tyre_age_laps": 8,
            "pit_status": 0,
            "penalties_s": 0,
            "result_status": 2,
        }
    ],
    "tyres": {
        "surface_temp_c": [98, 97, 92, 94],
        "inner_temp_c": [102, 101, 96, 97],
        "pressure_psi": [21.5, 21.6, 23.0, 23.1],
        "wear_pct": [12.5, 13.1, 8.2, 8.9],
        "wear_rate_pct_per_lap": [0.9, 1.0, 0.6, 0.7],
        "projected_wear_end_pct": [31.0, 33.5, 20.1, 22.0],
        "compound_actual": 18,
        "compound_visual": 16,
        "age_laps": 8,
    },
    "fuel": {
        "in_tank_kg": 43.2,
        "remaining_laps": 22.4,
        "laps_left_in_session": 21,
        "delta_laps": 1.4,
        "burn_last_lap_kg": 1.9,
    },
    "energy": {
        "store_j": 2800000.0,
        "store_pct": 70.0,
        "deploy_mode": 2,
        "harvested_lap_j": 120000.0,
        "deployed_lap_j": 350000.0,
    },
    "damage": {
        "front_left_wing_pct": 0,
        "front_right_wing_pct": 5,
        "rear_wing_pct": 0,
        "floor_pct": 0,
        "diffuser_pct": 0,
        "sidepod_pct": 0,
        "gearbox_pct": 3,
        "engine_pct": 7,
    },
    "pace": {"last_3_avg_ms": 92800, "ahead_last_3_avg_ms": 92500, "behind_last_3_avg_ms": 93400},
    "sectors": {
        "current_lap": [21345, None, None],
        "best_lap": [21001, 35400, 35900],
        "session_best": [20950, 35310, 35800],
        "last_lap_valid": True,
    },
    "timetrial": None,
    "health": {
        "packets_per_sec": 480,
        "parse_errors_total": 0,
        "kernel_drops_total": 0,
        "last_packet_age_ms": 40,
        "ws_clients": 2,
    },
}

LAP_EVENT: dict[str, Any] = {
    "type": "event",
    "t": 123.456,
    "code": "LAP",
    "data": {
        "lap_number": 12,
        "time_ms": 92345,
        "valid": True,
        "color": "purple",
        "sectors": [21001, 35400, 35944],
    },
}


class FakeLive:
    """Deterministic `LiveSource`."""

    def __init__(self, *, advance: bool = True) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.advance = advance
        self.fast_calls = 0

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "protocol_version": 1,
            "session": SESSION_BLOCK,
            "fast": FAST_FRAME,
            "slow": SLOW_FRAME,
            "recent_events": [LAP_EVENT],
        }

    def get_fast(self) -> dict[str, Any] | None:
        self.fast_calls += 1
        frame = dict(FAST_FRAME)
        if self.advance:
            frame["t"] = round(FAST_FRAME["t"] + self.fast_calls * 0.1, 3)
        return frame

    def get_slow(self) -> dict[str, Any] | None:
        return dict(SLOW_FRAME)


class FakeStats:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def stats(self) -> dict[str, Any]:
        return self.values


class BrokenStats:
    def stats(self) -> dict[str, Any]:
        raise RuntimeError("counter backend exploded")


class StubWs:
    """A WebSocket-shaped object whose sends can be held open.

    `gated=True` blocks inside `send_text` until `release()`, which is what
    makes a stalled reader reproducible.
    """

    def __init__(self, *, gated: bool = False) -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._gate = asyncio.Event()
        self._disconnected = asyncio.Event()
        if not gated:
            self._gate.set()

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        await self._gate.wait()
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_code is None:
            self.close_code = code
        self._disconnected.set()

    async def receive(self) -> dict[str, Any]:
        await self._disconnected.wait()
        return {"type": "websocket.disconnect", "code": self.close_code or 1000}

    def release(self) -> None:
        self._gate.set()

    def hang_up(self) -> None:
        self._disconnected.set()

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(text) for text in self.sent]


def make_cfg(tmp_path: Path | None = None, **overrides: Any) -> Config:
    cfg = Config()
    # Never let the developer's real build dir leak into a test.
    cfg.static_dir = str((tmp_path or Path("/nonexistent")) / "no-such-frontend")
    cfg.ws_fast_hz = 50.0
    cfg.ws_slow_hz = 0.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def settle(seconds: float = 0.02) -> None:
    """Let freshly created tasks reach their first await."""
    await asyncio.sleep(seconds)


async def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached within timeout")


# ---- ops endpoints ------------------------------------------------------


async def test_healthz_is_ok_without_telemetry_or_db() -> None:
    app = create_app(make_cfg(), FakeLive())
    async with client_for(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_security_headers_present() -> None:
    app = create_app(make_cfg(), FakeLive())
    async with client_for(app) as client:
        response = await client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self' ws: wss:" in csp
    assert "frame-ancestors 'none'" in csp


async def test_metrics_exposition_covers_every_source() -> None:
    sources = {
        "parser": FakeStats({"packets_total": 12345, "errors_total": 0, "format": "2026"}),
        "writer": FakeStats({"rows_total": 900, "batch_ms": 250.5}),
        "rawlog": FakeStats({"bytes_total": 1 << 20, "compressing": True}),
        "listener": FakeStats({"kernel_drops_total": 3}),
    }
    app = create_app(make_cfg(), FakeLive(), sources)
    async with client_for(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = response.text
    assert body.endswith("\n")
    assert "f126_up 1" in body
    assert "f126_parser_packets_total 12345" in body
    assert "# TYPE f126_parser_packets_total counter" in body
    assert "f126_writer_batch_ms 250.5" in body
    assert "# TYPE f126_writer_batch_ms gauge" in body
    assert "f126_rawlog_compressing 1" in body
    assert "f126_listener_kernel_drops_total 3" in body
    # Hub counters are always there, under the reserved "ws" name.
    assert "f126_ws_clients 0" in body
    assert "f126_ws_dropped_frames_total 0" in body
    # Strings are skipped, not rendered as a bare word (which would be invalid).
    assert "format" not in body


async def test_metrics_sources_are_all_optional() -> None:
    for sources in ({}, None, {"writer": None}):
        app = create_app(make_cfg(), FakeLive(), sources)
        async with client_for(app) as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert "f126_up 1" in response.text
        assert "f126_ws_clients 0" in response.text


async def test_metrics_survives_a_broken_source() -> None:
    sources = {"parser": BrokenStats(), "writer": FakeStats({"rows_total": 7})}
    app = create_app(make_cfg(), FakeLive(), sources)
    async with client_for(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "f126_writer_rows_total 7" in response.text
    assert "f126_parser" not in response.text


def test_render_skips_non_numeric_and_sanitises_names() -> None:
    text = metrics.render(
        {
            "odd name": {"a-key": 1, "note": "hello", "nothing": None},
            "broken": "not a mapping",
            "floats": {"nan": float("nan"), "inf": float("inf"), "neg": float("-inf")},
        }
    )
    assert "f126_odd_name_a_key 1" in text
    assert "hello" not in text
    assert "nothing" not in text
    assert "f126_floats_nan NaN" in text
    assert "f126_floats_inf +Inf" in text
    assert "f126_floats_neg -Inf" in text
    assert "broken" not in text
    # Every non-comment line must be "name value".
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition(" ")
            assert name.startswith("f126_") and value


# ---- read-only REST API -------------------------------------------------


async def test_sessions_api_is_503_without_a_database() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, None)
    async with client_for(app) as client:
        for path in ("/api/sessions", "/api/sessions/1234567890", "/api/sessions/12/laps"):
            response = await client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "database unavailable"}


async def test_sessions_api_degrades_when_the_query_layer_cannot_serve() -> None:
    # store.queries is built by another workstream. Before it lands the import
    # fails; after it lands, this bogus "connection" makes the query fail. Both
    # roads lead to the same opaque 503 — never a 500 with internals in it.
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        response = await client.get("/api/sessions")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


async def test_session_id_is_validated_before_it_reaches_a_query() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        for bad in ("x" * 100, "1; DROP TABLE laps", "-1", "abc", "9" * 30):
            response = await client.get(f"/api/sessions/{bad}")
            assert response.status_code == 422, bad
            laps = await client.get(f"/api/sessions/{bad}/laps")
            assert laps.status_code == 422, bad


async def test_car_index_filter_is_bounded() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        assert (await client.get("/api/sessions/1/laps?car_index=99")).status_code == 422
        assert (await client.get("/api/sessions/1/laps?car_index=-1")).status_code == 422


def test_query_helpers_resolve_against_the_real_store() -> None:
    """Integration guard: the names this app looks for must exist in the store.

    `_query` degrades to 503 when a helper is missing, which is the right
    behaviour at runtime and exactly the wrong thing to discover during a race
    weekend — so a rename on the store side has to fail here first.
    """
    queries = pytest.importorskip("f126.store.queries")
    for helper, candidates in _QUERY_NAMES.items():
        resolved = [name for name in candidates if hasattr(queries, name)]
        assert resolved, f"store.queries exposes none of {candidates} for {helper!r}"


async def test_limit_is_bounded() -> None:
    app = create_app(make_cfg(), FakeLive())
    async with client_for(app) as client:
        assert (await client.get("/api/sessions", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/sessions", params={"limit": 10_000})).status_code == 422


def _walk_routes(routes: list[Any]) -> Any:
    """Descend into included routers — FastAPI parks them as opaque entries in
    app.routes, which silently exempted every /api route from this walk."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk_routes(inner.routes)
            continue
        sub = getattr(route, "routes", None)
        if sub:
            yield from _walk_routes(sub)
            continue
        yield route


def test_no_mutating_routes() -> None:
    """The project invariant: this app can only ever be read from."""
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    checked = 0
    for route in _walk_routes(app.routes):
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, f"{route} exposes {methods}"
        if methods:
            checked += 1
    # If the walk ever goes vacuous again (framework change), fail loudly rather
    # than silently passing: the app has at least this many HTTP routes.
    assert checked >= 6, f"route walk only found {checked} routes — walker is broken"


async def test_mutating_requests_are_rejected(tmp_path: Path) -> None:
    """The same invariant from the outside, including through the SPA mount."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>dash</title>")
    app = create_app(make_cfg(tmp_path, static_dir=str(dist)), FakeLive(), {}, lambda: object())

    async with client_for(app) as client:
        for path in ("/", "/healthz", "/metrics", "/api/sessions", "/api/sessions/1", "/anything"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(method, path)
                assert response.status_code in (404, 405), f"{method} {path}"


# ---- static frontend ----------------------------------------------------


async def test_placeholder_page_when_frontend_is_not_built(tmp_path: Path) -> None:
    app = create_app(make_cfg(tmp_path), FakeLive())
    async with client_for(app) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "frontend not built" in response.text
    assert "make frontend" in response.text


async def test_spa_is_served_with_history_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>dash</title>")
    (dist / "app.js").write_text("export const x = 1;")
    app = create_app(make_cfg(tmp_path, static_dir=str(dist)), FakeLive())

    async with client_for(app) as client:
        root = await client.get("/")
        asset = await client.get("/app.js")
        deep_link = await client.get("/session/42/laps")
        api = await client.get("/api/sessions")

    assert root.status_code == 200 and "dash" in root.text
    assert asset.status_code == 200 and "export const x" in asset.text
    # Unknown non-/api path is a client-side route, not a 404.
    assert deep_link.status_code == 200 and "dash" in deep_link.text
    # ...but the API keeps answering for itself.
    assert api.status_code == 503


# ---- websocket: end to end ---------------------------------------------


def test_ws_sends_snapshot_first_then_fast_frames() -> None:
    app = create_app(make_cfg(), FakeLive())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["protocol_version"] == 1
        assert first["slow"]["session"]["track_name"] == "Suzuka"

        second = ws.receive_json()
        assert second["type"] == "fast"
        assert second["speed_kmh"] == 287.0
        third = ws.receive_json()
        assert third["type"] == "fast"
        assert third["t"] != second["t"]


def test_ws_serves_slow_frames_too() -> None:
    app = create_app(make_cfg(ws_fast_hz=0.0, ws_slow_hz=50.0), FakeLive())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        frame = ws.receive_json()
    assert frame["type"] == "slow"
    assert frame["tyres"]["compound_visual"] == 16


def test_ws_enforces_max_clients_with_1013() -> None:
    app = create_app(make_cfg(ws_max_clients=1), FakeLive())
    with TestClient(app) as client, client.websocket_connect("/ws") as first:
        assert first.receive_json()["type"] == "snapshot"
        with client.websocket_connect("/ws") as second, pytest.raises(WebSocketDisconnect) as exc:
            second.receive_json()
        assert exc.value.code == 1013

    hub = app.state.ws_hub
    assert hub.stats()["rejected_total"] == 1


def test_ws_disconnect_cleans_up() -> None:
    app = create_app(make_cfg(), FakeLive())
    with TestClient(app) as client:
        hub = app.state.ws_hub
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert hub.stats()["clients"] == 1
        # The unregister happens on the app's loop, so give it a moment.
        for _ in range(200):
            if hub.stats()["clients"] == 0:
                break
            client.portal.call(asyncio.sleep, 0.01)
        stats = hub.stats()

    assert stats["clients"] == 0
    assert stats["disconnects_total"] == 1
    assert stats["sent_snapshot_total"] == 1


def test_ws_events_reach_clients() -> None:
    live = FakeLive()
    app = create_app(make_cfg(ws_fast_hz=0.0), live)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        client.portal.call(live.events.put, LAP_EVENT)
        frame = ws.receive_json()
    assert frame["type"] == "event"
    assert frame["code"] == "LAP"
    assert frame["data"]["sectors"] == [21001, 35400, 35944]


# ---- websocket: back-pressure policy -----------------------------------


async def test_periodic_frames_drop_oldest_but_events_never_drop() -> None:
    """A stalled reader loses stale telemetry, never a lap."""
    hub = WsHub(make_cfg(), FakeLive(), queue_size=8)
    ws = StubWs(gated=True)
    handler = asyncio.create_task(hub.handle(ws))
    await settle()
    assert hub.client_count == 1

    # The sender is blocked mid-snapshot, so all of this piles up in the queue.
    for tick in range(50):
        hub.publish("fast", {"type": "fast", "t": float(tick)})
    hub.publish("event", LAP_EVENT)

    stats = hub.stats()
    assert stats["dropped_frames_total"] == 43  # 42 overflowing fast + 1 evicted for the event

    ws.release()
    await wait_for(lambda: len(ws.sent) == 9)
    frames = ws.frames()

    assert frames[0]["type"] == "snapshot"
    # What survived is the newest telemetry, not the oldest backlog...
    assert [f["t"] for f in frames[1:-1]] == [43.0, 44.0, 45.0, 46.0, 47.0, 48.0, 49.0]
    # ...and the lap event is still there, last in, last out.
    assert frames[-1]["type"] == "event"
    assert frames[-1]["code"] == "LAP"

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)
    assert hub.client_count == 0
    assert hub.stats()["dropped_frames_total"] == 43  # survives the disconnect


async def test_client_saturated_with_events_is_disconnected() -> None:
    """The pathological case: nothing left to sacrifice, so the client goes."""
    hub = WsHub(make_cfg(), FakeLive(), queue_size=4)
    ws = StubWs(gated=True)
    handler = asyncio.create_task(hub.handle(ws))
    await settle()

    for _ in range(4):  # fills the queue with undroppable frames
        hub.publish("event", LAP_EVENT)
    assert hub.client_count == 1
    hub.publish("event", LAP_EVENT)  # nowhere to put it

    await asyncio.wait_for(handler, timeout=2)
    assert hub.client_count == 0
    assert ws.close_code == 1013
    assert hub.stats()["disconnects_total"] == 1


async def test_fast_loop_skips_unchanged_frames() -> None:
    """No new telemetry means no new frames, however fast the loop ticks."""
    hub = WsHub(make_cfg(ws_fast_hz=100.0), FakeLive(advance=False))
    ws = StubWs()
    handler = asyncio.create_task(hub.handle(ws))
    await settle()
    await hub.start()
    await asyncio.sleep(0.15)  # ~15 ticks
    await hub.aclose()

    frames = ws.frames()
    assert [f["type"] for f in frames] == ["snapshot", "fast"]

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)


async def test_snapshot_survives_a_broken_state_layer() -> None:
    class ExplodingLive(FakeLive):
        def get_snapshot(self) -> dict[str, Any]:
            raise RuntimeError("state machine mid-reset")

    hub = WsHub(make_cfg(), ExplodingLive())
    ws = StubWs()
    handler = asyncio.create_task(hub.handle(ws))
    await wait_for(lambda: bool(ws.sent))

    frame = ws.frames()[0]
    assert frame == {"type": "snapshot", "protocol_version": 1}

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)


async def test_non_finite_floats_do_not_poison_a_frame() -> None:
    """`JSON.parse` rejects bare NaN, so it must never reach the wire."""
    hub = WsHub(make_cfg(), FakeLive())
    ws = StubWs()
    handler = asyncio.create_task(hub.handle(ws))
    await settle()

    hub.publish("fast", {"t": 1.0, "delta_best_ms": float("nan"), "speed_kmh": float("inf")})
    await wait_for(lambda: len(ws.sent) == 2)

    frame = ws.frames()[1]
    assert frame["type"] == "fast"
    assert frame["delta_best_ms"] is None
    assert frame["speed_kmh"] is None

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)


async def test_unserialisable_value_degrades_one_frame_not_the_stream() -> None:
    hub = WsHub(make_cfg(), FakeLive())
    ws = StubWs()
    handler = asyncio.create_task(hub.handle(ws))
    await settle()

    hub.publish("fast", {"t": 1.0, "delta_kind": object()})
    hub.publish("fast", {"t": 2.0, "speed_kmh": 100.0})
    await wait_for(lambda: len(ws.sent) == 3)

    degraded, healthy = ws.frames()[1], ws.frames()[2]
    assert isinstance(degraded["delta_kind"], str)  # stringified, not fatal
    assert healthy["speed_kmh"] == 100.0  # the next frame is unaffected

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)


async def test_broadcast_loop_survives_a_raising_source() -> None:
    """A state layer that trips mid-reset must not silence telemetry for good."""

    class FlakyLive(FakeLive):
        def get_fast(self) -> dict[str, Any] | None:
            self.fast_calls += 1
            if self.fast_calls < 3:
                raise RuntimeError("mid-reset")
            return {"type": "fast", "t": float(self.fast_calls)}

    hub = WsHub(make_cfg(ws_fast_hz=100.0), FlakyLive())
    ws = StubWs()
    handler = asyncio.create_task(hub.handle(ws))
    await settle()
    await hub.start()
    try:
        await wait_for(lambda: len(ws.sent) >= 3)
    finally:
        await hub.aclose()

    kinds = [f["type"] for f in ws.frames()]
    assert kinds[0] == "snapshot"
    assert kinds.count("fast") >= 2  # recovered after the raises

    ws.hang_up()
    await asyncio.wait_for(handler, timeout=2)

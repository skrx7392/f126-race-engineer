"""Phase 2 analysis endpoints: telemetry traces, lap comparison, corners, stints.

`docs/analysis-api.md` is the frozen contract; this module is the only thing that should be
assembling those shapes. It is deliberately thin — fetch rows, hand them to `f126.analysis`,
serialise — because every algorithm worth testing lives in that package, where it can be
tested against a synthetic lap with no database in sight.

Read-only, like the rest of the HTTP surface: four GETs and nothing else, ever
(`tests/test_web.py::test_no_mutating_routes` walks the route table and enforces it).

Error conventions, mirroring the Phase 1 handlers:

* **404** — the session, lap or trace is not in the database.
* **422** — the rows are real but cannot be analysed together (different circuits, laps
  that never overlap), or the request itself is out of range.
* **503** — anything to do with the database, with an opaque detail and the real reason in
  the log. This endpoint is unauthenticated; it does not narrate its internals.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from f126.analysis.compare import LapRef, compare_laps
from f126.analysis.corners import analyse_corners
from f126.analysis.resample import AnalysisError, json_floats, json_ints, trace_from_rows
from f126.analysis.stints import build_stints

# Shares the Phase 1 connection semantics rather than re-implementing them: a psycopg
# connection and a pool handle are both context managers and entering one is what returns
# it. `app` imports this module from inside `create_app()`, so this direction is safe.
from f126.web.app import _call_with_connection

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from f126.store.queries import Conn, JsonRow

log = logging.getLogger(__name__)

_DB_UNAVAILABLE = "database unavailable"

#: Same shape as the Phase 1 path parameter: a `sessions.id`, wide enough to also carry a
#: u64, rejected at the door before it can reach a query.
SessionId = Annotated[str, PathParam(pattern=r"^[0-9]{1,20}$")]
LapNumber = Annotated[int, PathParam(ge=0, le=5_000)]

SessionQuery = Annotated[int, Query(ge=1, le=10**18)]
LapQuery = Annotated[int, Query(ge=0, le=5_000)]
CarIndexQuery = Annotated[int | None, Query(ge=0, le=21)]
#: `best` (the session-best valid lap of the player that has a trace) or a lap number.
RefQuery = Annotated[str, Query(pattern=r"^(best|[0-9]{1,4})$")]

#: Channels the raw telemetry endpoint emits as integers; the rest go out as floats.
_INTEGER_CHANNELS: frozenset[str] = frozenset({"gear", "rpm", "drs_or_aero"})


def analysis_router(db_conn_factory: Callable[[], Any] | None) -> APIRouter:
    """The `/api` analysis surface. `db_conn_factory` None means every route answers 503."""
    router = APIRouter(prefix="/api", tags=["analysis"])

    @router.get("/sessions/{session_id}/laps/{lap_number}/telemetry", response_model=None)
    async def lap_telemetry(
        session_id: SessionId,
        lap_number: LapNumber,
        car_index: CarIndexQuery = None,
    ) -> Any:
        """One lap's 20 Hz trace as parallel arrays, ordered by distance.

        Not downsampled — the rows are already at `cfg.telemetry_hz`. They are *cleaned*:
        duplicate and backwards distance samples are dropped so the arrays are a genuine
        function of distance, which is what the charts and every other endpoint assume.
        """

        def work(conn: Conn) -> Any:
            session = _session_or_404(conn, int(session_id))
            car = _resolve_car_index(session, car_index)
            trace = _trace_or_404(conn, int(session_id), lap_number)
            payload: dict[str, Any] = {
                "session_id": int(session_id),
                "lap_number": int(lap_number),
                "car_index": car,
                "points": len(trace),
                "distance_m": json_floats(trace.distance_m, digits=2),
            }
            for name, values in trace.channels.items():
                payload[name] = (
                    json_ints(values) if name in _INTEGER_CHANNELS else json_floats(values)
                )
            payload["session_time_s"] = json_floats(trace.session_time_s, digits=4)
            return payload

        return await _run(db_conn_factory, work)

    @router.get("/analysis/compare", response_model=None)
    async def compare(
        session_a: SessionQuery,
        lap_a: LapQuery,
        session_b: SessionQuery,
        lap_b: LapQuery,
    ) -> Any:
        """Two laps resampled onto a shared ~5 m grid, plus the cumulative delta between them.

        Same session or two sessions at the same circuit; 422 if the tracks differ.
        """

        def work(conn: Conn) -> Any:
            a = _lap_ref(conn, session_a, lap_a)
            b = _lap_ref(conn, session_b, lap_b)
            return compare_laps(a, b)

        return await _run(db_conn_factory, work)

    @router.get("/analysis/corners", response_model=None)
    async def corners(
        session_id: SessionQuery,
        lap: LapQuery,
        ref: RefQuery = "best",
    ) -> Any:
        """Corner-by-corner segmentation of one lap against a reference lap.

        `ref=best` picks the session-best valid lap of the player that also has a trace; a
        number picks that lap. When no reference exists at all the corners still come back,
        with every reference-derived field `null`.
        """

        def work(conn: Conn) -> Any:
            subject = _lap_ref(conn, session_id, lap)
            reference = _reference_lap(conn, session_id, subject, ref)
            return analyse_corners(subject, reference)

        return await _run(db_conn_factory, work)

    @router.get("/analysis/stints", response_model=None)
    async def stints(
        session_id: SessionQuery,
        car_index: CarIndexQuery = None,
    ) -> Any:
        """Tyre stints with their degradation fit.

        Grouped by the recorded `tyre_stints` rows; when a capture has none, stints are
        reconstructed from compound changes and tyre-age resets in the lap table, in which
        case only the player's car is reconstructed unless `car_index` says otherwise.
        """

        def work(conn: Conn) -> Any:
            queries = _queries()
            session = _session_or_404(conn, session_id)
            recorded = queries.tyre_stints_for_session(conn, session_id, car_index)
            car = car_index
            if not recorded and car is None:
                car = _resolve_car_index(session, None)
            laps = queries.laps_for_session(conn, session_id, car)
            return build_stints(session_id, recorded, laps, car_index=car)

        return await _run(db_conn_factory, work)

    return router


# ---- shared plumbing ----------------------------------------------------------------


def _queries() -> Any:
    """`store.queries`, imported at call time.

    The store is built by another workstream and the web layer already treats it as
    optional: an import failure degrades these endpoints to 503 rather than breaking app
    construction.
    """
    from f126.store import queries  # noqa: PLC0415

    return queries


async def _run(factory: Callable[[], Any] | None, work: Callable[[Conn], Any]) -> Any:
    """Run one unit of analysis on a borrowed connection, off the event loop.

    `AnalysisError` carries its own HTTP status through untouched — a 404 for an unknown
    lap must not be reported as a database outage. Everything else collapses to 503 with
    the real reason logged, exactly like the Phase 1 `_query` helper.
    """
    if factory is None:
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE)
    try:
        return await asyncio.to_thread(_call_with_connection, factory, work)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        log.exception("analysis query failed")
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE) from None


def _session_or_404(conn: Conn, session_id: int) -> JsonRow:
    session = _queries().session_summary(conn, session_id)
    if session is None:
        raise AnalysisError(404, "session not found")
    return session


def _resolve_car_index(session: Mapping[str, Any], requested: int | None) -> int:
    """Which car the trace belongs to.

    `telemetry_samples` is a player-car table by construction, so asking for anyone else's
    trace is a 404 rather than an empty array pretending to be an answer. Falls back to car
    0 when the session packet never landed and the player index is unknown.
    """
    player = session.get("player_car_index")
    player = 0 if player is None else int(player)
    if requested is not None and requested != player:
        raise AnalysisError(404, "telemetry is recorded for the player car only")
    return player


def _trace_or_404(conn: Conn, session_id: int, lap_number: int) -> Any:
    rows = _queries().telemetry_trace(conn, session_id, lap_number)
    if not rows:
        raise AnalysisError(404, "lap not found")
    return trace_from_rows(rows)


def _lap_ref(conn: Conn, session_id: int, lap_number: int) -> LapRef:
    """Assemble the identity + timing + samples bundle the analysis works on."""
    session = _session_or_404(conn, session_id)
    car = _resolve_car_index(session, None)
    trace = _trace_or_404(conn, session_id, lap_number)
    lap = _queries().lap_row(conn, session_id, car, lap_number) or {}
    return LapRef(
        session_id=int(session["id"]),
        lap_number=int(lap_number),
        trace=trace,
        car_index=car,
        track_id=session.get("track_id"),
        track_name=session.get("track_name"),
        lap_time_ms=lap.get("lap_time_ms"),
        sectors_ms=(lap.get("s1_ms"), lap.get("s2_ms"), lap.get("s3_ms")),
    )


def _reference_lap(conn: Conn, session_id: int, subject: LapRef, ref: str) -> LapRef | None:
    """Resolve `?ref=` to a lap, or None when the session has nothing to compare against."""
    if ref != "best":
        return _lap_ref(conn, session_id, int(ref))
    car = subject.car_index if subject.car_index is not None else 0
    best = _queries().best_lap_with_telemetry(conn, session_id, car)
    if best is None:
        return None
    return _lap_ref(conn, session_id, int(best["lap_number"]))

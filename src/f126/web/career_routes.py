"""Phase 3 career endpoints: season overview and per-track progress.

`docs/analysis-api.md` (Phase 3) is the frozen contract. Thin like `analysis_routes` and
for the same reason — fetch rows, hand them to `f126.analysis.career`, serialise — every
derivation rule lives in that pure module, where it is tested against synthetic sessions
with no database in sight.

Read-only, like the rest of the HTTP surface: two GETs and nothing else, ever
(`tests/test_web.py::test_no_mutating_routes` walks the route table and enforces it).
Career tags are written by the `f126 tag` CLI only; no route here can touch them.

Error conventions, mirroring the analysis routes:

* **404** — the circuit has no recorded sessions.
* **422** — the request itself is out of range (`track_id` below 0: `-1` is the recorder's
  "the Session packet never landed" sentinel, not a circuit).
* **503** — anything to do with the database, with an opaque detail and the real reason in
  the log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter
from fastapi import Path as PathParam

from f126.analysis.career import build_overview, build_track
from f126.analysis.resample import AnalysisError

# Shares the analysis surface's plumbing rather than re-implementing it: the connection
# borrowing, the late `store.queries` import and the by-session grouping are one behaviour
# across the whole /api surface, not three.
from f126.web.analysis_routes import _group, _queries, _run

if TYPE_CHECKING:
    from collections.abc import Callable

    from f126.store.queries import Conn, JsonRow

#: A circuit, with the `-1` sentinel refused at the door — same bounds as the strategy
#: route's query parameter, here as a path segment.
TrackIdPath = Annotated[int, PathParam(ge=0, le=1000)]


def career_router(db_conn_factory: Callable[[], Any] | None) -> APIRouter:
    """The `/api/career` surface. `db_conn_factory` None means every route answers 503."""
    router = APIRouter(prefix="/api", tags=["career"])

    @router.get("/career/overview", response_model=None)
    async def overview() -> Any:
        """Seasons, weekends, totals and personal bests, derived from the whole timeline.

        An empty database is empty arrays, not an error — a career that has not started
        is an ordinary state.
        """

        def work(conn: Conn) -> Any:
            queries = _queries()
            sessions = queries.career_sessions(conn)
            ids = [int(row["id"]) for row in sessions]
            return build_overview(
                sessions,
                _group(queries.player_laps_for_sessions(conn, ids)),
                _group(queries.player_stints_for_sessions(conn, ids)),
                queries.career_tags(conn),
            )

        return await _run(db_conn_factory, work)

    @router.get("/career/tracks/{track_id}", response_model=None)
    async def track(track_id: TrackIdPath) -> Any:
        """One circuit's visits, per-session consistency and the PB detail.

        The whole career is read even though one circuit is served: season and round
        numbers only exist relative to the full timeline. Laps are fetched for this
        circuit's sessions only — the rest of the career contributes nothing here.
        """

        def work(conn: Conn) -> Any:
            queries = _queries()
            sessions = queries.career_sessions(conn)
            at_track = [row for row in sessions if _track_of(row) == track_id]
            if not at_track:
                raise AnalysisError(404, "no sessions recorded at this track")
            ids = [int(row["id"]) for row in at_track]
            return build_track(
                track_id,
                sessions,
                _group(queries.player_laps_for_sessions(conn, ids)),
                _group(queries.player_stints_for_sessions(conn, ids)),
                queries.career_tags(conn),
            )

        return await _run(db_conn_factory, work)

    return router


def _track_of(row: JsonRow) -> int | None:
    track = row.get("track_id")
    return None if track is None else int(track)

"""Read-side helpers for the web API.

Everything here returns plain JSON-ready dicts (psycopg already hands back JSONB as
dict/list, timestamps are stored as epoch doubles, so no custom encoder is needed).
These run on a separate connection from the writer's — never reuse the writer's.

Read semantics worth knowing: `laps` can hold several generations of the same lap
(history reconciliation supersedes a lap we wrote live); readers get the highest
generation per (car_index, lap_number) unless they ask for all of them.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

Conn = psycopg.Connection[Any]
JsonRow = dict[str, Any]

# Superseded lap generations must not be counted twice, nor win the "best lap" race.
_LAP_COUNT = """(SELECT count(DISTINCT (l.car_index, l.lap_number))
               FROM laps l WHERE l.session_id = s.id)"""
# Field best: the fastest valid lap by ANY car (the session's headline time).
_BEST_LAP = """(SELECT min(latest.lap_time_ms) FROM (
               SELECT DISTINCT ON (car_index, lap_number) lap_time_ms, valid
                 FROM laps WHERE session_id = s.id
                ORDER BY car_index, lap_number, generation DESC) latest
            WHERE latest.valid AND latest.lap_time_ms > 0)"""
# Player best: the driver's own fastest valid lap — what "best" means to the person
# reading the session list (showing the AI pole time as "best" misportrayed quali).
_PLAYER_BEST = """(SELECT min(latest.lap_time_ms) FROM (
               SELECT DISTINCT ON (lap_number) lap_time_ms, valid
                 FROM laps WHERE session_id = s.id AND car_index = s.player_car_index
                ORDER BY lap_number, generation DESC) latest
            WHERE latest.valid AND latest.lap_time_ms > 0)"""

_SESSION_LIST_SQL = f"""
    WITH seg AS (
        SELECT s.*,
               {_LAP_COUNT} AS lap_count,
               (SELECT count(DISTINCT (l.car_index, l.lap_number)) FROM laps l
                 WHERE l.session_id = s.id AND l.car_index = s.player_car_index)
                   AS player_lap_count,
               {_PLAYER_BEST} AS best_lap_ms,
               {_BEST_LAP} AS field_best_lap_ms,
               (SELECT p.name FROM participants p
                 WHERE p.session_id = s.id AND p.is_player LIMIT 1) AS player_name
          FROM sessions s
         WHERE s.session_uid <> '0'
    ),
    -- One game session can span several segment rows (pauses, process restarts).
    -- The representative row is the segment with the most laps AMONG those that
    -- know their track/type (pre-fix fragments could die before the 2 Hz Session
    -- packet arrived): that is the one detail/analysis endpoints are pointed at.
    rep AS (
        SELECT DISTINCT ON (session_uid) *
          FROM seg
         ORDER BY session_uid, (track_id >= 0 AND session_type > 0) DESC,
                  lap_count DESC NULLS LAST, id DESC
    ),
    agg AS (
        SELECT session_uid,
               count(*) AS segments,
               min(started_at_wall) AS first_started_at_wall,
               max(ended_at_wall) AS last_ended_at_wall,
               -- Segments re-report the same session history, so MAX (not SUM)
               -- is the honest cross-segment aggregate.
               max(lap_count) AS lap_count,
               max(player_lap_count) AS player_lap_count,
               min(best_lap_ms) AS best_lap_ms,
               min(field_best_lap_ms) AS field_best_lap_ms
          FROM seg
         GROUP BY session_uid
    )
    SELECT rep.id, rep.session_uid, rep.segment, rep.packet_format, rep.session_type,
           rep.session_type_name, rep.track_id, rep.track_name, rep.ended_reason,
           rep.joined_in_progress, rep.player_car_index, rep.total_laps,
           rep.session_duration_s, rep.weather_json, rep.raw_file, rep.player_name,
           agg.segments,
           agg.first_started_at_wall AS started_at_wall,
           agg.last_ended_at_wall AS ended_at_wall,
           agg.lap_count, agg.player_lap_count, agg.best_lap_ms, agg.field_best_lap_ms
      FROM rep JOIN agg USING (session_uid)
     WHERE %s::bool OR coalesce(agg.player_lap_count, 0) > 0 OR coalesce(agg.lap_count, 0) > 0
     ORDER BY agg.first_started_at_wall DESC NULLS LAST, rep.id DESC
     LIMIT %s
"""

_SESSION_ONE_SQL = f"""
    SELECT s.*,
           {_LAP_COUNT} AS lap_count,
           (SELECT count(*) FROM telemetry_samples t WHERE t.session_id = s.id)
               AS telemetry_sample_count,
           (SELECT count(*) FROM wear_samples w WHERE w.session_id = s.id) AS wear_sample_count,
           (SELECT count(*) FROM events e WHERE e.session_id = s.id) AS event_count,
           {_PLAYER_BEST} AS best_lap_ms,
           {_BEST_LAP} AS field_best_lap_ms
      FROM sessions s
     WHERE s.id = %s
"""

_PARTICIPANTS_SQL = """
    SELECT car_index, name, team_id, race_number, is_ai, is_player
      FROM participants
     WHERE session_id = %s
     ORDER BY car_index
"""

_STINTS_SQL = """
    SELECT car_index, stint_no, compound_actual, compound_visual, lap_start, lap_end,
           wear_at_end_json, end_reason
      FROM tyre_stints
     WHERE session_id = %s
     ORDER BY car_index, stint_no
"""

_LAPS_LATEST_SQL = """
    SELECT DISTINCT ON (car_index, lap_number) *
      FROM laps
     WHERE session_id = %s AND (%s::int IS NULL OR car_index = %s::int)
     ORDER BY car_index, lap_number, generation DESC
"""

_LAPS_ALL_SQL = """
    SELECT *
      FROM laps
     WHERE session_id = %s AND (%s::int IS NULL OR car_index = %s::int)
     ORDER BY car_index, lap_number, generation
"""


def _fetch(conn: Conn, sql: str, params: tuple[Any, ...]) -> list[JsonRow]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def list_sessions(conn: Conn, limit: int = 50, *, include_empty: bool = False) -> list[JsonRow]:
    """Most recent game sessions first — segment rows grouped per session_uid.

    Menu noise is excluded: uid-0 rows never appear, and sessions with zero laps
    (post-race menu stubs with a real uid) are hidden unless include_empty.
    """
    limit = max(1, min(int(limit), 500))
    return _fetch(conn, _SESSION_LIST_SQL, (include_empty, limit))


def session_detail(conn: Conn, session_id: int) -> JsonRow | None:
    """One session plus its participants, tyre stints and row counts. None if unknown."""
    rows = _fetch(conn, _SESSION_ONE_SQL, (session_id,))
    if not rows:
        return None
    detail = rows[0]
    detail["participants"] = _fetch(conn, _PARTICIPANTS_SQL, (session_id,))
    detail["tyre_stints"] = _fetch(conn, _STINTS_SQL, (session_id,))
    return detail


def laps_for_session(
    conn: Conn,
    session_id: int,
    car_index: int | None = None,
    *,
    all_generations: bool = False,
    limit: int = 5_000,
) -> list[JsonRow]:
    """Laps ordered by car then lap; only the newest generation of each lap by default.

    Bounded because this backs an unauthenticated endpoint; 5k covers every
    car in the longest race weekend with two orders of magnitude to spare.
    """
    sql = _LAPS_ALL_SQL if all_generations else _LAPS_LATEST_SQL
    sql = f"{sql} LIMIT %s"
    return _fetch(conn, sql, (session_id, car_index, car_index, max(1, int(limit))))


def telemetry_for_lap(
    conn: Conn, session_id: int, lap_number: int, *, limit: int = 20_000
) -> list[JsonRow]:
    """One lap's telemetry trace, ordered by distance (for the lap-comparison charts)."""
    return _fetch(
        conn,
        "SELECT * FROM telemetry_samples"
        " WHERE session_id = %s AND lap_number = %s"
        " ORDER BY lap_distance_m, session_time_s LIMIT %s",
        (session_id, lap_number, max(1, int(limit))),
    )


def events_for_session(conn: Conn, session_id: int, limit: int = 1_000) -> list[JsonRow]:
    """Session events in chronological order."""
    return _fetch(
        conn,
        "SELECT * FROM events WHERE session_id = %s ORDER BY session_time_s, frame_id LIMIT %s",
        (session_id, max(1, int(limit))),
    )


# --------------------------------------------------------------------------------------
# Phase 2 analysis reads (f126.analysis / f126.web.analysis_routes)
#
# Added for the analysis API; the helpers above are untouched. Two things distinguish
# these from the Phase 1 reads. They are *narrow* — the analysis endpoints run several
# queries per request, so paying for `count(*)` over telemetry_samples the way
# `session_detail` does would be wasteful — and they all resolve generations, because an
# analysis that averaged a lap with its own superseded flashback take would be quietly
# wrong rather than loudly broken.
# --------------------------------------------------------------------------------------

_SESSION_SUMMARY_SQL = """
    SELECT id, session_uid, segment, track_id, track_name, session_type, session_type_name,
           player_car_index, total_laps, started_at_wall
      FROM sessions
     WHERE id = %s
"""

_LAP_ROW_SQL = """
    SELECT DISTINCT ON (car_index, lap_number) *
      FROM laps
     WHERE session_id = %s AND car_index = %s AND lap_number = %s
     ORDER BY car_index, lap_number, generation DESC
"""

# "Best" must mean a lap the analysis can actually draw, so the EXISTS clause drops laps
# whose telemetry was never written (the samples are dropped first when the DB is degraded,
# and a lap recorded before capture started has a time but no trace).
_BEST_LAP_WITH_TELEMETRY_SQL = """
    SELECT latest.* FROM (
        SELECT DISTINCT ON (car_index, lap_number) *
          FROM laps
         WHERE session_id = %s AND car_index = %s
         ORDER BY car_index, lap_number, generation DESC) latest
     WHERE latest.valid AND latest.lap_time_ms > 0
       AND (%s::int IS NULL OR latest.lap_number <> %s::int)
       AND EXISTS (SELECT 1 FROM telemetry_samples t
                    WHERE t.session_id = latest.session_id
                      AND t.lap_number = latest.lap_number)
     ORDER BY latest.lap_time_ms, latest.lap_number
     LIMIT 1
"""

# Same idea as _BEST_LAP_WITH_TELEMETRY_SQL, widened to every session at one circuit, so a
# race lap can be measured against the weekend's quali benchmark. Each session has its own
# player index, hence the join on `s.player_car_index` rather than a passed-in car index.
# `s.track_id = %s` is an equality on the sentinel-bearing column: -1 means "the session
# packet never landed", and matching -1 to -1 would compare two unknown circuits, so
# callers pass a real track_id only.
_TRACK_BEST_LAP_WITH_TELEMETRY_SQL = """
    SELECT latest.* FROM (
        SELECT DISTINCT ON (l.session_id, l.car_index, l.lap_number) l.*
          FROM laps l
          JOIN sessions s ON s.id = l.session_id
         WHERE s.track_id = %s AND l.car_index = s.player_car_index
         ORDER BY l.session_id, l.car_index, l.lap_number, l.generation DESC) latest
     WHERE latest.valid AND latest.lap_time_ms > 0
       AND NOT (latest.session_id = %s AND latest.lap_number = %s)
       AND EXISTS (SELECT 1 FROM telemetry_samples t
                    WHERE t.session_id = latest.session_id
                      AND t.lap_number = latest.lap_number)
     ORDER BY latest.lap_time_ms, latest.session_id, latest.lap_number
     LIMIT 1
"""

# telemetry_samples is append-only, so a flashback leaves both takes of the lap in the
# table. The newest generation is the one that counts; COALESCE because rows written
# before the generation column was populated carry NULL.
_TELEMETRY_TRACE_SQL = """
    SELECT t.* FROM telemetry_samples t
     WHERE t.session_id = %s AND t.lap_number = %s
       AND coalesce(t.generation, 0) = (
           SELECT max(coalesce(generation, 0)) FROM telemetry_samples
            WHERE session_id = %s AND lap_number = %s)
     ORDER BY t.lap_distance_m, t.session_time_s
     LIMIT %s
"""

_TYRE_STINTS_SQL = """
    SELECT car_index, stint_no, compound_actual, compound_visual, lap_start, lap_end,
           wear_at_end_json, end_reason
      FROM tyre_stints
     WHERE session_id = %s AND (%s::int IS NULL OR car_index = %s::int)
     ORDER BY car_index, stint_no
"""


def session_summary(conn: Conn, session_id: int) -> JsonRow | None:
    """Track identity and player car for one session. None if unknown.

    The cheap half of `session_detail`: the analysis endpoints need `track_id` (to refuse a
    cross-circuit comparison) and `player_car_index` (to default `car_index`), and nothing
    else it computes.
    """
    rows = _fetch(conn, _SESSION_SUMMARY_SQL, (session_id,))
    return rows[0] if rows else None


def lap_row(conn: Conn, session_id: int, car_index: int, lap_number: int) -> JsonRow | None:
    """One lap's newest generation — times, sectors, validity, compound. None if unknown."""
    rows = _fetch(conn, _LAP_ROW_SQL, (session_id, car_index, lap_number))
    return rows[0] if rows else None


def best_lap_with_telemetry(
    conn: Conn, session_id: int, car_index: int, *, exclude_lap: int | None = None
) -> JsonRow | None:
    """Fastest valid lap of one car that also has a telemetry trace. None if there is none.

    Backs `?ref=best` on the corners endpoint. `exclude_lap` skips one lap number, which is
    how the corners route avoids resolving the reference to the subject lap itself and
    rendering an all-zeros self-comparison.
    """
    rows = _fetch(
        conn, _BEST_LAP_WITH_TELEMETRY_SQL, (session_id, car_index, exclude_lap, exclude_lap)
    )
    return rows[0] if rows else None


def track_best_lap_with_telemetry(
    conn: Conn,
    track_id: int,
    *,
    exclude_session_id: int | None = None,
    exclude_lap: int | None = None,
) -> JsonRow | None:
    """The player's fastest drawable lap at one circuit, across every session there.

    Backs `?ref=track_best`. Only the player car of each session is considered — telemetry
    is a player-only table, so nobody else's lap could be drawn anyway. The excluded pair
    keeps the subject lap out of its own reference.
    """
    rows = _fetch(
        conn,
        _TRACK_BEST_LAP_WITH_TELEMETRY_SQL,
        (track_id, exclude_session_id, exclude_lap),
    )
    return rows[0] if rows else None


def telemetry_trace(
    conn: Conn, session_id: int, lap_number: int, *, limit: int = 20_000
) -> list[JsonRow]:
    """One lap's telemetry, newest generation only, ordered by distance.

    Bounded like every other unauthenticated read: 20k rows is 1000 s of 20 Hz samples,
    two orders of magnitude more than the longest lap on the calendar.
    """
    return _fetch(
        conn,
        _TELEMETRY_TRACE_SQL,
        (session_id, lap_number, session_id, lap_number, max(1, int(limit))),
    )


def tyre_stints_for_session(
    conn: Conn, session_id: int, car_index: int | None = None
) -> list[JsonRow]:
    """Recorded tyre stints, optionally for one car. Empty when stints were never written."""
    return _fetch(conn, _TYRE_STINTS_SQL, (session_id, car_index, car_index))

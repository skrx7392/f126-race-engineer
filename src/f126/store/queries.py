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
_BEST_LAP = """(SELECT min(latest.lap_time_ms) FROM (
               SELECT DISTINCT ON (car_index, lap_number) lap_time_ms, valid
                 FROM laps WHERE session_id = s.id
                ORDER BY car_index, lap_number, generation DESC) latest
            WHERE latest.valid AND latest.lap_time_ms > 0)"""

_SESSION_LIST_SQL = f"""
    SELECT s.*,
           {_LAP_COUNT} AS lap_count,
           (SELECT p.name FROM participants p
             WHERE p.session_id = s.id AND p.is_player LIMIT 1) AS player_name
      FROM sessions s
     ORDER BY s.started_at_wall DESC NULLS LAST, s.id DESC
     LIMIT %s
"""

_SESSION_ONE_SQL = f"""
    SELECT s.*,
           {_LAP_COUNT} AS lap_count,
           (SELECT count(*) FROM telemetry_samples t WHERE t.session_id = s.id)
               AS telemetry_sample_count,
           (SELECT count(*) FROM wear_samples w WHERE w.session_id = s.id) AS wear_sample_count,
           (SELECT count(*) FROM events e WHERE e.session_id = s.id) AS event_count,
           {_BEST_LAP} AS best_lap_ms
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


def list_sessions(conn: Conn, limit: int = 50) -> list[JsonRow]:
    """Most recent sessions first, each with its lap count and the player's name."""
    limit = max(1, min(int(limit), 500))
    return _fetch(conn, _SESSION_LIST_SQL, (limit,))


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
) -> list[JsonRow]:
    """Laps ordered by car then lap; only the newest generation of each lap by default."""
    sql = _LAPS_ALL_SQL if all_generations else _LAPS_LATEST_SQL
    return _fetch(conn, sql, (session_id, car_index, car_index))


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

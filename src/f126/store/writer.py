"""Batching Postgres writer: the only thing in the process that talks to the DB on the hot path.

The state layer emits ``(table_name, row_dict)`` pairs from the asyncio event loop and calls
:meth:`DbWriter.enqueue`, which is a non-blocking hand-off to a dedicated thread. That thread
owns a psycopg connection and flushes everything it has accumulated every ``cfg.db_batch_ms``
inside ONE transaction.

Degradation contract (raw capture logs are the source of truth, Postgres is a derived index):

* ``enqueue`` never blocks and never raises into the caller — a wedged or dead database must
  not stall UDP capture.
* While the connection is down, sample rows (``telemetry_samples``, ``wear_samples``,
  ``events``, ``tyre_stints``, ``raw_captures``) are DROPPED and counted; they are recomputable
  from the raw capture with ``f126 backfill``.
* ``sessions``, ``participants`` and ``laps`` rows are small and precious, so they go into a
  bounded retry buffer instead and are replayed on the first successful reconnect.

Row dicts use the column names in schema.sql. Rows for child tables are keyed by
``session_key_uid`` + ``segment`` (the natural key the state layer knows); the writer resolves
that to the ``sessions.id`` surrogate, creating a placeholder session row if telemetry beats the
session packet to the queue.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from f126.config import Config
from f126.store import db

log = logging.getLogger(__name__)

# --- Coordinate contract with the state layer -------------------------------------------------
# Column names/order per table, identity columns excluded. Kept in Python (rather than
# introspected) so batching works before the first successful connection and so INSERT column
# order is deterministic; tests/test_store.py asserts this stays in sync with schema.sql.

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "session_uid",
        "segment",
        "packet_format",
        "session_type",
        "session_type_name",
        "track_id",
        "track_name",
        "started_at_wall",
        "ended_at_wall",
        "ended_reason",
        "joined_in_progress",
        "player_car_index",
        "total_laps",
        "session_duration_s",
        "weather_json",
        "final_classification_json",
        "raw_file",
    ),
    "participants": (
        "session_id",
        "car_index",
        "name",
        "team_id",
        "race_number",
        "is_ai",
        "is_player",
    ),
    "laps": (
        "session_id",
        "car_index",
        "lap_number",
        "generation",
        "lap_time_ms",
        "s1_ms",
        "s2_ms",
        "s3_ms",
        "valid",
        "compound_actual",
        "compound_visual",
        "tyre_age_laps",
        "fuel_start_kg",
        "fuel_end_kg",
        "ers_deployed_j",
        "ers_harvested_j",
        "top_speed_kmh",
        "penalties_s",
        "wall_ts",
    ),
    "telemetry_samples": (
        "session_id",
        "segment",
        "generation",
        "session_time_s",
        "frame_id",
        "overall_frame_id",
        "lap_number",
        "lap_distance_m",
        "speed_kmh",
        "throttle",
        "brake",
        "steer",
        "gear",
        "rpm",
        "drs_or_aero",
        "tyre_surface_temp",
        "tyre_inner_temp",
        "tyre_pressure",
        "brake_temp",
        "fuel_kg",
        "energy_store_j",
        "energy_deploy_mode",
        "world_x",
        "world_z",
    ),
    "wear_samples": (
        "session_id",
        "segment",
        "session_time_s",
        "lap_number",
        "tyre_wear_pct",
        "damage_json",
    ),
    "events": (
        "session_id",
        "segment",
        "session_time_s",
        "frame_id",
        "wall_ts",
        "code",
        "details_json",
    ),
    "tyre_stints": (
        "session_id",
        "segment",
        "car_index",
        "stint_no",
        "compound_actual",
        "compound_visual",
        "lap_start",
        "lap_end",
        "wear_at_end_json",
        "end_reason",
    ),
    "raw_captures": (
        "session_id",
        "path",
        "bytes",
        "packets",
        "first_wall",
        "last_wall",
        "packet_format",
    ),
    # Written out-of-band by `insert_debrief` rather than through the queue — a debrief is
    # one row per session, generated minutes after the capture path has gone quiet. It is
    # listed here so the column order lives in one place and stays checked against
    # schema.sql by tests/test_store.py.
    "debriefs": (
        "session_id",
        "created_at",
        "model",
        "prompt_version",
        "fact_sheet",
        "text",
    ),
}

# Upsert targets. Everything else is a plain append-only INSERT.
CONFLICT_TARGETS: dict[str, tuple[str, ...]] = {
    "sessions": ("session_uid", "segment", "started_at_wall"),
    "participants": ("session_id", "car_index"),
    "laps": ("session_id", "car_index", "lap_number", "generation"),
    "tyre_stints": ("session_id", "car_index", "stint_no"),
    "raw_captures": ("path",),
}

# Written parents-first so foreign keys resolve inside a single transaction.
TABLE_ORDER: tuple[str, ...] = (
    "sessions",
    "participants",
    "laps",
    "tyre_stints",
    "raw_captures",
    "telemetry_samples",
    "wear_samples",
    "events",
    "debriefs",
)

# Kept in the retry buffer while the DB is down; everything else is dropped and counted.
RETAINED_TABLES = frozenset({"sessions", "participants", "laps"})

JSONB_COLUMNS = frozenset(
    {
        "weather_json",
        "final_classification_json",
        "damage_json",
        "details_json",
        "wear_at_end_json",
        "fact_sheet",
    }
)
REAL_ARRAY_COLUMNS = frozenset(
    {"tyre_surface_temp", "tyre_inner_temp", "tyre_pressure", "brake_temp", "tyre_wear_pct"}
)

_SESSION_CACHE_MAX = 256


# --- SQL construction -------------------------------------------------------------------------


def _placeholder(column: str) -> str:
    """Explicit casts keep psycopg from having to guess REAL[]/JSONB from Python values."""
    if column in REAL_ARRAY_COLUMNS:
        return "%s::real[]"
    if column in JSONB_COLUMNS:
        return "%s::jsonb"
    return "%s"


def insert_sql(table: str, columns: Sequence[str]) -> str:
    """Build the INSERT (with the table's upsert clause, if any) for this exact column set.

    Rows are grouped by their column set before this is called, so a partial row never
    clobbers columns it does not carry. On conflict the update is COALESCE'd: a later,
    less complete row for the same key can fill blanks but can never null out a value.
    """
    cols = ", ".join(columns)
    values = ", ".join(_placeholder(c) for c in columns)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({values})"
    target = CONFLICT_TARGETS.get(table)
    if target is None:
        return sql
    conflict = ", ".join(target)
    updatable = [c for c in columns if c not in target]
    if not updatable:
        return f"{sql} ON CONFLICT ({conflict}) DO NOTHING"
    sets = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in updatable)
    return f"{sql} ON CONFLICT ({conflict}) DO UPDATE SET {sets}"


_SQL_CACHE: dict[tuple[str, tuple[str, ...]], str] = {}


def _cached_sql(table: str, columns: tuple[str, ...]) -> str:
    key = (table, columns)
    sql = _SQL_CACHE.get(key)
    if sql is None:
        sql = insert_sql(table, columns)
        _SQL_CACHE[key] = sql
    return sql


def adapt_value(column: str, value: Any) -> Any:
    """Coerce a row value into something psycopg can bind for this column."""
    if value is None:
        return None
    if column in JSONB_COLUMNS:
        if isinstance(value, Jsonb):
            return value
        if isinstance(value, str):
            try:
                return Jsonb(json.loads(value))
            except TypeError, ValueError:
                return Jsonb(value)
        return Jsonb(value)
    if column in REAL_ARRAY_COLUMNS:
        # psycopg maps a tuple to a composite type, not an array.
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, list):
            return value
        return [value]
    if column == "session_uid":
        return str(value)
    return value


def _row_id(record: Any) -> int | None:
    if record is None:
        return None
    value = record.get("id") if isinstance(record, Mapping) else record[0]
    return None if value is None else int(value)


def insert_debrief(
    conn: Any,
    session_id: int,
    *,
    created_at: float,
    model: str,
    prompt_version: int,
    fact_sheet: Mapping[str, Any],
    text: str,
) -> int | None:
    """Append one `debriefs` row and return its id. Commits on the caller's connection.

    Deliberately not routed through :class:`DbWriter`. That queue exists to keep the UDP hot
    path off the database; a debrief is one row written minutes after the session ended, from
    a CLI invocation or a background task that is allowed to block, and it needs the row id
    back. Column order comes from ``TABLE_COLUMNS`` so this stays in step with schema.sql.

    Regeneration is an INSERT, never an UPDATE: readers take the newest row, so a debrief
    that came out badly is superseded rather than destroyed.
    """
    columns = TABLE_COLUMNS["debriefs"]
    values = {
        "session_id": int(session_id),
        "created_at": float(created_at),
        "model": model,
        "prompt_version": int(prompt_version),
        "fact_sheet": fact_sheet,
        "text": text,
    }
    sql = f"{insert_sql('debriefs', columns)} RETURNING id"
    params = tuple(adapt_value(column, values[column]) for column in columns)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row_id = _row_id(cur.fetchone())
    # A bare psycopg connection is not autocommit; the callers here own short-lived
    # connections and would otherwise discard the row on close.
    if not getattr(conn, "autocommit", True):
        conn.commit()
    return row_id


# --- Stats ------------------------------------------------------------------------------------


@dataclass(slots=True)
class WriterStats:
    """Snapshot of writer health. Handed to the web API's /api/health."""

    enabled: bool = False
    connected: bool = False
    queue_depth: int = 0
    retry_depth: int = 0
    written_total: dict[str, int] = field(default_factory=dict)
    written_rows: int = 0
    dropped_total: int = 0
    dropped_by_table: dict[str, int] = field(default_factory=dict)
    flushes: int = 0
    reconnects: int = 0
    last_error: str | None = None
    last_flush_wall: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "queue_depth": self.queue_depth,
            "retry_depth": self.retry_depth,
            "written_total": dict(self.written_total),
            "written_rows": self.written_rows,
            "dropped_total": self.dropped_total,
            "dropped_by_table": dict(self.dropped_by_table),
            "flushes": self.flushes,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_flush_wall": self.last_flush_wall,
        }


class _Wake:
    """Queue sentinel: wakes the writer thread out of its blocking get."""

    __slots__ = ()


@dataclass(slots=True)
class _FlushMarker:
    """Queue sentinel: set once everything queued before it has been processed."""

    event: threading.Event


_WAKE = _Wake()

Row = dict[str, Any]
Batch = dict[str, list[Row]]
_Item = tuple[str, Row] | _Wake | _FlushMarker


# --- The writer -------------------------------------------------------------------------------


class DbWriter:
    """Thread-owned Postgres writer. Construct, :meth:`start`, ``enqueue`` freely, :meth:`stop`."""

    __slots__ = (
        "_backoff",
        "_backoff_initial",
        "_backoff_max",
        "_batch_s",
        "_connect",
        "_conn",
        "_dropped",
        "_dropped_by",
        "_enabled",
        "_flushes",
        "_last_error",
        "_last_flush_wall",
        "_lock",
        "_max_batch",
        "_next_attempt",
        "_placeholders",
        "_q_durable",
        "_q_sample",
        "_reconnects",
        "_retry",
        "_session_ids",
        "_state_connected",
        "_stop",
        "_tx_new",
        "_thread",
        "_url",
        "_written",
    )

    def __init__(
        self,
        cfg: Config,
        *,
        connect: Callable[[str], db.Conn] | None = None,
        queue_maxsize: int = 50_000,
        retry_maxlen: int = 20_000,
        max_batch_rows: int = 5_000,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 60.0,
    ) -> None:
        self._url = cfg.database_url
        self._enabled = bool(cfg.database_url)
        self._batch_s = max(cfg.db_batch_ms, 1) / 1000.0
        self._connect = connect if connect is not None else db.connect
        self._max_batch = max_batch_rows

        self._q_sample: queue.Queue[_Item] = queue.Queue(maxsize=queue_maxsize)
        # Durable rows get their own queue so a sample backlog can never starve them.
        self._q_durable: queue.Queue[_Item] = queue.Queue(maxsize=queue_maxsize)
        self._retry: deque[tuple[str, Row]] = deque(maxlen=retry_maxlen)

        self._conn: db.Conn | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._backoff = backoff_initial_s
        self._next_attempt = 0.0

        self._session_ids: dict[tuple[str, int], int] = {}
        self._placeholders: dict[tuple[str, int], int] = {}
        # Cache entries added by the in-flight transaction; dropped if it never commits.
        self._tx_new: set[tuple[str, int]] = set()

        self._written: dict[str, int] = {}
        self._dropped = 0
        self._dropped_by: dict[str, int] = {}
        self._flushes = 0
        self._reconnects = 0
        self._last_error: str | None = None
        self._last_flush_wall: float | None = None
        self._state_connected = False

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the writer thread. No-op when no database URL is configured."""
        if not self._enabled:
            log.info("db writer disabled (no F126_DATABASE_URL); capture continues to raw log")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="f126-db-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Flush what is queued, close the connection, join the thread. Idempotent."""
        self._stop.set()
        # A full queue means the thread is already busy and will notice the stop flag.
        with contextlib.suppress(queue.Full):
            self._q_sample.put_nowait(_WAKE)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover - only on a wedged libpq call
                log.warning("db writer thread did not stop within %.1fs", timeout)

    close = stop

    def __enter__(self) -> DbWriter:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- producer side (event loop thread) -----------------------------------------------------

    def enqueue(self, table: str, row: Row) -> bool:
        """Hand one row to the writer thread. Never blocks, never raises.

        Returns False when the row was dropped (writer disabled, unknown table, queue full).
        """
        if table not in TABLE_COLUMNS:
            self._count_drop(f"unknown:{table}", 1)
            log.warning("dropping row for unknown table %r", table)
            return False
        if not self._enabled:
            self._count_drop(table, 1)
            return False
        q = self._q_durable if table in RETAINED_TABLES else self._q_sample
        try:
            q.put_nowait((table, row))
        except queue.Full:
            self._count_drop(table, 1)
            return False
        return True

    def enqueue_many(self, rows: Iterable[tuple[str, Row]]) -> int:
        """Enqueue a batch; returns how many were accepted."""
        return sum(1 for table, row in rows if self.enqueue(table, row))

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything queued before this call has been processed.

        Returns True if the writer was connected when the flush completed — a False here
        means the rows were dropped or buffered, not written. Used by tests and shutdown.
        """
        if not self._enabled or self._thread is None:
            return False
        marker = _FlushMarker(threading.Event())
        try:
            self._q_durable.put_nowait(marker)
        except queue.Full:  # pragma: no cover
            return False
        completed = marker.event.wait(timeout)
        return completed and self._state_connected

    def stats(self) -> WriterStats:
        """Snapshot of writer health; cheap enough to call per HTTP request."""
        with self._lock:
            return WriterStats(
                enabled=self._enabled,
                connected=self._state_connected,
                queue_depth=self._q_sample.qsize() + self._q_durable.qsize(),
                retry_depth=len(self._retry),
                written_total=dict(self._written),
                written_rows=sum(self._written.values()),
                dropped_total=self._dropped,
                dropped_by_table=dict(self._dropped_by),
                flushes=self._flushes,
                reconnects=self._reconnects,
                last_error=self._last_error,
                last_flush_wall=self._last_flush_wall,
            )

    # -- consumer side (writer thread) ---------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                batch, markers, stopping = self._collect()
                self._flush_batch(batch, final=stopping)
            except Exception as exc:  # pragma: no cover - last-resort guard
                log.exception("db writer loop error")
                self._record_error(exc)
                batch, markers, stopping = {}, [], self._stop.is_set()
                time.sleep(0.1)
            for marker in markers:
                marker.event.set()
            if stopping:
                break
        self._abandon_queued()
        with self._lock:
            retry_depth = len(self._retry)
        if retry_depth:
            log.warning(
                "db writer stopping with %d durable rows unreplayed — "
                "recover them with `f126 backfill`",
                retry_depth,
            )
        self._disconnect(quiet=True)

    def _collect(self) -> tuple[Batch, list[_FlushMarker], bool]:
        """Accumulate rows for up to one batch window. Returns (batch, markers, stopping)."""
        batch: Batch = {}
        markers: list[_FlushMarker] = []
        deadline = time.monotonic() + self._batch_s
        count = 0
        while True:
            # Durable rows first: they are the ones we refuse to lose, and parents before children.
            count += self._drain(self._q_durable, batch, markers, limit=None)
            # A pending flush marker means "everything queued before me", so ignore the size cap.
            limit = None if markers else max(self._max_batch - count, 0)
            count += self._drain(self._q_sample, batch, markers, limit=limit)
            if self._stop.is_set():
                return batch, markers, True
            if markers or count >= self._max_batch:
                return batch, markers, False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return batch, markers, False
            try:
                item = self._q_sample.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if self._accept(item, batch, markers):
                count += 1

    def _drain(
        self,
        q: queue.Queue[_Item],
        batch: Batch,
        markers: list[_FlushMarker],
        *,
        limit: int | None,
    ) -> int:
        taken = 0
        while limit is None or taken < limit:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if self._accept(item, batch, markers):
                taken += 1
        return taken

    @staticmethod
    def _accept(
        item: _Item,
        batch: Batch,
        markers: list[_FlushMarker],
    ) -> bool:
        if isinstance(item, _FlushMarker):
            markers.append(item)
            return False
        if isinstance(item, _Wake):
            return False
        table, row = item
        batch.setdefault(table, []).append(row)
        return True

    def _flush_batch(self, batch: Batch, *, final: bool = False) -> None:
        has_rows = bool(batch) or bool(self._retry)
        if not self._ensure_connection(force=final and has_rows):
            self._degrade(batch)
            return
        if self._retry:
            batch = self._merge_retry(batch)
        if not batch:
            return
        conn = self._conn
        assert conn is not None
        written: dict[str, int] = {}
        try:
            with conn.cursor() as cur:
                self._write(cur, batch, written)
            conn.commit()
            self._tx_new.clear()
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # Connection-level failure: nothing in this batch landed.
            self._record_error(exc)
            self._disconnect()
            self._schedule_retry()
            self._degrade(batch)
            return
        except psycopg.Error as exc:
            # Data/statement-level failure: the batch is poisoned by (probably) one bad row.
            # Roll back and retry row by row so one bad row cannot wedge the writer.
            self._record_error(exc)
            log.warning("batch write failed (%s); retrying row-by-row", exc)
            self._rollback()
            written = {}
            self._write_isolated(batch, written)
        self._commit_stats(written)

    def _merge_retry(self, batch: Batch) -> Batch:
        merged: Batch = {}
        for table, row in self._retry:
            merged.setdefault(table, []).append(row)
        self._retry.clear()
        for table, rows in batch.items():
            merged.setdefault(table, []).extend(rows)
        return merged

    def _write(
        self,
        cur: psycopg.Cursor[Any],
        batch: Batch,
        written: dict[str, int],
    ) -> None:
        for table in TABLE_ORDER:
            rows = batch.get(table)
            if not rows:
                continue
            if table == "sessions":
                for row in rows:
                    if self._upsert_session(cur, row) is not None:
                        written["sessions"] = written.get("sessions", 0) + 1
                continue
            groups: dict[tuple[str, ...], list[tuple[Any, ...]]] = {}
            for row in rows:
                prepared = self._prepare(cur, table, row)
                if prepared is None:
                    continue
                groups.setdefault(tuple(prepared), []).append(tuple(prepared.values()))
            for columns, values in groups.items():
                cur.executemany(_cached_sql(table, columns), values)
                written[table] = written.get(table, 0) + len(values)

    def _write_isolated(self, batch: Batch, written: dict[str, int]) -> None:
        """Slow path after a statement error: one transaction per row, bad rows counted."""
        conn = self._conn
        if conn is None:
            self._degrade(batch)
            return
        for table in TABLE_ORDER:
            for row in batch.get(table, []):
                try:
                    with conn.cursor() as cur:
                        if table == "sessions":
                            ok = self._upsert_session(cur, row) is not None
                        else:
                            prepared = self._prepare(cur, table, row)
                            ok = prepared is not None
                            if prepared is not None:
                                cur.execute(
                                    _cached_sql(table, tuple(prepared)),
                                    tuple(prepared.values()),
                                )
                    conn.commit()
                    self._tx_new.clear()
                    if ok:
                        written[table] = written.get(table, 0) + 1
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self._record_error(exc)
                    self._disconnect()
                    self._schedule_retry()
                    self._degrade({table: [row]})
                    return
                except psycopg.Error as exc:
                    self._record_error(exc)
                    self._rollback()
                    self._count_drop(table, 1)
                    log.warning("dropping bad %s row: %s", table, exc)

    # -- session id resolution -----------------------------------------------------------------

    def _prepare(self, cur: psycopg.Cursor[Any], table: str, row: Mapping[str, Any]) -> Row | None:
        """Filter a row to real columns and resolve its sessions.id. None means "drop it"."""
        columns = TABLE_COLUMNS[table]
        session_id = row.get("session_id")
        if session_id is None:
            uid = row.get("session_key_uid", row.get("session_uid"))
            if uid is not None:
                session_id = self._resolve_session_id(cur, str(uid), _int(row.get("segment")))
        if session_id is None and table != "raw_captures":
            self._count_drop(table, 1)
            log.warning("dropping %s row with no resolvable session key", table)
            return None
        prepared: Row = {}
        for column in columns:
            if column == "session_id":
                if session_id is not None:
                    prepared[column] = session_id
            elif column in row:
                prepared[column] = adapt_value(column, row[column])
        if not prepared:
            self._count_drop(table, 1)
            return None
        return prepared

    def _resolve_session_id(self, cur: psycopg.Cursor[Any], uid: str, segment: int) -> int | None:
        key = (uid, segment)
        cached = self._session_ids.get(key)
        if cached is not None:
            return cached
        cur.execute(
            "SELECT id FROM sessions WHERE session_uid = %s AND segment = %s"
            " ORDER BY started_at_wall DESC NULLS LAST, id DESC LIMIT 1",
            (uid, segment),
        )
        session_id = _row_id(cur.fetchone())
        if session_id is None:
            # Child rows beat the session packet to the queue: park them on a placeholder row
            # that the real session row will adopt (see _upsert_session).
            cur.execute(
                "INSERT INTO sessions (session_uid, segment) VALUES (%s, %s)"
                " ON CONFLICT (session_uid, segment, started_at_wall)"
                " DO UPDATE SET segment = EXCLUDED.segment RETURNING id",
                (uid, segment),
            )
            session_id = _row_id(cur.fetchone())
            if session_id is None:  # pragma: no cover - RETURNING always yields a row
                return None
            self._placeholders[key] = session_id
        self._remember(key, session_id)
        return session_id

    def _upsert_session(self, cur: psycopg.Cursor[Any], row: Mapping[str, Any]) -> int | None:
        raw_uid = row.get("session_uid", row.get("session_key_uid"))
        if raw_uid is None:
            self._count_drop("sessions", 1)
            log.warning("dropping sessions row with no session_uid")
            return None
        uid = str(raw_uid)
        segment = _int(row.get("segment"))
        key = (uid, segment)

        values: dict[str, Any] = {"session_uid": uid, "segment": segment}
        for column in TABLE_COLUMNS["sessions"]:
            if column in values or column not in row:
                continue
            values[column] = adapt_value(column, row[column])

        placeholder_id = self._placeholders.pop(key, None)
        if placeholder_id is not None:
            # Promote the placeholder in place so child rows already pointing at it stay attached.
            assignments = ", ".join(
                f"{c} = COALESCE(%s, {c})" for c in values if c not in ("session_uid", "segment")
            )
            params = [v for c, v in values.items() if c not in ("session_uid", "segment")]
            sql = (
                f"UPDATE sessions SET {assignments} WHERE id = %s RETURNING id"
                if assignments
                else "UPDATE sessions SET segment = segment WHERE id = %s RETURNING id"
            )
            cur.execute(sql, (*params, placeholder_id))
            session_id = _row_id(cur.fetchone()) or placeholder_id
        else:
            columns = tuple(values)
            sql = f"{_cached_sql('sessions', columns)} RETURNING id"
            cur.execute(sql, tuple(values.values()))
            session_id = _row_id(cur.fetchone())
        if session_id is None:  # pragma: no cover - RETURNING always yields a row
            return None
        self._remember(key, session_id)
        return session_id

    def _remember(self, key: tuple[str, int], session_id: int) -> None:
        self._session_ids[key] = session_id
        self._tx_new.add(key)
        while len(self._session_ids) > _SESSION_CACHE_MAX:
            oldest = next(iter(self._session_ids))
            self._session_ids.pop(oldest, None)
            self._placeholders.pop(oldest, None)
            self._tx_new.discard(oldest)

    # -- connection management -----------------------------------------------------------------

    def _ensure_connection(self, *, force: bool = False) -> bool:
        conn = self._conn
        if conn is not None and not conn.closed:
            return True
        self._conn = None
        now = time.monotonic()
        if not force and now < self._next_attempt:
            return False
        try:
            self._conn = self._connect(self._url)
        except Exception as exc:
            self._record_error(exc)
            self._set_connected(False)
            self._schedule_retry()
            return False
        self._backoff = self._backoff_initial
        self._next_attempt = 0.0
        with self._lock:
            self._reconnects += 1
        self._set_connected(True)
        log.info("db writer connected (retry buffer holds %d rows)", len(self._retry))
        return True

    def _schedule_retry(self) -> None:
        self._next_attempt = time.monotonic() + self._backoff * (1.0 + random.random() * 0.1)
        self._backoff = min(self._backoff * 2.0, self._backoff_max)

    def _abandon_queued(self) -> None:
        """At stop: whatever is still queued is counted as dropped, not vanished."""
        while True:
            try:
                item = self._q_sample.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                self._count_drop(item[0], 1)
            elif isinstance(item, _FlushMarker):
                item.event.set()

    def _disconnect(self, quiet: bool = False) -> None:
        self._forget_uncommitted()
        conn, self._conn = self._conn, None
        self._set_connected(False, quiet=quiet)
        if conn is not None:
            with contextlib.suppress(Exception):  # closing an already-broken socket
                conn.close()

    def _forget_uncommitted(self) -> None:
        """Session ids resolved inside a transaction that never committed are fiction."""
        for key in self._tx_new:
            self._session_ids.pop(key, None)
            self._placeholders.pop(key, None)
        self._tx_new.clear()

    def _rollback(self) -> None:
        self._forget_uncommitted()
        conn = self._conn
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception as exc:  # pragma: no cover
            self._record_error(exc)
            self._disconnect()
            self._schedule_retry()

    def _degrade(self, batch: Mapping[str, list[Row]]) -> None:
        """DB is unusable: keep the precious rows, count everything else as dropped."""
        for table in TABLE_ORDER:
            rows = batch.get(table)
            if not rows:
                continue
            if table in RETAINED_TABLES:
                for row in rows:
                    if len(self._retry) == self._retry.maxlen:
                        self._retry.popleft()
                        self._count_drop(table, 1)
                    self._retry.append((table, row))
            else:
                self._count_drop(table, len(rows))

    # -- stats plumbing ------------------------------------------------------------------------

    def _commit_stats(self, written: Mapping[str, int]) -> None:
        with self._lock:
            for table, n in written.items():
                self._written[table] = self._written.get(table, 0) + n
            self._flushes += 1
            self._last_flush_wall = time.time()

    def _count_drop(self, table: str, n: int) -> None:
        with self._lock:
            self._dropped += n
            self._dropped_by[table] = self._dropped_by.get(table, 0) + n

    def _record_error(self, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}".strip()
        with self._lock:
            self._last_error = message

    def _set_connected(self, value: bool, quiet: bool = False) -> None:
        with self._lock:
            was, self._state_connected = self._state_connected, value
        if was and not value and not quiet:
            log.warning("db writer lost its connection; samples will be dropped until it returns")


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except TypeError, ValueError:
        return default

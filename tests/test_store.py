"""Tests for the Postgres store layer.

Tier 1 (always runs) is pure logic against a fake connection: SQL construction, batch
accumulation, session-id cache resolution, degradation counting, retry-buffer bounds.

Tier 2 needs a real Postgres and is skipped unless F126_TEST_DATABASE_URL is set::

    docker run --rm -e POSTGRES_PASSWORD=t -p 5555:5432 -d postgres:18
    F126_TEST_DATABASE_URL=postgresql://postgres:t@localhost:5555/postgres \\
        uv run pytest tests/test_store.py

Each tier-2 test gets its own scratch Postgres schema (via ``options=-csearch_path=...``)
which is dropped afterwards, so pointing this at a database with other content is safe.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg.types.json import Jsonb

from f126.config import Config
from f126.store import db, queries
from f126.store.writer import (
    CONFLICT_TARGETS,
    RETAINED_TABLES,
    TABLE_COLUMNS,
    TABLE_ORDER,
    DbWriter,
    adapt_value,
    insert_sql,
)

UID = "9876543210123456789"

# --------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))
        self.conn.fail_if_poisoned(params)
        stripped = sql.lstrip()
        if stripped.startswith("UPDATE sessions") and params:
            self.conn.result = {"id": params[-1]}
        elif "RETURNING id" in sql:
            self.conn.next_id += 1
            self.conn.result = {"id": self.conn.next_id}
        elif stripped.startswith("SELECT id FROM sessions"):
            self.conn.result = self.conn.session_lookup
        else:
            self.conn.result = None

    def executemany(self, sql: str, seq: Any) -> None:
        rows = list(seq)
        for row in rows:
            self.conn.fail_if_poisoned(row)
        self.conn.many.append((sql, rows))

    def fetchone(self) -> Any:
        return self.conn.result

    def close(self) -> None:
        pass


class FakeConn:
    """Just enough psycopg surface for DbWriter: cursor/commit/rollback/close/closed."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.many: list[tuple[str, list[Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.next_id = 0
        self.result: Any = None
        self.session_lookup: Any = None
        self.poison: Any = None

    def fail_if_poisoned(self, params: Any) -> None:
        if self.poison is not None and params is not None and self.poison in tuple(params):
            raise psycopg.ProgrammingError("poisoned row")

    def cursor(self, **_kwargs: Any) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def rows_for(self, table: str) -> list[Any]:
        out: list[Any] = []
        for sql, rows in self.many:
            if sql.startswith(f"INSERT INTO {table} "):
                out.extend(rows)
        return out


def make_writer(conn: FakeConn | None = None, **kwargs: Any) -> tuple[DbWriter, FakeConn]:
    conn = conn if conn is not None else FakeConn()
    # A long batch window keeps tier-1 batching deterministic: flush()/stop() drive the writes.
    cfg = Config(database_url="postgresql://fake/f126", db_batch_ms=5000)
    writer = DbWriter(cfg, connect=lambda _url: conn, **kwargs)  # type: ignore[arg-type]
    return writer, conn


def telemetry_row(i: int, uid: str = UID) -> dict[str, Any]:
    return {
        "session_key_uid": uid,
        "segment": 0,
        "generation": 0,
        "session_time_s": round(i * 0.05, 4),
        "frame_id": 1000 + i,
        "overall_frame_id": 5000 + i,
        "lap_number": 1 + i // 40,
        "lap_distance_m": float((i % 40) * 130),
        "speed_kmh": 180.0 + (i % 120),
        "throttle": (i % 10) / 10.0,
        "brake": 0.0 if i % 10 else 0.8,
        "steer": ((i % 21) - 10) / 10.0,
        "gear": 1 + (i % 8),
        "rpm": 9000 + (i % 3000),
        "drs_or_aero": i % 2,
        "tyre_surface_temp": [95.0, 96.5, 98.0, 99.5],
        "tyre_inner_temp": [102.0, 103.5, 105.0, 106.5],
        "tyre_pressure": [23.1, 23.2, 22.8, 22.9],
        "brake_temp": [410.0, 415.0, 480.0, 485.0],
        "fuel_kg": 100.0 - i * 0.01,
        "energy_store_j": 4_000_000.0 - (i % 1000) * 10,
        "energy_deploy_mode": 2,
        "world_x": float(i),
        "world_z": float(-i),
    }


def lap_row(lap: int, car_index: int = 0, uid: str = UID, **overrides: Any) -> dict[str, Any]:
    row = {
        "session_key_uid": uid,
        "segment": 0,
        "car_index": car_index,
        "lap_number": lap,
        "generation": 0,
        "lap_time_ms": 92_000 + lap * 37,
        "s1_ms": 28_000 + lap,
        "s2_ms": 34_000 + lap,
        "s3_ms": 30_000 + lap * 35,
        "valid": True,
        "compound_actual": 17,
        "compound_visual": 16,
        "tyre_age_laps": lap,
        "fuel_start_kg": 100.0 - lap,
        "fuel_end_kg": 99.0 - lap,
        "ers_deployed_j": 3.9e6,
        "ers_harvested_j": 2.1e6,
        "top_speed_kmh": 318.0,
        "penalties_s": 0,
        "wall_ts": 1_700_000_000.0 + lap * 92.0,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------------------------
# Tier 1: schema/contract
# --------------------------------------------------------------------------------------------


def _schema_columns() -> dict[str, list[str]]:
    """Parse column names out of schema.sql, skipping constraints and identity columns."""
    sql = db.schema_sql()
    tables: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", sql, re.S):
        name, body = match.group(1), match.group(2)
        columns: list[str] = []
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            first = line.split()[0].upper()
            if first in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT", "EXCLUDE"}:
                continue
            if "GENERATED ALWAYS AS IDENTITY" in line:
                continue
            columns.append(line.split()[0])
        tables[name] = columns
    return tables


def test_schema_sql_matches_table_columns() -> None:
    parsed = _schema_columns()
    assert set(parsed) == set(TABLE_COLUMNS) | {"schema_meta"}
    for table, columns in TABLE_COLUMNS.items():
        assert list(columns) == parsed[table], f"{table} drifted from schema.sql"


def test_schema_sql_is_parameter_free_and_idempotent() -> None:
    sql = db.schema_sql()
    assert "%s" not in sql  # applied as one multi-statement execute()
    assert "DROP TABLE" not in sql.upper()
    assert sql.upper().count("CREATE TABLE ") == sql.upper().count("CREATE TABLE IF NOT EXISTS ")


def test_conflict_targets_are_real_columns() -> None:
    for table, target in CONFLICT_TARGETS.items():
        for column in target:
            assert column in TABLE_COLUMNS[table]
    assert set(TABLE_ORDER) == set(TABLE_COLUMNS)
    assert set(TABLE_COLUMNS) >= RETAINED_TABLES


def test_insert_sql_shapes() -> None:
    plain = insert_sql("events", ("session_id", "code", "details_json"))
    assert plain == (
        "INSERT INTO events (session_id, code, details_json) VALUES (%s, %s, %s::jsonb)"
    )

    upsert = insert_sql("laps", ("session_id", "car_index", "lap_number", "generation", "s1_ms"))
    assert "ON CONFLICT (session_id, car_index, lap_number, generation) DO UPDATE SET" in upsert
    assert "s1_ms = COALESCE(EXCLUDED.s1_ms, laps.s1_ms)" in upsert
    assert "car_index = COALESCE" not in upsert  # key columns are never updated

    keys_only = insert_sql("participants", ("session_id", "car_index"))
    assert keys_only.endswith("ON CONFLICT (session_id, car_index) DO NOTHING")

    arrays = insert_sql("telemetry_samples", ("session_id", "tyre_pressure"))
    assert "%s::real[]" in arrays


def test_adapt_value() -> None:
    assert isinstance(adapt_value("details_json", {"a": 1}), Jsonb)
    assert isinstance(adapt_value("details_json", '{"a": 1}'), Jsonb)
    assert adapt_value("details_json", None) is None
    assert adapt_value("tyre_pressure", (1.0, 2.0)) == [1.0, 2.0]
    assert adapt_value("tyre_pressure", [1.0]) == [1.0]
    assert adapt_value("session_uid", 12345) == "12345"
    assert adapt_value("speed_kmh", 301.5) == 301.5


# --------------------------------------------------------------------------------------------
# Tier 1: batching + session resolution
# --------------------------------------------------------------------------------------------


def test_batch_groups_rows_by_column_set() -> None:
    writer, conn = make_writer()
    with writer:
        writer.enqueue("telemetry_samples", telemetry_row(0))
        writer.enqueue("telemetry_samples", telemetry_row(1))
        partial = telemetry_row(2)
        partial.pop("world_x")
        partial.pop("world_z")
        writer.enqueue("telemetry_samples", partial)
        assert writer.flush() is True

    inserts = [(sql, rows) for sql, rows in conn.many if "telemetry_samples" in sql]
    assert len(inserts) == 2  # one executemany per distinct column set
    assert sorted(len(rows) for _sql, rows in inserts) == [1, 2]
    assert conn.commits == 1
    assert writer.stats().written_total["telemetry_samples"] == 3


def test_whole_batch_is_one_transaction() -> None:
    writer, conn = make_writer()
    with writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 1.0})
        writer.enqueue("participants", {"session_key_uid": UID, "car_index": 0, "name": "KR"})
        writer.enqueue("laps", lap_row(1))
        for i in range(5):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        writer.flush()
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_session_id_resolved_once_and_cached() -> None:
    writer, conn = make_writer()
    with writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 10.0})
        for i in range(4):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        writer.flush()
        # A later batch must not re-query: the cache survives flushes.
        for i in range(4, 8):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        writer.flush()

    lookups = [s for s, _p in conn.executed if s.lstrip().startswith("SELECT id FROM sessions")]
    assert lookups == []  # the sessions upsert seeded the cache
    session_inserts = [s for s, _p in conn.executed if s.startswith("INSERT INTO sessions")]
    assert len(session_inserts) == 1
    assert session_inserts[0].endswith("RETURNING id")

    telemetry = conn.rows_for("telemetry_samples")
    assert len(telemetry) == 8
    assert {row[0] for row in telemetry} == {1}  # session_id column, same surrogate everywhere


def test_child_rows_create_a_placeholder_session_that_is_later_adopted() -> None:
    writer, conn = make_writer()
    with writer:
        writer.enqueue("telemetry_samples", telemetry_row(0))
        writer.flush()
        placeholder_inserts = [s for s, _p in conn.executed if s.startswith("INSERT INTO sessions")]
        assert len(placeholder_inserts) == 1

        writer.enqueue(
            "sessions",
            {"session_uid": UID, "segment": 0, "started_at_wall": 5.0, "track_name": "Suzuka"},
        )
        writer.flush()

    updates = [(s, p) for s, p in conn.executed if s.startswith("UPDATE sessions")]
    assert len(updates) == 1, "the real session row must adopt the placeholder, not insert a second"
    assert updates[0][1][-1] == 1  # WHERE id = <placeholder id>
    assert len([s for s, _p in conn.executed if s.startswith("INSERT INTO sessions")]) == 1


def test_rows_without_a_session_key_are_dropped_not_raised() -> None:
    writer, conn = make_writer()
    with writer:
        writer.enqueue("laps", {"car_index": 0, "lap_number": 1})
        writer.flush()
    assert writer.stats().dropped_by_table["laps"] == 1
    assert conn.rows_for("laps") == []


def test_unknown_table_and_disabled_writer_never_raise() -> None:
    writer, _conn = make_writer()
    assert writer.enqueue("not_a_table", {"x": 1}) is False
    assert writer.stats().dropped_total == 1

    disabled = DbWriter(Config(database_url=""))
    disabled.start()
    assert disabled.enqueue("laps", lap_row(1)) is False
    stats = disabled.stats()
    assert stats.enabled is False and stats.connected is False and stats.dropped_total == 1
    disabled.stop()


def test_enqueue_does_not_block_when_the_queue_is_full() -> None:
    writer, _conn = make_writer(queue_maxsize=2)  # thread never started: nothing drains
    assert writer.enqueue("telemetry_samples", telemetry_row(0)) is True
    assert writer.enqueue("telemetry_samples", telemetry_row(1)) is True
    assert writer.enqueue("telemetry_samples", telemetry_row(2)) is False
    stats = writer.stats()
    assert stats.dropped_by_table["telemetry_samples"] == 1
    assert stats.queue_depth == 2


# --------------------------------------------------------------------------------------------
# Tier 1: degradation
# --------------------------------------------------------------------------------------------


def _dead_connect(_url: str) -> Any:
    raise psycopg.OperationalError("connection refused")


def test_degradation_drops_samples_and_retains_durable_rows() -> None:
    cfg = Config(database_url="postgresql://fake/f126", db_batch_ms=5000)
    writer = DbWriter(cfg, connect=_dead_connect, backoff_initial_s=30.0)
    with writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 1.0})
        for i in range(2):
            writer.enqueue("participants", {"session_key_uid": UID, "car_index": i, "name": "x"})
        for lap in range(3):
            writer.enqueue("laps", lap_row(lap))
        for i in range(100):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        for i in range(10):
            writer.enqueue("events", {"session_key_uid": UID, "code": "PENA", "frame_id": i})
        assert writer.flush() is False

    stats = writer.stats()
    assert stats.connected is False
    assert stats.dropped_total == 110
    assert stats.dropped_by_table == {"telemetry_samples": 100, "events": 10}
    assert stats.retry_depth == 6  # 1 session + 2 participants + 3 laps
    assert stats.written_total == {}
    assert "OperationalError" in (stats.last_error or "")


def test_retry_buffer_is_bounded_and_counts_evictions() -> None:
    cfg = Config(database_url="postgresql://fake/f126", db_batch_ms=5000)
    writer = DbWriter(cfg, connect=_dead_connect, retry_maxlen=5, backoff_initial_s=30.0)
    with writer:
        for lap in range(12):
            writer.enqueue("laps", lap_row(lap))
        writer.flush()
    stats = writer.stats()
    assert stats.retry_depth == 5
    assert stats.dropped_by_table["laps"] == 7


def test_retained_rows_are_replayed_on_reconnect() -> None:
    conn = FakeConn()
    attempts = {"n": 0}

    def flaky(_url: str) -> FakeConn:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.OperationalError("connection refused")
        return conn

    cfg = Config(database_url="postgresql://fake/f126", db_batch_ms=5000)
    writer = DbWriter(cfg, connect=flaky, backoff_initial_s=0.01)
    with writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 1.0})
        for lap in range(3):
            writer.enqueue("laps", lap_row(lap))
        for i in range(20):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        writer.flush()
        assert writer.stats().connected is False

        deadline_reached = False
        for _ in range(200):
            if writer.flush() and writer.stats().retry_depth == 0:
                deadline_reached = True
                break
        assert deadline_reached, "writer never reconnected"

    stats = writer.stats()
    assert stats.written_total["laps"] == 3
    assert stats.written_total["sessions"] == 1
    assert stats.dropped_by_table["telemetry_samples"] == 20  # samples were not retained
    assert stats.reconnects == 1
    assert len(conn.rows_for("laps")) == 3


def test_backoff_doubles_and_caps() -> None:
    cfg = Config(database_url="postgresql://fake/f126", db_batch_ms=5000)
    writer = DbWriter(cfg, connect=_dead_connect, backoff_initial_s=1.0, backoff_max_s=60.0)
    seen = []
    for _ in range(12):
        writer._ensure_connection()  # noqa: SLF001 - exercising the backoff schedule directly
        seen.append(writer._backoff)  # noqa: SLF001
        writer._next_attempt = 0.0  # noqa: SLF001 - skip the wait, keep the schedule
    assert seen[:4] == [2.0, 4.0, 8.0, 16.0]
    assert seen[-1] == 60.0
    assert writer.stats().connected is False


def test_one_bad_row_does_not_poison_the_batch() -> None:
    conn = FakeConn()
    conn.poison = -999.0
    writer, _conn = make_writer(conn)
    with writer:
        writer.enqueue("telemetry_samples", telemetry_row(0))
        writer.enqueue("telemetry_samples", telemetry_row(1) | {"speed_kmh": -999.0})
        writer.enqueue("telemetry_samples", telemetry_row(2))
        writer.flush()
        stats = writer.stats()  # inside the block: stop() intentionally reports disconnected

    assert conn.rollbacks >= 1
    assert stats.dropped_by_table["telemetry_samples"] == 1
    assert stats.written_total["telemetry_samples"] == 2
    assert stats.connected is True  # a data error is not a connection error


def test_stats_snapshot_is_json_ready() -> None:
    writer, _conn = make_writer()
    with writer:
        writer.enqueue("laps", lap_row(1))
        writer.flush()
        payload = writer.stats().as_dict()
    assert json.loads(json.dumps(payload))["connected"] is True
    assert set(payload) >= {
        "connected",
        "queue_depth",
        "written_total",
        "dropped_total",
        "last_error",
    }


def test_stop_flushes_what_is_queued() -> None:
    writer, conn = make_writer()
    writer.start()
    for lap in range(5):
        writer.enqueue("laps", lap_row(lap))
    writer.stop()
    assert len(conn.rows_for("laps")) == 5
    assert conn.closed is True
    assert writer.stats().written_total["laps"] == 5


# --------------------------------------------------------------------------------------------
# Tier 2: integration (needs F126_TEST_DATABASE_URL)
# --------------------------------------------------------------------------------------------

TEST_DB_URL = os.environ.get("F126_TEST_DATABASE_URL", "")
integration = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="set F126_TEST_DATABASE_URL to run the store integration tier",
)


def _url_with_schema(url: str, schema: str) -> str:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params["options"] = f"-csearch_path={schema}"
    return urlunsplit(parts._replace(query=urlencode(params)))


@pytest.fixture
def scratch_url() -> Any:
    """A private Postgres schema per test, dropped afterwards."""
    schema = f"f126_t_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(TEST_DB_URL, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        yield _url_with_schema(TEST_DB_URL, schema)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _count(conn: Any, table: str, where: str = "") -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table} {where}").fetchone()
    return int(row["n"])


@integration
def test_apply_schema_is_idempotent(scratch_url: str) -> None:
    assert db.init_db(scratch_url) == db.SCHEMA_VERSION
    assert db.init_db(scratch_url) == db.SCHEMA_VERSION  # second boot must be a no-op
    with db.connect(scratch_url) as conn:
        assert db.schema_version(conn) == 1
        assert _count(conn, "schema_meta") == 1
        for table in TABLE_COLUMNS:
            assert _count(conn, table) == 0


@integration
def test_schema_version_is_none_before_apply(scratch_url: str) -> None:
    with db.connect(scratch_url) as conn:
        assert db.schema_version(conn) is None


@integration
def test_realistic_session_roundtrip(scratch_url: str) -> None:
    db.init_db(scratch_url)
    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    started = 1_700_000_000.0

    with DbWriter(cfg) as writer:
        writer.enqueue(
            "sessions",
            {
                "session_uid": UID,
                "segment": 0,
                "packet_format": 2026,
                "session_type": 10,
                "session_type_name": "Race",
                "track_id": 13,
                "track_name": "Spa-Francorchamps",
                "started_at_wall": started,
                "joined_in_progress": False,
                "player_car_index": 0,
                "total_laps": 44,
                "session_duration_s": 5400,
                "weather_json": [{"offset_min": 0, "weather": 1, "rain_percentage": 20}],
                "raw_file": "/data/raw/2026-08-06T12-00-00.f1raw",
            },
        )
        for car in range(20):
            writer.enqueue(
                "participants",
                {
                    "session_key_uid": UID,
                    "segment": 0,
                    "car_index": car,
                    "name": "PLAYER" if car == 0 else f"AI_{car:02d}",
                    "team_id": car % 10,
                    "race_number": car + 1,
                    "is_ai": car != 0,
                    "is_player": car == 0,
                },
            )
        for lap in range(1, 51):
            writer.enqueue("laps", lap_row(lap))
        for i in range(2000):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        for i in range(200):
            writer.enqueue(
                "wear_samples",
                {
                    "session_key_uid": UID,
                    "segment": 0,
                    "session_time_s": float(i),
                    "lap_number": 1 + i // 4,
                    "tyre_wear_pct": [i * 0.1, i * 0.1, i * 0.11, i * 0.11],
                    "damage_json": {"front_left_wing_pct": i % 5, "floor_pct": 0},
                },
            )
        for i in range(30):
            writer.enqueue(
                "events",
                {
                    "session_key_uid": UID,
                    "segment": 0,
                    "session_time_s": float(i * 60),
                    "frame_id": 1000 + i,
                    "wall_ts": started + i * 60,
                    "code": "FTLP" if i % 3 else "PENA",
                    "details_json": {"car_index": 0, "lap_time_ms": 91_500 + i},
                },
            )
        for stint in range(3):
            writer.enqueue(
                "tyre_stints",
                {
                    "session_key_uid": UID,
                    "segment": 0,
                    "car_index": 0,
                    "stint_no": stint,
                    "compound_actual": 16 + stint,
                    "compound_visual": 16 + stint,
                    "lap_start": 1 + stint * 17,
                    "lap_end": 17 + stint * 17,
                    "wear_at_end_json": {"rl": 41.0, "rr": 44.5, "fl": 38.0, "fr": 39.5},
                    "end_reason": "pit",
                },
            )
        writer.enqueue(
            "raw_captures",
            {
                "session_key_uid": UID,
                "segment": 0,
                "path": "/data/raw/2026-08-06T12-00-00.f1raw",
                "bytes": 91_000_000,
                "packets": 812_345,
                "first_wall": started,
                "last_wall": started + 3600,
                "packet_format": 2026,
            },
        )
        assert writer.flush(timeout=30.0) is True

        # --- history reconciliation: same generation corrects in place ---
        writer.enqueue(
            "laps",
            lap_row(7, lap_time_ms=91_111, s1_ms=27_000, s2_ms=33_000, s3_ms=31_111, valid=False),
        )
        # --- and a superseding generation lands as a new row ---
        writer.enqueue("laps", lap_row(7, generation=1, lap_time_ms=90_500))
        # --- re-emitting the session row must not create a second session ---
        writer.enqueue(
            "sessions",
            {
                "session_uid": UID,
                "segment": 0,
                "started_at_wall": started,
                "ended_at_wall": started + 3600,
                "ended_reason": "chequered_flag",
                "track_name": None,  # COALESCE must keep the value we already have
                "final_classification_json": {"rows": [{"car_index": 0, "position": 1}]},
            },
        )
        assert writer.flush(timeout=30.0) is True
        stats = writer.stats()

    assert stats.dropped_total == 0
    assert stats.written_total["telemetry_samples"] == 2000

    with db.connect(scratch_url) as conn:
        assert _count(conn, "sessions") == 1
        assert _count(conn, "participants") == 20
        assert _count(conn, "laps") == 51  # 50 laps + one superseding generation
        assert _count(conn, "telemetry_samples") == 2000
        assert _count(conn, "wear_samples") == 200
        assert _count(conn, "events") == 30
        assert _count(conn, "tyre_stints") == 3
        assert _count(conn, "raw_captures") == 1

        session = conn.execute("SELECT * FROM sessions").fetchone()
        session_id = session["id"]
        assert session["track_name"] == "Spa-Francorchamps"  # not nulled by the partial re-emit
        assert session["ended_reason"] == "chequered_flag"
        assert session["final_classification_json"]["rows"][0]["position"] == 1
        assert session["weather_json"][0]["rain_percentage"] == 20

        corrected = conn.execute(
            "SELECT * FROM laps WHERE lap_number = 7 AND generation = 0"
        ).fetchone()
        assert corrected["lap_time_ms"] == 91_111
        assert corrected["valid"] is False

        sample = conn.execute(
            "SELECT * FROM telemetry_samples ORDER BY frame_id LIMIT 1"
        ).fetchone()
        assert sample["session_id"] == session_id
        assert sample["tyre_pressure"] == pytest.approx([23.1, 23.2, 22.8, 22.9], rel=1e-4)
        assert sample["gear"] == 1

        wear = conn.execute("SELECT * FROM wear_samples ORDER BY session_time_s LIMIT 1").fetchone()
        assert wear["damage_json"] == {"front_left_wing_pct": 0, "floor_pct": 0}

        # --- queries.py ---
        listed = queries.list_sessions(conn, limit=10)
        assert len(listed) == 1
        assert listed[0]["id"] == session_id
        assert listed[0]["lap_count"] == 50  # distinct laps, not generations
        assert listed[0]["player_name"] == "PLAYER"

        detail = queries.session_detail(conn, session_id)
        assert detail is not None
        assert len(detail["participants"]) == 20
        assert detail["participants"][0]["is_player"] is True
        assert len(detail["tyre_stints"]) == 3
        assert detail["telemetry_sample_count"] == 2000
        assert detail["event_count"] == 30
        assert detail["best_lap_ms"] == 90_500  # newest generation of lap 7, valid
        assert queries.session_detail(conn, session_id + 999) is None

        laps = queries.laps_for_session(conn, session_id)
        assert len(laps) == 50
        lap7 = next(lap for lap in laps if lap["lap_number"] == 7)
        assert lap7["generation"] == 1 and lap7["lap_time_ms"] == 90_500
        assert len(queries.laps_for_session(conn, session_id, all_generations=True)) == 51
        assert len(queries.laps_for_session(conn, session_id, car_index=0)) == 50
        assert queries.laps_for_session(conn, session_id, car_index=5) == []

        trace = queries.telemetry_for_lap(conn, session_id, 1)
        assert len(trace) == 40
        assert trace == sorted(trace, key=lambda r: r["lap_distance_m"])
        assert len(queries.events_for_session(conn, session_id)) == 30
        assert json.dumps(listed[0], default=str)  # JSON-ready


@integration
def test_placeholder_session_is_adopted_by_the_real_session_row(scratch_url: str) -> None:
    db.init_db(scratch_url)
    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    with DbWriter(cfg) as writer:
        for i in range(10):  # telemetry beats the session packet
            writer.enqueue("telemetry_samples", telemetry_row(i))
        assert writer.flush(timeout=15.0) is True
        writer.enqueue(
            "sessions",
            {"session_uid": UID, "segment": 0, "started_at_wall": 42.0, "track_name": "Monza"},
        )
        assert writer.flush(timeout=15.0) is True

    with db.connect(scratch_url) as conn:
        assert _count(conn, "sessions") == 1
        session = conn.execute("SELECT * FROM sessions").fetchone()
        assert session["track_name"] == "Monza"
        assert session["started_at_wall"] == 42.0
        assert _count(conn, "telemetry_samples", f"WHERE session_id = {session['id']}") == 10


@integration
def test_segments_of_one_uid_are_separate_sessions(scratch_url: str) -> None:
    db.init_db(scratch_url)
    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    with DbWriter(cfg) as writer:
        for segment in (0, 1):
            writer.enqueue(
                "sessions",
                {"session_uid": UID, "segment": segment, "started_at_wall": 100.0 + segment},
            )
            writer.enqueue("laps", lap_row(1) | {"segment": segment})
        assert writer.flush(timeout=15.0) is True

    with db.connect(scratch_url) as conn:
        assert _count(conn, "sessions") == 2
        assert _count(conn, "laps") == 2
        rows = conn.execute(
            "SELECT s.segment, l.lap_number FROM laps l JOIN sessions s ON s.id = l.session_id"
            " ORDER BY s.segment"
        ).fetchall()
        assert [r["segment"] for r in rows] == [0, 1]


@integration
def test_durable_rows_survive_a_dead_database(scratch_url: str) -> None:
    db.init_db(scratch_url)
    attempts = {"n": 0}

    def flaky(url: str) -> Any:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise psycopg.OperationalError("simulated outage")
        return db.connect(url)

    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    writer = DbWriter(cfg, connect=flaky, backoff_initial_s=0.05)
    with writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 7.0})
        for lap in range(1, 6):
            writer.enqueue("laps", lap_row(lap))
        for i in range(50):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        assert writer.flush(timeout=5.0) is False  # first attempt lands during the outage

        for _ in range(400):
            if writer.flush(timeout=5.0) and writer.stats().retry_depth == 0:
                break
        assert writer.stats().connected is True

    with db.connect(scratch_url) as conn:
        assert _count(conn, "sessions") == 1
        assert _count(conn, "laps") == 5  # retained through the outage
        assert _count(conn, "telemetry_samples") == 0  # dropped, recoverable via backfill
    assert writer.stats().dropped_by_table["telemetry_samples"] == 50


@integration
def test_writer_recovers_from_a_killed_backend(scratch_url: str) -> None:
    """The harsh case: Postgres kills our connection mid-session, capture keeps running."""
    db.init_db(scratch_url)
    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    with DbWriter(cfg, backoff_initial_s=0.05) as writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 3.0})
        writer.enqueue("laps", lap_row(1))
        assert writer.flush(timeout=15.0) is True

        with psycopg.connect(TEST_DB_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = 'f126' AND pid <> pg_backend_pid()"
            )

        writer.enqueue("laps", lap_row(2))
        for i in range(20):
            writer.enqueue("telemetry_samples", telemetry_row(i))
        for _ in range(400):
            if writer.flush(timeout=5.0) and writer.stats().retry_depth == 0:
                break
        stats = writer.stats()
        assert stats.connected is True
        assert stats.reconnects >= 2

    with db.connect(scratch_url) as conn:
        assert _count(conn, "sessions") == 1  # cache survived, no duplicate session
        assert _count(conn, "laps") == 2  # lap 2 replayed out of the retry buffer


@integration
def test_cascade_delete_removes_children(scratch_url: str) -> None:
    db.init_db(scratch_url)
    cfg = Config(database_url=scratch_url, db_batch_ms=20)
    with DbWriter(cfg) as writer:
        writer.enqueue("sessions", {"session_uid": UID, "segment": 0, "started_at_wall": 1.0})
        writer.enqueue("laps", lap_row(1))
        writer.enqueue("telemetry_samples", telemetry_row(0))
        writer.enqueue(
            "raw_captures", {"session_key_uid": UID, "segment": 0, "path": "/data/raw/x.f1raw"}
        )
        assert writer.flush(timeout=15.0) is True

    with db.connect(scratch_url, autocommit=True) as conn:
        conn.execute("DELETE FROM sessions")
        assert _count(conn, "laps") == 0
        assert _count(conn, "telemetry_samples") == 0
        assert _count(conn, "raw_captures") == 1  # inventory survives, unlinked
        assert conn.execute("SELECT session_id FROM raw_captures").fetchone()["session_id"] is None


def test_schema_file_ships_with_the_package() -> None:
    assert Path(db.SCHEMA_PATH).is_file()

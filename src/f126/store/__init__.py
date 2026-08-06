"""Postgres store: derived index over the raw capture logs.

- ``db``      connection + idempotent schema application
- ``writer``  DbWriter, the batching writer thread the capture path enqueues into
- ``queries`` read-side helpers for the web API

Nothing here is on the critical path for capture: if Postgres is unavailable the writer
degrades (see writer.py) and the raw log keeps everything needed to backfill later.
"""

from __future__ import annotations

from f126.store.db import SCHEMA_VERSION, apply_schema, connect, init_db, schema_version
from f126.store.queries import (
    events_for_session,
    laps_for_session,
    list_sessions,
    session_detail,
    telemetry_for_lap,
)
from f126.store.writer import TABLE_COLUMNS, DbWriter, WriterStats

__all__ = [
    "SCHEMA_VERSION",
    "TABLE_COLUMNS",
    "DbWriter",
    "WriterStats",
    "apply_schema",
    "connect",
    "events_for_session",
    "init_db",
    "laps_for_session",
    "list_sessions",
    "schema_version",
    "session_detail",
    "telemetry_for_lap",
]

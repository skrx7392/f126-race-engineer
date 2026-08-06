"""Connection helpers and the boot-time migration runner.

There is one schema file (schema.sql) and it is idempotent, so "migration" means
"execute schema.sql, then record the version". When the schema grows a v2 the
upgrade path is: keep schema.sql creating the current shape, add the ALTERs guarded
by IF NOT EXISTS, bump SCHEMA_VERSION.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

Conn = psycopg.Connection[dict[str, Any]]


def schema_sql() -> str:
    """The DDL applied at boot. Read from disk each call (it is tiny and cached by the OS)."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


def connect(
    url: str,
    *,
    autocommit: bool = False,
    connect_timeout: int = 5,
    application_name: str = "f126",
) -> Conn:
    """Open one connection. Raises psycopg.OperationalError if Postgres is unreachable.

    Callers on the capture path must never let that escape — DbWriter owns the retry
    policy; everything else (tests, ops, backfill) is allowed to fail loudly.
    """
    if not url:
        raise ValueError("database url is empty (F126_DATABASE_URL unset: DB writes disabled)")
    return psycopg.connect(
        url,
        autocommit=autocommit,
        row_factory=dict_row,
        connect_timeout=connect_timeout,
        application_name=application_name,
    )


def schema_version(conn: Conn) -> int | None:
    """Current recorded schema version, or None if the schema has never been applied."""
    try:
        with conn.transaction():
            cur = conn.execute("SELECT max(version) AS version FROM schema_meta")
            row = cur.fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    if row is None:
        return None
    value = row["version"] if isinstance(row, dict) else row[0]
    return None if value is None else int(value)


def apply_schema(conn: Conn) -> int:
    """Run schema.sql and record SCHEMA_VERSION. Idempotent; safe on every boot.

    Returns the version now recorded in schema_meta.
    """
    sql = schema_sql()
    with conn.transaction():
        # No placeholders in schema.sql, so psycopg sends it as one multi-statement query.
        conn.execute(sql)
        conn.execute(
            "UPDATE schema_meta SET version = %s WHERE version < %s",
            (SCHEMA_VERSION, SCHEMA_VERSION),
        )
    version = schema_version(conn)
    if version != SCHEMA_VERSION:  # pragma: no cover - only reachable on a future downgrade
        log.warning("schema version is %s, expected %s", version, SCHEMA_VERSION)
    return version if version is not None else SCHEMA_VERSION


def init_db(url: str) -> int:
    """Connect, apply the schema, disconnect. Returns the recorded schema version."""
    with connect(url, autocommit=True) as conn:
        version = apply_schema(conn)
    log.info("schema applied, version=%s", version)
    return version


def main(argv: list[str] | None = None) -> int:
    """Entry point for an `f126-init-db` console script (wired at integration time)."""
    parser = argparse.ArgumentParser(
        prog="f126-init-db", description="create/upgrade the f126 schema"
    )
    parser.add_argument("url", nargs="?", help="postgres URL; defaults to F126_DATABASE_URL")
    args = parser.parse_args(argv)

    url = args.url
    if not url:
        from f126 import config

        url = config.load().database_url
    if not url:
        print("no database url: pass one or set F126_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        version = init_db(url)
    except (psycopg.Error, ValueError) as exc:
        print(f"init-db failed: {exc}", file=sys.stderr)
        return 1
    print(f"f126 schema version {version} applied")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

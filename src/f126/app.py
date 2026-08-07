"""Composition root: wires capture → raw log → parser → state → (DB, WebSocket).

Three entry modes share one pipeline:
- serve:    UDP listener + raw log + DB + web dashboard (production)
- replay:   .f1raw file replaces the listener; no raw re-logging (dev loop)
- backfill: re-parse raw captures into the database; no web, no live state
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from f126.capture.rawlog import RawLogWriter, raw_dir, recover_orphans
from f126.config import Config
from f126.parser import PacketParser
from f126.replay.replayer import replay
from f126.state import build_state
from f126.store import db as store_db
from f126.store.writer import DbWriter
from f126.udp.listener import make_queue, start_listener
from f126.web.app import create_app
from f126.web.app import run as web_run

log = logging.getLogger("f126")

QueueItem = tuple[bytes, int, int]


class _DictStats:
    """Adapt a dict-of-counters attribute to the web layer's StatsSource."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def stats(self) -> dict[str, float | int | str]:
        out: dict[str, float | int | str] = {}
        for key, value in self._fn().items():
            if isinstance(value, dict):
                for sub, v in value.items():
                    if isinstance(v, int | float):
                        out[f"{key}_{sub}"] = v
            elif isinstance(value, int | float | str):
                out[key] = value
        return out


def _parser_stats(parser: PacketParser) -> _DictStats:
    return _DictStats(lambda: dict(parser.counters))


def _rawlog_stats(rawlog: RawLogWriter) -> _DictStats:
    return _DictStats(
        lambda: {
            "bytes_written": rawlog.bytes_written,
            "packets_written": rawlog.packets_written,
            "compressed_bytes": rawlog.compressed_bytes,
        }
    )


def _writer_stats(writer: DbWriter) -> _DictStats:
    return _DictStats(lambda: writer.stats().as_dict())


async def _pump(
    queue: asyncio.Queue[QueueItem],
    parser: PacketParser,
    state: Any,
    rawlog: RawLogWriter | None,
) -> None:
    """The hot loop: raw-log first, parse second, feed state third.

    The raw-log write runs in a worker thread because its periodic fsync would
    otherwise stall the event loop (rawlog's own documented contract). Ordering
    is preserved: this task awaits each write before taking the next datagram.
    """
    while True:
        data, mono_ns, wall_ns = await queue.get()
        if rawlog is not None:
            await asyncio.to_thread(rawlog.write, data, mono_ns, wall_ns)
        pkt = parser.parse(data, mono_ns, wall_ns)
        if pkt is not None:
            state.feed(pkt)


async def _ticker(
    state: Any,
    parser: PacketParser,
    listener: Any | None,
    hub_getter: Any,
    interval_s: float = 1.0,
) -> None:
    """1 Hz housekeeping: lifecycle tick + health counters for the slow frame."""
    last_received = 0
    while True:
        await asyncio.sleep(interval_s)
        now_ns = time.monotonic_ns()
        state.tick(now_ns)
        if listener is not None:
            current, drops = listener.received, listener.dropped
        else:  # replay: no listener, count parsed packets instead
            current, drops = parser.counters.get("parsed_total", 0), 0
        errors = parser.counters.get("errors_total", 0)
        hub = hub_getter()
        state.live.update_health(
            packets_per_sec=current - last_received,
            parse_errors_total=errors,
            kernel_drops_total=drops,
            ws_clients=hub.client_count if hub is not None else 0,
        )
        last_received = current


def _make_writer(cfg: Config) -> DbWriter | None:
    if not cfg.database_url:
        log.warning("F126_DATABASE_URL empty — DB writes disabled, raw log only")
        return None
    try:
        store_db.init_db(cfg.database_url)
        log.info("database schema applied")
    except Exception:
        log.exception("schema apply failed — writer will retry in background")
    writer = DbWriter(cfg)
    writer.start()
    return writer


def _db_conn_factory(cfg: Config) -> Any | None:
    if not cfg.database_url:
        return None

    def factory() -> Any:
        return store_db.connect(cfg.database_url)

    return factory


# --- post-session debrief ---------------------------------------------------------------
#
# Generation happens here, off the capture path, in exactly two places: automatically when a
# session closes with reason "finished", and on demand from `f126 debrief`. There is
# deliberately no HTTP route that generates one — the dashboard's read-only invariant is
# worth more than the convenience of a button.


async def generate_debrief(cfg: Config, session_id: int, *, regenerate: bool = False) -> int | None:
    """Build the fact sheet, have it written up, and store the result. Returns the row id.

    Returns None when there was nothing to do: a debrief already exists and `regenerate` is
    false, or the session has no player laps to describe.

    Raises:
        LlmError: the endpoint failed or is not configured.
        AnalysisError: the session id is unknown.
        psycopg.Error: the database is unreachable.
    """
    from f126.analysis.factsheet import build_fact_sheet  # noqa: PLC0415
    from f126.llm import write_debrief  # noqa: PLC0415
    from f126.store import queries  # noqa: PLC0415
    from f126.store.writer import insert_debrief  # noqa: PLC0415

    def _read() -> dict[str, Any] | None:
        with store_db.connect(cfg.database_url) as conn:
            if not regenerate and queries.latest_debrief(conn, session_id) is not None:
                return None
            if queries.player_lap_count(conn, session_id) < 1:
                log.info("session %s has no player laps; skipping debrief", session_id)
                return None
            return build_fact_sheet(conn, session_id)

    fact_sheet = await asyncio.to_thread(_read)
    if fact_sheet is None:
        return None

    debrief = await write_debrief(cfg, fact_sheet)

    def _write() -> int | None:
        with store_db.connect(cfg.database_url) as conn:
            return insert_debrief(
                conn,
                session_id,
                created_at=time.time(),
                model=debrief.model,
                prompt_version=debrief.prompt_version,
                fact_sheet=fact_sheet,
                text=debrief.text,
            )

    row_id = await asyncio.to_thread(_write)
    log.info(
        "debrief written for session %s (row %s, model %s, %d words)",
        session_id,
        row_id,
        debrief.model,
        len(debrief.text.split()),
    )
    return row_id


def _debrief_on_close(
    cfg: Config, writer: DbWriter | None, tasks: set[asyncio.Task[Any]]
) -> Callable[[Any, str], None]:
    """The serve-path hook: a fire-and-forget debrief when a session finishes properly.

    Three properties matter more than the debrief itself, since this runs inside the state
    layer's close callback, which runs inside the packet pump:

    * it never blocks — all it does synchronously is create a task;
    * it never raises — the callback chain that ends a session must not be broken by an
      optional feature, so every failure is logged and swallowed;
    * it never runs on an unfinished session — only `reason == "finished"`, which means the
      game sent a FinalClassification packet. A session that timed out or was superseded is
      a fragment, and a debrief of a fragment is worse than none.

    `tasks` is the caller's strong-reference set. asyncio only holds weak references to
    tasks, so a task that nothing else refers to can be garbage collected mid-await.
    """

    def _on_close(key: Any, reason: str) -> None:
        if reason != "finished" or not cfg.llm_enabled or not cfg.database_url:
            return
        try:
            uid = key.uid_str
        except AttributeError:
            log.debug("debrief skipped: unrecognised session key %r", key)
            return
        try:
            task = asyncio.get_running_loop().create_task(
                _debrief_task(cfg, writer, uid), name=f"debrief:{uid}"
            )
        except RuntimeError:
            # No running loop: a replay driven from a synchronous test harness, or a close
            # fired during interpreter shutdown. Nothing to schedule onto.
            log.debug("debrief skipped for %s: no running event loop", uid)
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return _on_close


async def _debrief_task(cfg: Config, writer: DbWriter | None, session_uid: str) -> None:
    """Wait for the session's final rows to land, then generate. Swallows every failure."""
    try:
        if writer is not None:
            # The close callback runs before the writer has flushed the final classification
            # and the last laps, and a fact sheet built from a half-written session would be
            # confidently wrong. Flushing is the deterministic wait; the delay covers the
            # case where there is no writer to flush.
            await asyncio.to_thread(writer.flush, 30.0)
        if cfg.debrief_delay_s > 0:
            await asyncio.sleep(cfg.debrief_delay_s)

        from f126.store import queries  # noqa: PLC0415

        def _resolve() -> int | None:
            with store_db.connect(cfg.database_url) as conn:
                return queries.session_id_for_key(conn, session_uid)

        session_id = await asyncio.to_thread(_resolve)
        if session_id is None:
            log.info("debrief skipped: no session row for uid %s", session_uid)
            return
        await generate_debrief(cfg, session_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("post-session debrief failed for uid %s (continuing)", session_uid)


async def _serve_async(cfg: Config) -> int:
    salvaged = recover_orphans(cfg.data_dir)
    if salvaged:
        log.info("recovered %d orphaned raw capture(s)", len(salvaged))

    queue = make_queue()
    rawlog = RawLogWriter(cfg.data_dir)
    writer = _make_writer(cfg)
    parser = PacketParser()

    # Per-file capture bookkeeping: which session the current raw file records,
    # and the counter baselines to compute per-file byte/packet totals from the
    # writer's lifetime counters.
    capture: dict[str, Any] = {"key": None, "bytes0": 0, "packets0": 0, "opened_wall": time.time()}

    def _emit_capture_row(closed_path: str) -> None:
        key = capture["key"]
        if writer is None or key is None:
            return
        writer.enqueue(
            "raw_captures",
            {
                "session_key_uid": key[0],
                "segment": key[1],
                "path": closed_path,
                "bytes": rawlog.bytes_written - capture["bytes0"],
                "packets": rawlog.packets_written - capture["packets0"],
                "first_wall": capture["opened_wall"],
                "last_wall": time.time(),
                "packet_format": None,
            },
        )

    def _on_rotate(uid: int, seg: int) -> str:
        # rotate() returns the *finalized old* file; catalog it against the
        # session that just ended, then hand the *new* file's final name to the
        # state layer for `sessions.raw_file` (the contract build_state expects).
        closed = rawlog.rotate(uid, seg)
        _emit_capture_row(str(closed))
        capture.update(
            key=(str(uid), int(seg)),
            bytes0=rawlog.bytes_written,
            packets0=rawlog.packets_written,
            opened_wall=time.time(),
        )
        return str(rawlog.final_path)

    # Strong references to in-flight debrief tasks. asyncio holds only weak ones, so without
    # this a debrief can be collected mid-request.
    debrief_tasks: set[asyncio.Task[Any]] = set()
    if cfg.llm_enabled:
        log.info("post-session debriefs enabled (model %s)", cfg.llm_model)

    state = build_state(
        cfg,
        emit_row=writer.enqueue if writer is not None else None,
        on_rotate=_on_rotate,
        on_session_close=_debrief_on_close(cfg, writer, debrief_tasks),
    )

    listener = await start_listener(cfg, queue)
    app = create_app(
        cfg,
        live=state.live,
        stats_sources={
            "state": state,
            "listener": listener,
            "parser": _parser_stats(parser),
            "rawlog": _rawlog_stats(rawlog),
            "writer": _writer_stats(writer) if writer is not None else None,
        },
        db_conn_factory=_db_conn_factory(cfg),
    )
    log.info(
        "listening UDP %s:%d (rcvbuf %d), http %s:%d",
        cfg.udp_host,
        cfg.udp_port,
        listener.granted_rcvbuf,
        cfg.http_host,
        cfg.http_port,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    # listener stats() includes its own counters; expose via state health ticker
    stats_sources = app.state  # hub lives at app.state.ws_hub after startup

    tasks = [
        asyncio.create_task(_pump(queue, parser, state, rawlog), name="pump"),
        asyncio.create_task(
            _ticker(state, parser, listener, lambda: getattr(stats_sources, "ws_hub", None)),
            name="ticker",
        ),
        asyncio.create_task(web_run(cfg, app), name="web"),
    ]
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        done, _ = await asyncio.wait(
            [*tasks, stopper], return_when=asyncio.FIRST_COMPLETED
        )
        for t in done:
            if t is not stopper and t.exception() is not None:
                raise t.exception()  # noqa: RSE102 — re-raise task failure
    finally:
        # Each step guarded: one failing cleanup (e.g. ENOSPC in rawlog.close)
        # must never prevent the later ones — especially writer.stop, whose
        # daemon thread would otherwise die with rows still queued.
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Debriefs are cancelled rather than awaited: shutdown must not wait 60 s on an
        # LLM call, and the CLI can regenerate any debrief that was lost this way.
        for t in debrief_tasks:
            t.cancel()
        if debrief_tasks:
            await asyncio.gather(*debrief_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            listener.close()
        try:
            state.shutdown()
        except Exception:
            log.exception("state shutdown failed")
        try:
            final_path = rawlog.close()
            _emit_capture_row(str(final_path))
        except Exception:
            log.exception("raw log close failed")
        if writer is not None:
            try:
                writer.stop(timeout=10.0)
            except Exception:
                log.exception("db writer stop failed")
    return 0


async def _replay_async(cfg: Config, path: str, speed: str, loop_playback: bool) -> int:
    queue = make_queue()
    writer = _make_writer(cfg)
    parser = PacketParser()
    state = build_state(
        cfg,
        emit_row=writer.enqueue if writer is not None else None,
        on_rotate=None,  # replays are never re-logged
    )
    app = create_app(
        cfg,
        live=state.live,
        stats_sources={
            "state": state,
            "parser": _parser_stats(parser),
            "writer": _writer_stats(writer) if writer is not None else None,
        },
        db_conn_factory=_db_conn_factory(cfg),
    )

    pump = asyncio.create_task(_pump(queue, parser, state, None), name="pump")
    ticker = asyncio.create_task(
        _ticker(state, parser, None, lambda: getattr(app.state, "ws_hub", None)),
        name="ticker",
    )
    web = asyncio.create_task(web_run(cfg, app), name="web")
    log.info(
        "replaying %s at speed=%s — dashboard on http://localhost:%d", path, speed, cfg.http_port
    )
    try:
        feeder = asyncio.create_task(
            replay(path, queue, speed=speed, loop=loop_playback), name="feeder"
        )
        # Supervise: a dead pump would otherwise block the feeder forever on a
        # full queue with its traceback swallowed.
        done, _ = await asyncio.wait({feeder, pump}, return_when=asyncio.FIRST_COMPLETED)
        if pump in done and pump.exception() is not None:
            raise pump.exception()  # noqa: RSE102
        result = await feeder
        while not queue.empty() and not pump.done():
            await asyncio.sleep(0.05)
        log.info("replay done: %s packets", getattr(result, "packets_fed", "?"))
        # keep serving the dashboard until Ctrl-C so the session stays inspectable
        await web
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in (pump, ticker, web):
            t.cancel()
        await asyncio.gather(pump, ticker, web, return_exceptions=True)
        state.shutdown()
        if writer is not None:
            writer.stop(timeout=10.0)
    return 0


async def _backfill_async(cfg: Config, paths: list[str]) -> int:
    if not cfg.database_url:
        log.error("backfill requires F126_DATABASE_URL")
        return 1
    targets: list[Path] = []
    for p in paths or [str(raw_dir(cfg.data_dir))]:
        pp = Path(p)
        if pp.is_dir():
            targets.extend(sorted(pp.glob("*.f1raw")))
        elif pp.exists():
            targets.append(pp)
    if not targets:
        log.error("no .f1raw files found")
        return 1

    writer = _make_writer(cfg)
    assert writer is not None
    exit_code = 0
    for target in targets:
        parser = PacketParser()
        state = build_state(cfg, emit_row=writer.enqueue, on_rotate=None)
        if state.rows is not None:
            state.rows.set_raw_file(str(target))
        queue: asyncio.Queue[QueueItem] = make_queue(maxsize=65536)
        pump = asyncio.create_task(_pump(queue, parser, state, None))
        try:
            feeder = asyncio.create_task(replay(target, queue, speed="max"), name="feeder")
            done, _ = await asyncio.wait({feeder, pump}, return_when=asyncio.FIRST_COMPLETED)
            if pump in done and pump.exception() is not None:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
                raise pump.exception()  # noqa: RSE102
            result = await feeder
            while not queue.empty() and not pump.done():
                await asyncio.sleep(0.05)
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            state.shutdown()
        errors = parser.counters.get("errors_total", 0)
        log.info(
            "backfilled %s: %s packets, %d parse errors",
            target.name,
            getattr(result, "packets_fed", "?"),
            errors,
        )
        if errors:
            exit_code = 1
    if not writer.flush(timeout=30.0):
        log.warning("writer flush timed out; some rows may be dropped")
    writer.stop(timeout=30.0)
    return exit_code


def run_serve(cfg: Config) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(_serve_async(cfg))


def run_replay(cfg: Config, path: str, *, speed: str = "1", loop: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(_replay_async(cfg, path, speed, loop))


def run_backfill(cfg: Config, paths: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(_backfill_async(cfg, paths))


async def _debrief_async(cfg: Config, session_id: int, regenerate: bool) -> int:
    from f126.analysis.resample import AnalysisError  # noqa: PLC0415
    from f126.llm import LlmError  # noqa: PLC0415
    from f126.store import queries  # noqa: PLC0415

    if not cfg.database_url:
        log.error("debrief requires F126_DATABASE_URL")
        return 1
    if not cfg.llm_enabled:
        log.error("debrief requires F126_LLM_BASE_URL and F126_LLM_MODEL")
        return 2

    try:
        row_id = await generate_debrief(cfg, session_id, regenerate=regenerate)
    except AnalysisError as exc:
        log.error("session %s: %s", session_id, exc.detail)
        return 1
    except LlmError as exc:
        log.error("debrief generation failed: %s", exc)
        return 1

    def _read() -> Any:
        with store_db.connect(cfg.database_url) as conn:
            return queries.latest_debrief(conn, session_id)

    existing = await asyncio.to_thread(_read)
    if existing is None:
        log.error("no debrief for session %s and none could be generated", session_id)
        return 1
    if row_id is None:
        log.info(
            "session %s already has a debrief; pass --regenerate to write a new one", session_id
        )
    print(existing["text"])
    return 0


def run_debrief(cfg: Config, session_id: int, *, regenerate: bool = False) -> int:
    """`f126 debrief <session_id>` — generate (or print) one session's debrief.

    Idempotent by default: a session that already has a debrief prints the stored one rather
    than spending a request on a second opinion. `--regenerate` appends a new one.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(_debrief_async(cfg, session_id, regenerate))


def run_tag(
    cfg: Config,
    session_id: int | None,
    *,
    season: int | None = None,
    round_no: int | None = None,
    note: str | None = None,
    clear: bool = False,
    list_tags: bool = False,
) -> int:
    """`f126 tag` — pin, clear or list career season/round overrides.

    `career_tags` is the one hand-written table in the database and this is its only
    writer: the HTTP surface is read-only by project invariant, so the mutation path is
    the CLI, on the same DSN serve and backfill use. The tag keys on the game's
    `session_uid`, which is why it survives a backfill dropping and re-deriving every
    session row.

    Synchronous on purpose — one connection, one statement, nothing to schedule.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from f126.store import queries  # noqa: PLC0415
    from f126.store.writer import delete_career_tag, upsert_career_tag  # noqa: PLC0415

    if not cfg.database_url:
        log.error("tag requires F126_DATABASE_URL")
        return 1

    if list_tags:
        if session_id is not None or clear or season is not None:
            log.error("--list takes no session id and no other options")
            return 2
        with store_db.connect(cfg.database_url) as conn:
            rows = queries.career_tags(conn)
            for row in rows:
                resolved = queries.session_id_for_key(conn, str(row["session_uid"]))
                line = (
                    f"session {resolved if resolved is not None else '?'}"
                    f"  uid {row['session_uid']}"
                    f"  season {row['season']}"
                    f"  round {row['round'] if row['round'] is not None else '-'}"
                )
                if row.get("note"):
                    line += f"  {row['note']}"
                print(line)
        if not rows:
            print("no career tags")
        return 0

    if session_id is None:
        log.error("tag needs a session id (or --list)")
        return 2
    if clear and (season is not None or round_no is not None or note is not None):
        log.error("--clear replaces the tag with nothing; it takes no --season/--round/--note")
        return 2
    if not clear and season is None:
        log.error("tag needs --season (or --clear to remove the tag)")
        return 2
    if (season is not None and season < 1) or (round_no is not None and round_no < 1):
        log.error("season and round are 1-based")
        return 2

    with store_db.connect(cfg.database_url) as conn:
        summary = queries.session_summary(conn, session_id)
        if summary is None:
            log.error("session %s not found", session_id)
            return 1
        uid = str(summary["session_uid"])
        if clear:
            if delete_career_tag(conn, uid):
                print(f"cleared tag for session {session_id} (uid {uid})")
            else:
                print(f"session {session_id} (uid {uid}) had no tag")
            return 0
        upsert_career_tag(
            conn,
            uid,
            season=season,
            round_no=round_no,
            note=note,
            updated_at=time.time(),
        )
        pinned = f"season {season}" if round_no is None else f"season {season}, round {round_no}"
        print(f"tagged session {session_id} (uid {uid}): {pinned}")
    return 0

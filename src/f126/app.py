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
    """The hot loop: raw-log first, parse second, feed state third."""
    while True:
        data, mono_ns, wall_ns = await queue.get()
        if rawlog is not None:
            rawlog.write(data, mono_ns, wall_ns)
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
        received = listener.received if listener is not None else last_received
        errors = parser.counters.get("errors_total", 0)
        drops = listener.dropped if listener is not None else 0
        hub = hub_getter()
        state.live.update_health(
            packets_per_sec=received - last_received,
            parse_errors_total=errors,
            kernel_drops_total=drops,
            ws_clients=hub.client_count if hub is not None else 0,
        )
        last_received = received


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


async def _serve_async(cfg: Config) -> int:
    salvaged = recover_orphans(cfg.data_dir)
    if salvaged:
        log.info("recovered %d orphaned raw capture(s)", len(salvaged))

    queue = make_queue()
    rawlog = RawLogWriter(cfg.data_dir)
    writer = _make_writer(cfg)
    parser = PacketParser()
    state = build_state(
        cfg,
        emit_row=writer.enqueue if writer is not None else None,
        on_rotate=lambda uid, seg: str(rawlog.rotate(uid, seg)),
    )

    app = create_app(
        cfg,
        live=state.live,
        stats_sources={
            "state": state.live,
            "parser": _parser_stats(parser),
            "rawlog": _rawlog_stats(rawlog),
            "writer": _writer_stats(writer) if writer is not None else None,
        },
        db_conn_factory=_db_conn_factory(cfg),
    )
    listener = await start_listener(cfg, queue)
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
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        listener.close()
        state.shutdown()
        rawlog.close()
        if writer is not None:
            writer.stop(timeout=10.0)
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
            "state": state.live,
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
        result = await replay(path, queue, speed=speed, loop=loop_playback)
        while not queue.empty():
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
            result = await replay(target, queue, speed="max")
            while not queue.empty():
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

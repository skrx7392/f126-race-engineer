"""Replay a .f1raw capture into the live ingest queue.

WHY REPLAY EXISTS: the raw log is the source of truth, so every downstream stage (parser,
state machine, DB writer, WebSocket broadcaster) must be exercisable without a PS5 in the
room. Replay puts exactly the same tuples on exactly the same queue the UDP listener
feeds, so no consumer needs a "am I live or replaying?" branch.

TIMESTAMP CHOICE (deliberate, and the reason consumers can stay ignorant):
queue items are ``(data, recv_monotonic_ns, recv_wall_ns)`` carrying the **original
recorded stamps**, not the moment of re-feeding. Consumers derive lap times, sample rates,
and stall detection from these stamps; handing them replay-time stamps would smear every
derived time by the age of the capture and make a 2x replay produce lap times that are
half as long. The re-feed clock is still tracked — it is exposed on
:class:`ReplayProgress` as ``fed_monotonic_ns``/``fed_wall_ns`` for anything that
genuinely needs wall-clock progress (progress bars, throughput metering) — but it never
enters the queue.

The consequence to know about: with ``loop=True`` the stamps repeat from the top on each
pass, i.e. they jump backwards. A consumer that assumes monotonic stamps forever should
treat a backwards jump the same way it treats one from the game (flashbacks do this too).

PACING: gaps come from the recorded *monotonic* deltas (wall stamps can jump under NTP).
``speed="max"`` never sleeps. A numeric speed divides every gap by that factor, scheduled
against an absolute timeline so scheduling latency does not accumulate as drift.

BACKPRESSURE: replay uses ``await queue.put(...)``, i.e. it blocks rather than drops. A
replay that outran a slow consumer would silently produce different results from the live
path, and unlike the wire, a file will wait.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from f126.capture.rawlog import RawLogReader

log = logging.getLogger(__name__)

#: Packets between progress callbacks, unless overridden.
DEFAULT_PROGRESS_EVERY = 500

#: Sleeps shorter than this are not worth a loop yield; gaps below it are coalesced.
MIN_SLEEP_S = 0.0005

Speed = float | str
ProgressHook = Callable[["ReplayProgress"], None]


@dataclass(slots=True, frozen=True)
class ReplayProgress:
    """Snapshot handed to the progress hook. ``pct`` is None only if the file is empty."""

    path: Path
    packets_fed: int
    bytes_fed: int
    compressed_pos: int
    compressed_size: int
    pct: float | None
    elapsed_s: float
    loop_index: int
    #: Feed-time stamps — deliberately NOT what goes on the queue (see module docstring).
    fed_monotonic_ns: int
    fed_wall_ns: int
    finished: bool = False


@dataclass(slots=True, frozen=True)
class ReplayResult:
    """Returned when replay finishes (never returned when ``loop=True``)."""

    path: Path
    packets_fed: int
    bytes_fed: int
    loops: int
    elapsed_s: float
    truncated: bool


def parse_speed(speed: Speed) -> float | None:
    """Normalize a speed argument. Returns None for 'max' (no sleeping).

    Accepts the raw CLI string (``--speed 2``, ``--speed max``) as well as a float.
    """
    if isinstance(speed, str):
        token = speed.strip().lower()
        if token in {"max", "inf", "infinite", "none"}:
            return None
        try:
            value = float(token)
        except ValueError as exc:
            raise ValueError(f"invalid speed {speed!r}: expected a number or 'max'") from exc
    else:
        value = float(speed)
    if value != value:  # NaN
        raise ValueError("invalid speed: NaN")
    if value == float("inf"):
        return None
    if value <= 0:
        raise ValueError(f"invalid speed {speed!r}: must be > 0 (or 'max')")
    return value


async def replay(
    path: str | os.PathLike[str],
    queue: asyncio.Queue[tuple[bytes, int, int]],
    speed: Speed = 1,
    loop: bool = False,
    *,
    progress: ProgressHook | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    max_gap_s: float | None = None,
) -> ReplayResult:
    """Feed ``path`` into ``queue`` as ``(data, recv_monotonic_ns, recv_wall_ns)`` tuples.

    Args:
        path: a .f1raw file (finalized or still ``.open``).
        queue: the same bounded queue :func:`f126.udp.start_listener` feeds.
        speed: playback multiplier (``2`` = twice as fast), or ``"max"`` for no sleeping.
        loop: restart from the top forever. Cancel the task to stop it.
        progress: called every ``progress_every`` packets and once at the end of each pass.
        progress_every: packet interval between progress callbacks.
        max_gap_s: clamp for recorded gaps; ``None`` reproduces pauses faithfully. Useful
            for skipping the dead air between a session ending and the next one starting.

    Returns:
        A :class:`ReplayResult`. With ``loop=True`` this only returns via cancellation.

    Raises:
        f126.capture.rawlog.RawLogError: the file is not a readable capture.
        ValueError: bad ``speed``.
    """
    file_path = Path(path)
    factor = parse_speed(speed)
    if progress_every <= 0:
        raise ValueError("progress_every must be > 0")
    clamp_ns = int(max_gap_s * 1e9) if max_gap_s is not None else None

    started = time.monotonic()
    packets_fed = 0
    bytes_fed = 0
    loops = 0
    truncated = False

    while True:
        loops += 1
        pass_packets = 0
        # Wall-clock origin of this pass, anchored on the first packet so that reader
        # start-up cost is not silently owed back by the first few sleeps.
        origin = 0.0
        # Virtual timeline: recorded ns consumed so far this pass, after clamping.
        virtual_ns = 0

        with RawLogReader(file_path) as reader:
            prev_mono_ns: int | None = None
            for data, mono_ns, wall_ns in reader:
                if factor is not None:
                    if prev_mono_ns is None:
                        origin = time.monotonic()
                    else:
                        gap_ns = mono_ns - prev_mono_ns
                        if gap_ns < 0:
                            gap_ns = 0  # capture spliced across a monotonic reset
                        if clamp_ns is not None and gap_ns > clamp_ns:
                            gap_ns = clamp_ns
                        virtual_ns += gap_ns
                        delay = origin + (virtual_ns / 1e9) / factor - time.monotonic()
                        if delay > MIN_SLEEP_S:
                            await asyncio.sleep(delay)
                prev_mono_ns = mono_ns

                # ORIGINAL stamps go on the queue — see the module docstring.
                await queue.put((data, mono_ns, wall_ns))

                packets_fed += 1
                pass_packets += 1
                bytes_fed += len(data)

                if factor is None and pass_packets % 1024 == 0:
                    # Full-speed replay must still let consumers run.
                    await asyncio.sleep(0)

                if progress is not None and pass_packets % progress_every == 0:
                    progress(
                        _snapshot(
                            file_path,
                            reader,
                            packets_fed,
                            bytes_fed,
                            started,
                            loops,
                            finished=False,
                        )
                    )

            truncated = reader.truncated
            if truncated:
                log.warning(
                    "%s: truncated tail after %d packets (recovered capture?)",
                    file_path,
                    pass_packets,
                )
            if progress is not None:
                progress(
                    _snapshot(
                        file_path, reader, packets_fed, bytes_fed, started, loops, finished=True
                    )
                )

        if not loop:
            break
        if pass_packets == 0:
            raise ValueError(f"{file_path}: contains no records; refusing to loop forever")

    return ReplayResult(
        path=file_path,
        packets_fed=packets_fed,
        bytes_fed=bytes_fed,
        loops=loops,
        elapsed_s=time.monotonic() - started,
        truncated=truncated,
    )


def _snapshot(
    file_path: Path,
    reader: RawLogReader,
    packets_fed: int,
    bytes_fed: int,
    started: float,
    loop_index: int,
    *,
    finished: bool,
) -> ReplayProgress:
    size = reader.size
    pos = reader.compressed_pos if not finished else size
    pct = (100.0 * pos / size) if size else None
    return ReplayProgress(
        path=file_path,
        packets_fed=packets_fed,
        bytes_fed=bytes_fed,
        compressed_pos=pos,
        compressed_size=size,
        pct=pct,
        elapsed_s=time.monotonic() - started,
        loop_index=loop_index,
        fed_monotonic_ns=time.monotonic_ns(),
        fed_wall_ns=time.time_ns(),
        finished=finished,
    )

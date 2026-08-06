"""Tests for .f1raw replay.

The load-bearing property is that replay is *indistinguishable* from live capture to
every consumer: same queue, same tuple shape, same (recorded) timestamps, same order.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from f126.capture.rawlog import RawLogWriter
from f126.config import Config
from f126.replay.replayer import ReplayProgress, parse_speed, replay
from f126.udp.listener import make_queue


def make_capture(
    tmp_path: Path, n: int = 50, *, gap_ms: float = 10.0, size: int = 128
) -> tuple[Path, list[tuple[bytes, int, int]]]:
    """A finalized capture whose records are ``gap_ms`` apart on the monotonic clock."""
    records = []
    gap_ns = int(gap_ms * 1_000_000)
    for i in range(n):
        payload = i.to_bytes(4, "little") + bytes([i % 251]) * (size - 4)
        mono = 5_000_000_000 + i * gap_ns
        records.append((payload, mono, 1_700_000_000_000_000_000 + i * gap_ns))
    writer = RawLogWriter(tmp_path, session_uid="replaytest", segment=0)
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    return writer.close(), records


async def drain(queue: asyncio.Queue) -> list[tuple[bytes, int, int]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# ---- speed parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1.0), (2.0, 2.0), ("1", 1.0), ("2.5", 2.5), ("max", None), ("MAX", None), (" max ", None)],
)
def test_parse_speed(value: float | str, expected: float | None) -> None:
    assert parse_speed(value) == expected


@pytest.mark.parametrize("value", [0, -1, "0", "-2", "fast", ""])
def test_parse_speed_rejects_nonsense(value: float | str) -> None:
    with pytest.raises(ValueError):
        parse_speed(value)


# ---- fidelity -------------------------------------------------------------


async def test_replay_max_preserves_bytes_stamps_and_order(tmp_path: Path) -> None:
    path, records = make_capture(tmp_path, n=300)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1024)

    started = time.monotonic()
    consumer_task = asyncio.create_task(_collect(queue, len(records)))
    result = await replay(path, queue, speed="max")
    got = await consumer_task
    elapsed = time.monotonic() - started

    assert got == records  # exact bytes, exact ORIGINAL stamps, exact order
    assert result.packets_fed == len(records)
    assert result.bytes_fed == sum(len(d) for d, _, _ in records)
    assert result.loops == 1
    assert result.truncated is False
    assert elapsed < 2.0  # 'max' must not honour the 3 seconds of recorded gaps


async def _collect(queue: asyncio.Queue, n: int) -> list[tuple[bytes, int, int]]:
    out = []
    for _ in range(n):
        out.append(await queue.get())
    return out


async def test_replay_queue_shape_matches_listener(tmp_path: Path) -> None:
    """A consumer written for the live listener must not need a replay branch."""
    path, records = make_capture(tmp_path, n=5)
    queue = make_queue(maxsize=64)
    await replay(path, queue, speed="max")
    items = await drain(queue)
    assert len(items) == 5
    for item, (data, mono, wall) in zip(items, records, strict=True):
        assert isinstance(item, tuple) and len(item) == 3
        payload, got_mono, got_wall = item
        assert isinstance(payload, bytes)
        assert isinstance(got_mono, int) and isinstance(got_wall, int)
        assert (payload, got_mono, got_wall) == (data, mono, wall)


async def test_replay_stamps_are_recorded_not_feed_time(tmp_path: Path) -> None:
    """Explicit guard on the documented choice: no re-stamping into the queue."""
    path, records = make_capture(tmp_path, n=10)
    queue = make_queue(maxsize=64)
    now_mono = time.monotonic_ns()
    await replay(path, queue, speed="max")
    items = await drain(queue)
    for _, mono, wall in items:
        assert mono < now_mono  # recorded stamps are from the past, not "now"
        assert wall == records[[m for _, m, _ in records].index(mono)][2]


async def test_replay_of_empty_capture(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)
    path = writer.close()
    queue = make_queue(maxsize=8)
    result = await replay(path, queue, speed="max")
    assert result.packets_fed == 0
    assert queue.empty()


async def test_replay_respects_backpressure(tmp_path: Path) -> None:
    """A tiny queue must block replay, not drop — unlike the live listener."""
    path, records = make_capture(tmp_path, n=100)
    queue = make_queue(maxsize=2)
    task = asyncio.create_task(replay(path, queue, speed="max"))
    await asyncio.sleep(0.05)
    assert queue.qsize() == 2  # blocked, nothing lost
    got = await _collect(queue, len(records))
    result = await task
    assert got == records
    assert result.packets_fed == len(records)


# ---- pacing ---------------------------------------------------------------


async def test_speed_two_sleeps_about_half_the_recorded_gaps(tmp_path: Path) -> None:
    path, _ = make_capture(tmp_path, n=21, gap_ms=20.0)  # 400 ms of recorded time
    queue = make_queue(maxsize=64)

    started = time.monotonic()
    await replay(path, queue, speed=2.0)
    elapsed = time.monotonic() - started

    # Expect ~0.20 s. Tolerant bounds: clearly slower than 'max', clearly faster than 1x.
    assert 0.10 < elapsed < 0.45, elapsed
    assert queue.qsize() == 21


async def _delays_under_virtual_clock(
    path: Path, speed: float, monkeypatch: pytest.MonkeyPatch
) -> list[float]:
    """Run a replay against a virtual clock and return every delay it asked for.

    Both ``asyncio`` and ``time`` are shimmed *inside the replayer module only*, so the
    scheduler sees a clock that advances by exactly the amount it sleeps. That removes
    machine-speed noise and makes the pacing assertions exact rather than tolerant.
    """
    import types

    import f126.replay.replayer as mod

    now = 1000.0
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay
        await asyncio.sleep(0)  # still yield, so consumers can run

    monkeypatch.setattr(mod, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(
        mod,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: now,
            monotonic_ns=lambda: int(now * 1e9),
            time_ns=lambda: int(now * 1e9),
        ),
    )
    await replay(path, make_queue(maxsize=64), speed=speed)
    return delays


@pytest.mark.parametrize(
    ("speed", "expected_total"), [(1.0, 0.5), (2.0, 0.25), (0.5, 1.0), (10.0, 0.05)]
)
async def test_requested_delays_scale_inversely_with_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, speed: float, expected_total: float
) -> None:
    """Precise version of the timing test: inspect the sleeps against a virtual clock."""
    path, _ = make_capture(tmp_path, n=11, gap_ms=50.0)  # 10 gaps of 50 ms = 0.5 s
    delays = await _delays_under_virtual_clock(path, speed, monkeypatch)
    assert sum(delays) == pytest.approx(expected_total, rel=1e-6)
    assert len(delays) == 10
    assert all(d == pytest.approx(0.05 / speed, rel=1e-6) for d in delays)


async def test_max_gap_clamps_long_pauses(tmp_path: Path) -> None:
    """A capture with a 60 s hole must not make replay sit there for a minute."""
    records = [
        (b"a", 1_000_000_000, 1),
        (b"b", 61_000_000_000, 2),  # 60 s later
        (b"c", 61_010_000_000, 3),
    ]
    writer = RawLogWriter(tmp_path)
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    path = writer.close()

    queue = make_queue(maxsize=8)
    started = time.monotonic()
    await replay(path, queue, speed=1.0, max_gap_s=0.05)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, elapsed
    assert [item[1] for item in await drain(queue)] == [r[1] for r in records]


# ---- looping and progress -------------------------------------------------


async def test_loop_repeats_the_capture(tmp_path: Path) -> None:
    path, records = make_capture(tmp_path, n=10)
    queue = make_queue(maxsize=1024)
    task = asyncio.create_task(replay(path, queue, speed="max", loop=True))

    got = await asyncio.wait_for(_collect(queue, len(records) * 3), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert got[: len(records)] == records
    assert got[len(records) : len(records) * 2] == records
    assert got[len(records) * 2 :] == records


async def test_loop_refuses_an_empty_capture(tmp_path: Path) -> None:
    """Looping a file with no records would be a busy-spin; fail loudly instead."""
    writer = RawLogWriter(tmp_path)
    path = writer.close()
    queue = make_queue(maxsize=8)
    with pytest.raises(ValueError, match="no records"):
        await asyncio.wait_for(replay(path, queue, speed="max", loop=True), timeout=5.0)


async def test_progress_hook_reports_monotonically_to_100_pct(tmp_path: Path) -> None:
    path, records = make_capture(tmp_path, n=250)
    queue = make_queue(maxsize=1024)
    seen: list[ReplayProgress] = []

    consumer = asyncio.create_task(_collect(queue, len(records)))
    await replay(path, queue, speed="max", progress=seen.append, progress_every=50)
    await consumer

    assert len(seen) >= 2
    assert [p.packets_fed for p in seen] == sorted(p.packets_fed for p in seen)
    assert seen[-1].finished is True
    assert seen[-1].packets_fed == len(records)
    assert seen[-1].pct == pytest.approx(100.0)
    assert all(p.pct is not None and 0.0 <= p.pct <= 100.0 for p in seen)
    assert seen[-1].compressed_size == path.stat().st_size
    assert seen[-1].bytes_fed == sum(len(d) for d, _, _ in records)
    assert seen[-1].fed_monotonic_ns > records[-1][1]  # feed clock, not recorded clock


async def test_replay_reads_an_unfinalized_open_file(tmp_path: Path) -> None:
    """Replaying a capture that is still being recorded is a supported workflow."""
    writer = RawLogWriter(tmp_path, session_uid="live", segment=0)
    records = [(bytes([i]) * 32, 1000 + i, 2000 + i) for i in range(30)]
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    writer.flush()

    queue = make_queue(maxsize=64)
    result = await replay(writer.path, queue, speed="max")
    assert result.packets_fed == 30
    assert await drain(queue) == records
    writer.close()


async def test_bad_speed_rejected_before_touching_the_file(tmp_path: Path) -> None:
    queue = make_queue(maxsize=8)
    with pytest.raises(ValueError):
        await replay(tmp_path / "does-not-exist.f1raw", queue, speed="quick")


# ---- end to end -----------------------------------------------------------


async def test_live_capture_crash_recovery_replay_round_trip(tmp_path: Path) -> None:
    """The whole doctrine in one test.

    Real datagrams over a real socket -> raw log -> simulated crash -> boot recovery ->
    replay -> byte-identical stream back on the queue with the original arrival stamps.
    """
    import socket

    from f126.capture.rawlog import recover_orphans
    from f126.udp.listener import start_listener

    ingest = make_queue(maxsize=1024)
    listener = await start_listener(
        Config(udp_host="127.0.0.1", udp_port=0, rcvbuf_bytes=1 << 20), ingest
    )
    payloads = [i.to_bytes(2, "little") + bytes([i % 251]) * 200 for i in range(120)]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for payload in payloads:
                sock.sendto(payload, ("127.0.0.1", listener.port))
        finally:
            sock.close()

        deadline = time.monotonic() + 3.0
        while listener.received < len(payloads) and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert listener.received == len(payloads)
        assert listener.dropped == 0

        writer = RawLogWriter(tmp_path, session_uid="e2e", segment=0)
        captured = []
        while not ingest.empty():
            data, mono, wall = ingest.get_nowait()
            captured.append((data, mono, wall))
            writer.write(data, mono, wall)
        writer.flush()
    finally:
        listener.close()

    # Crash: no close(), so the file is still .open on the next boot.
    writer._stream = None  # noqa: SLF001
    writer._fh.close()  # noqa: SLF001
    writer._fh = None  # noqa: SLF001

    recovered = recover_orphans(tmp_path)
    assert len(recovered) == 1

    out = make_queue(maxsize=1024)
    result = await replay(recovered[0], out, speed="max")
    assert result.packets_fed == len(payloads)
    assert await drain(out) == captured
    assert [data for data, _, _ in captured] == payloads

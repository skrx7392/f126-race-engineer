"""Tests for the asyncio UDP listener.

Everything binds port 0 on loopback, so these run anywhere and never collide.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from f126.config import Config
from f126.udp.listener import (
    DEFAULT_QUEUE_MAXSIZE,
    Listener,
    TelemetryProtocol,
    make_queue,
    start_listener,
)


def cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "udp_host": "127.0.0.1",
        "udp_port": 0,
        "rcvbuf_bytes": 1 << 20,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


async def start(queue: asyncio.Queue, **overrides: object) -> Listener:
    return await start_listener(cfg(**overrides), queue)


def send(listener: Listener, *payloads: bytes) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for payload in payloads:
            sock.sendto(payload, ("127.0.0.1", listener.port))
    finally:
        sock.close()


async def wait_for(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ---- queue helper ---------------------------------------------------------


def test_make_queue_is_bounded() -> None:
    assert make_queue().maxsize == DEFAULT_QUEUE_MAXSIZE == 4096
    assert make_queue(16).maxsize == 16
    with pytest.raises(ValueError, match="bounded"):
        make_queue(0)
    with pytest.raises(ValueError, match="bounded"):
        make_queue(-1)


async def test_unbounded_queue_is_refused() -> None:
    """An unbounded queue trades a drop counter for an OOM; that is not a trade we take."""
    with pytest.raises(ValueError, match="bounded"):
        await start(asyncio.Queue())


# ---- binding --------------------------------------------------------------


async def test_binds_ephemeral_port_and_reports_it() -> None:
    listener = await start(make_queue(64))
    try:
        assert listener.port != 0
        assert listener.host == "127.0.0.1"
    finally:
        listener.close()


async def test_rcvbuf_is_requested_before_bind_and_granted_size_exposed() -> None:
    listener = await start(make_queue(64), rcvbuf_bytes=1 << 20)
    try:
        assert listener.requested_rcvbuf == 1 << 20
        assert listener.granted_rcvbuf > 0
        # Kernels clamp and (on Linux) double-count; only sanity is assertable.
        assert listener.granted_rcvbuf >= 8192
    finally:
        listener.close()


async def test_absurd_rcvbuf_request_degrades_instead_of_failing() -> None:
    """A tunable we cannot raise must not be the reason capture does not start."""
    listener = await start(make_queue(64), rcvbuf_bytes=1 << 40)
    try:
        assert listener.granted_rcvbuf > 0
    finally:
        listener.close()


async def test_close_is_idempotent() -> None:
    listener = await start(make_queue(64))
    listener.close()
    listener.close()


# ---- receive path ---------------------------------------------------------


async def test_datagrams_arrive_with_stamps(monkeypatch) -> None:
    queue = make_queue(64)
    listener = await start(queue)
    try:
        before_mono, before_wall = time.monotonic_ns(), time.time_ns()
        payloads = [b"", b"\x01\x02\x03", bytes(range(256)) * 4]
        send(listener, *payloads)
        assert await wait_for(lambda: queue.qsize() == len(payloads))
        after_mono, after_wall = time.monotonic_ns(), time.time_ns()

        items = [queue.get_nowait() for _ in range(len(payloads))]
    finally:
        listener.close()

    assert [data for data, _, _ in items] == payloads
    monos = [m for _, m, _ in items]
    walls = [w for _, _, w in items]
    assert monos == sorted(monos)  # stamps are non-decreasing in arrival order
    assert all(before_mono <= m <= after_mono for m in monos)
    assert all(before_wall <= w <= after_wall for w in walls)
    assert listener.received == len(payloads)
    assert listener.dropped == 0
    assert listener.bytes_received == sum(len(p) for p in payloads)


async def test_stats_snapshot() -> None:
    queue = make_queue(64)
    listener = await start(queue)
    try:
        send(listener, b"abc")
        assert await wait_for(lambda: listener.received == 1)
        stats = listener.stats()
        assert stats["received"] == 1
        assert stats["dropped"] == 0
        assert stats["bytes_received"] == 3
        assert stats["port"] == listener.port
        assert stats["queue_depth"] == 1
        assert stats["granted_rcvbuf"] == listener.granted_rcvbuf
    finally:
        listener.close()


async def test_full_queue_drops_and_counts() -> None:
    """maxsize=2, five datagrams: two queued, the rest counted as drops. Never raises."""
    queue = make_queue(2)
    listener = await start(queue)
    try:
        payloads = [bytes([i]) * 8 for i in range(5)]
        send(listener, *payloads)
        assert await wait_for(lambda: listener.received + listener.dropped == 5)

        assert queue.qsize() == 2
        assert listener.received == 2
        assert listener.dropped == 3
        # The two that made it are the first two, in order.
        assert [queue.get_nowait()[0] for _ in range(2)] == payloads[:2]

        # Draining the queue lets capture resume without a restart.
        send(listener, b"resumed")
        assert await wait_for(lambda: listener.received == 3)
        assert queue.get_nowait()[0] == b"resumed"
        assert listener.dropped == 3
    finally:
        listener.close()


async def test_sustained_burst_is_not_dropped_with_a_deep_queue() -> None:
    queue = make_queue(4096)
    listener = await start(queue)
    try:
        payloads = [i.to_bytes(2, "little") + b"\x00" * 1200 for i in range(500)]
        send(listener, *payloads)
        got = await wait_for(lambda: listener.received + listener.dropped >= 500, timeout=3.0)
        assert got
        assert listener.dropped == 0
        received = [queue.get_nowait()[0] for _ in range(queue.qsize())]
        assert received == payloads[: len(received)]  # ordering preserved
    finally:
        listener.close()


# ---- protocol unit level --------------------------------------------------


def test_protocol_receive_path_never_raises_on_full_queue() -> None:
    """Unit-level guard: datagram_received must swallow QueueFull, always."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    proto = TelemetryProtocol(queue)
    proto.datagram_received(b"one", ("127.0.0.1", 1))
    proto.datagram_received(b"two", ("127.0.0.1", 1))
    proto.datagram_received(b"three", ("127.0.0.1", 1))
    assert proto.received == 1
    assert proto.dropped == 2
    assert proto.bytes_received == 3
    assert queue.get_nowait()[0] == b"one"


def test_protocol_error_received_is_counted_not_raised() -> None:
    proto = TelemetryProtocol(asyncio.Queue(maxsize=4))
    proto.error_received(ConnectionRefusedError("icmp port unreachable"))
    proto.connection_lost(None)
    assert proto.errors == 1


def test_protocol_stamps_are_taken_before_enqueue() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    proto = TelemetryProtocol(queue)
    t0 = time.monotonic_ns()
    proto.datagram_received(b"x", ("127.0.0.1", 1))
    t1 = time.monotonic_ns()
    _, mono, wall = queue.get_nowait()
    assert t0 <= mono <= t1
    assert wall > 1_700_000_000_000_000_000

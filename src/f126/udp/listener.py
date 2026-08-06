"""asyncio UDP listener — the only code that touches the wire.

The PS5 fires up to ~1200 datagrams/second and never retransmits, so the receive path is
optimised for one thing: getting off the event loop's callback as fast as possible.
:meth:`TelemetryProtocol.datagram_received` therefore does exactly three things — take a
monotonic stamp, take a wall stamp, and ``put_nowait`` the triple onto a bounded queue.
No parsing, no logging, no allocation beyond the tuple. Everything else (raw-log write,
parse, state) happens in consumer tasks that can fall behind without stalling the socket.

Backpressure policy: the queue is **bounded** and a full queue **drops the datagram** and
increments :attr:`Listener.dropped`. Blocking is not an option (it would stall the event
loop and therefore the socket drain), and neither is unbounded growth (a stalled consumer
would eat all memory during a 2-hour race). A non-zero ``dropped`` count means a consumer
is too slow — it is a health signal the app surfaces, not something to paper over.

The kernel receive buffer is raised to ``cfg.rcvbuf_bytes`` *before* bind, which is the
real defence against burst loss; the granted size is read back with ``getsockopt`` and
exposed as :attr:`Listener.granted_rcvbuf` because kernels silently clamp the request
(Linux halves it against ``net.core.rmem_max``, and reports double what it granted).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field

from f126.config import Config

log = logging.getLogger(__name__)

#: Default depth of the ingest queue: ~4 seconds of headroom at full 1200 Hz rate.
DEFAULT_QUEUE_MAXSIZE = 4096

#: Queue item shape, shared with the replayer: (payload, recv_monotonic_ns, recv_wall_ns).
QueueItem = tuple[bytes, int, int]

#: SO_RCVBUF is a C int; requests wider than this cannot even be expressed to the kernel.
_MAX_SOCKOPT_INT = 2**31 - 1


def make_queue(maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> asyncio.Queue[QueueItem]:
    """Create the bounded ingest queue the listener and replayer both feed."""
    if maxsize <= 0:
        raise ValueError("ingest queue must be bounded (maxsize > 0)")
    return asyncio.Queue(maxsize=maxsize)


class TelemetryProtocol(asyncio.DatagramProtocol):
    """Datagram protocol whose receive path is stamp-and-enqueue, nothing else."""

    __slots__ = ("_put", "_queue", "bytes_received", "dropped", "errors", "received")

    def __init__(self, queue: asyncio.Queue[QueueItem]) -> None:
        self._queue = queue
        # Bound method lookup hoisted out of the hot path.
        self._put = queue.put_nowait
        self.received: int = 0
        self.dropped: int = 0
        self.bytes_received: int = 0
        self.errors: int = 0

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        mono_ns = time.monotonic_ns()
        wall_ns = time.time_ns()
        try:
            self._put((data, mono_ns, wall_ns))
        except asyncio.QueueFull:
            self.dropped += 1
            return
        self.received += 1
        self.bytes_received += len(data)

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable and friends. Never fatal for a receive-only socket.
        self.errors += 1
        log.debug("udp error_received: %r", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            log.warning("udp socket closed with error: %r", exc)


@dataclass(slots=True)
class Listener:
    """Handle for a bound, running UDP listener."""

    transport: asyncio.DatagramTransport
    protocol: TelemetryProtocol
    queue: asyncio.Queue[QueueItem] = field(repr=False)
    host: str
    port: int
    requested_rcvbuf: int
    granted_rcvbuf: int

    @property
    def received(self) -> int:
        """Datagrams successfully handed to the queue."""
        return self.protocol.received

    @property
    def dropped(self) -> int:
        """Datagrams discarded because the ingest queue was full."""
        return self.protocol.dropped

    @property
    def bytes_received(self) -> int:
        return self.protocol.bytes_received

    @property
    def errors(self) -> int:
        return self.protocol.errors

    def stats(self) -> dict[str, int | str]:
        return {
            "host": self.host,
            "port": self.port,
            "received": self.received,
            "dropped": self.dropped,
            "bytes_received": self.bytes_received,
            "errors": self.errors,
            "queue_depth": self.queue.qsize(),
            "requested_rcvbuf": self.requested_rcvbuf,
            "granted_rcvbuf": self.granted_rcvbuf,
        }

    def close(self) -> None:
        """Close the socket. Idempotent; already-queued items stay queued."""
        if not self.transport.is_closing():
            self.transport.close()


def _bind_socket(host: str, port: int, rcvbuf_bytes: int) -> tuple[socket.socket, int]:
    """Create + bind the UDP socket with SO_RCVBUF applied *before* bind."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_RCVBUF is a C int: anything wider is rejected with TypeError, not OSError.
        requested = min(int(rcvbuf_bytes), _MAX_SOCKOPT_INT)
        while requested > 0:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested)
                break
            except (OSError, OverflowError, TypeError, ValueError) as exc:
                # macOS refuses outright above kern.ipc.maxsockbuf; halve and retry
                # rather than failing capture over a tunable we may not control.
                log.warning("SO_RCVBUF=%d rejected (%s); retrying at half", requested, exc)
                requested //= 2
        sock.setblocking(False)
        sock.bind((host, port))
        granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except BaseException:
        sock.close()
        raise
    return sock, granted


async def start_listener(cfg: Config, queue: asyncio.Queue[QueueItem]) -> Listener:
    """Bind ``cfg.udp_host:cfg.udp_port`` and start feeding ``queue``.

    ``queue`` must be bounded (see :func:`make_queue`) — an unbounded queue turns a slow
    consumer into an OOM instead of a drop counter.
    """
    if queue.maxsize <= 0:
        raise ValueError(
            "ingest queue must be bounded; use f126.udp.make_queue() "
            "(an unbounded queue trades packet drops for an out-of-memory kill)"
        )

    sock, granted = _bind_socket(cfg.udp_host, cfg.udp_port, cfg.rcvbuf_bytes)
    bound_host, bound_port = sock.getsockname()[:2]

    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: TelemetryProtocol(queue), sock=sock
        )
    except BaseException:
        sock.close()
        raise

    if granted < cfg.rcvbuf_bytes:
        log.warning(
            "kernel granted SO_RCVBUF=%d of the requested %d; raise net.core.rmem_max "
            "(Linux) or kern.ipc.maxsockbuf (macOS) to avoid burst loss",
            granted,
            cfg.rcvbuf_bytes,
        )
    log.info(
        "udp listener bound on %s:%d (rcvbuf granted %d, queue maxsize %d)",
        bound_host,
        bound_port,
        granted,
        queue.maxsize,
    )

    return Listener(
        transport=transport,
        protocol=protocol,
        queue=queue,
        host=str(bound_host),
        port=int(bound_port),
        requested_rcvbuf=cfg.rcvbuf_bytes,
        granted_rcvbuf=granted,
    )

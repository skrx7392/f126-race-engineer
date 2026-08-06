"""WebSocket hub: fan-out of `snapshot` / `fast` / `slow` / `event` frames.

One process, one hub, a handful of browser tabs. The contract on the wire is
`docs/ws-protocol.md`; this module is only responsible for getting those frames
to every client without letting any one client hurt the others.

Design rules, in priority order:

1. **A slow client must never slow the pipeline.** Every client owns a bounded
   queue and a sender task. The broadcast loops only ever do a non-blocking put,
   so a client on hotel wifi cannot back-pressure the state machine.
2. **Periodic frames are lossy by design.** A `fast` frame that is 300 ms stale
   is worthless — the newer one supersedes it. When a client's queue is full the
   OLDEST queued periodic frame is evicted to make room, so what the client
   eventually receives is the freshest data, not the oldest backlog.
3. **Events are never dropped.** `LAP`, `PENALTY`, `FLAG` and friends are the
   narrative of the session; silently losing one rewrites history. An event will
   evict a periodic frame to get queued. If a client is so far behind that its
   queue holds *nothing but* events, that client is disconnected instead — the
   pathological case, and we would rather lose the client than the story.
4. **`snapshot` is always frame #1.** It is queued before the client joins the
   registry, so no broadcast can slip in front of it, and it is not evictable.

Serialisation happens once per broadcast, not once per client: at 20 clients
× 10 Hz that is the difference between 200 and 10 `json.dumps` calls a second.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import enum
import itertools
import json
import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.websockets import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from f126.config import Config
    from f126.web.interfaces import Frame, LiveSource

log = logging.getLogger(__name__)

#: Bumped only for breaking changes to the frame contract (see the protocol doc).
PROTOCOL_VERSION = 1

#: Per-client queue depth. 64 frames is ~6 s of `fast` at 10 Hz: long enough to
#: ride out a GC pause or a tab that just came back from background, short
#: enough that a client which is truly gone is noticed within seconds.
CLIENT_QUEUE_SIZE = 64

#: Frame kinds that may be evicted when a queue fills up.
PERIODIC_KINDS = frozenset({"fast", "slow"})

#: Close codes.
CLOSE_TRY_LATER = 1013  # at capacity, or client too slow to keep up
CLOSE_GOING_AWAY = 1001  # hub shutting down
CLOSE_INTERNAL = 1011  # send failed

_MISSING = object()


class _Offer(enum.Enum):
    """Outcome of trying to enqueue a frame for one client."""

    QUEUED = "queued"
    DROPPED = "dropped"  # a periodic frame was sacrificed (this one or an older one)
    OVERFLOW = "overflow"  # an event could not be queued; the client has to go


@dataclass(frozen=True, slots=True)
class _Frame:
    """A frame serialised once and shared by every client's queue."""

    kind: str
    text: str

    @property
    def periodic(self) -> bool:
        return self.kind in PERIODIC_KINDS


class _FrameQueue(asyncio.Queue[_Frame]):
    """Bounded per-client queue with kind-aware overflow.

    `_init` is one of `asyncio.Queue`'s documented override points (it is how
    `PriorityQueue` and `LifoQueue` are built), which makes the underlying deque
    ours to evict from the middle — the one operation the plain Queue API cannot
    express and the whole reason this subclass exists.
    """

    dropped: int

    def _init(self, maxsize: int) -> None:
        self._queue: collections.deque[_Frame] = collections.deque()
        self.dropped = 0  # cumulative periodic frames lost by this client

    def offer(self, frame: _Frame) -> _Offer:
        """Enqueue `frame` without ever blocking. See module docstring for policy."""
        if not self.full():
            self.put_nowait(frame)
            return _Offer.QUEUED

        if self._evict_oldest_periodic():
            # Made room by throwing away stale telemetry: the newcomer gets in.
            self.put_nowait(frame)
            self.dropped += 1
            return _Offer.DROPPED

        if frame.periodic:
            # Nothing stale to evict (queue is snapshot + events) and this frame
            # is itself disposable, so drop it rather than the history.
            self.dropped += 1
            return _Offer.DROPPED

        return _Offer.OVERFLOW

    def _evict_oldest_periodic(self) -> bool:
        for index, queued in enumerate(self._queue):
            if queued.periodic:
                del self._queue[index]
                return True
        return False


@dataclass(eq=False, slots=True)
class _Client:
    """One connected browser tab."""

    id: int
    ws: WebSocket
    queue: _FrameQueue
    sender: asyncio.Task[None] | None = None
    doomed: bool = False  # kill already scheduled; do not schedule twice


@dataclass(slots=True)
class _Totals:
    """Counters that must survive the clients they describe."""

    sent: Counter[str] = field(default_factory=Counter)
    dropped: int = 0  # from clients that have since disconnected
    disconnects: int = 0
    rejected: int = 0  # connections refused at the capacity cap


class WsHub:
    """Owns every WS client and the three broadcast loops that feed them.

    Lifecycle: `await start()` on app startup, `await aclose()` on shutdown,
    `await handle(ws)` per connection (that call is the endpoint's body and
    returns only when the connection is over).
    """

    def __init__(
        self,
        cfg: Config,
        live: LiveSource,
        *,
        queue_size: int = CLIENT_QUEUE_SIZE,
    ) -> None:
        self._cfg = cfg
        self._live = live
        self._queue_size = queue_size
        self._clients: set[_Client] = set()
        self._loops: list[asyncio.Task[None]] = []  # the three broadcast pumps
        self._kills: set[asyncio.Task[None]] = set()  # in-flight forced closes
        self._totals = _Totals()
        self._ids = itertools.count(1)
        self._closing = False

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Spawn the fast, slow and event pumps. Idempotent."""
        if self._loops:
            return
        self._closing = False
        if self._cfg.ws_fast_hz > 0:
            self._loops.append(
                asyncio.create_task(
                    self._periodic_loop("fast", self._cfg.ws_fast_hz, self._live.get_fast),
                    name="ws-fast",
                )
            )
        if self._cfg.ws_slow_hz > 0:
            self._loops.append(
                asyncio.create_task(
                    self._periodic_loop("slow", self._cfg.ws_slow_hz, self._live.get_slow),
                    name="ws-slow",
                )
            )
        self._loops.append(asyncio.create_task(self._event_loop(), name="ws-events"))
        log.info(
            "ws hub started (fast %.1f Hz, slow %.1f Hz, max %d clients)",
            self._cfg.ws_fast_hz,
            self._cfg.ws_slow_hz,
            self._cfg.ws_max_clients,
        )

    async def aclose(self) -> None:
        """Stop the loops and hang up on everyone. Safe to call twice."""
        self._closing = True
        await _cancel([*self._loops, *self._kills])
        self._loops.clear()
        self._kills.clear()
        for client in list(self._clients):
            await self._close_ws(client, CLOSE_GOING_AWAY, "server shutting down")
        # Senders are cancelled by the per-connection handlers as they unwind;
        # cancel here too so aclose() is enough even if a handler is wedged.
        await _cancel([c.sender for c in list(self._clients) if c.sender is not None])

    # ---- connection handling -------------------------------------------

    @property
    def client_count(self) -> int:
        """Live client count — the state layer mirrors this into `slow.health`."""
        return len(self._clients)

    async def handle(self, ws: WebSocket) -> None:
        """Serve one connection start to finish. This is the `/ws` endpoint body."""
        await ws.accept()

        if self._closing:
            await ws.close(code=CLOSE_GOING_AWAY, reason="server shutting down")
            return

        if len(self._clients) >= max(1, self._cfg.ws_max_clients):
            # Accept-then-close so the client sees code 1013 rather than a bare
            # handshake failure, and knows to back off instead of hammering.
            self._totals.rejected += 1
            log.warning("ws client refused: at capacity (%d)", self._cfg.ws_max_clients)
            await ws.close(code=CLOSE_TRY_LATER, reason="too many clients")
            return

        client = _Client(id=next(self._ids), ws=ws, queue=_FrameQueue(self._queue_size))
        # Queued before registration: nothing can get in front of the snapshot.
        client.queue.offer(_Frame("snapshot", _encode(self._snapshot())))
        self._clients.add(client)
        client.sender = asyncio.create_task(self._sender(client), name=f"ws-send-{client.id}")
        log.info("ws client %d connected (%d total)", client.id, len(self._clients))

        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # The protocol is server->client only. Anything a client sends is
                # read and discarded: there is no command surface to reach.
        except WebSocketDisconnect:
            pass
        except (RuntimeError, ConnectionError, OSError) as exc:
            log.debug("ws client %d read ended: %s", client.id, exc)
        finally:
            await self._unregister(client)

    async def _unregister(self, client: _Client) -> None:
        self._clients.discard(client)
        self._totals.dropped += client.queue.dropped
        self._totals.disconnects += 1
        if client.sender is not None:
            await _cancel([client.sender])
        log.info("ws client %d disconnected (%d left)", client.id, len(self._clients))

    async def _sender(self, client: _Client) -> None:
        """Drain one client's queue onto its socket. One task per client."""
        try:
            while True:
                frame = await client.queue.get()
                await client.ws.send_text(frame.text)
                self._totals.sent[frame.kind] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # peer gone, or the transport gave up
            log.debug("ws client %d send failed: %s", client.id, exc)
            await self._close_ws(client, CLOSE_INTERNAL, "send failed")

    def _kill(self, client: _Client, code: int, reason: str) -> None:
        """Evict a client that cannot be served correctly (never from its own task)."""
        if client.doomed:
            return
        client.doomed = True
        # Off the registry immediately so the next broadcast skips it; the
        # handler's finally block does the accounting when its read loop ends.
        self._clients.discard(client)
        task = asyncio.create_task(
            self._close_ws(client, code, reason), name=f"ws-kill-{client.id}"
        )
        self._kills.add(task)  # hold a reference so the task is not GC'd mid-flight
        task.add_done_callback(self._kills.discard)

    async def _close_ws(self, client: _Client, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await client.ws.close(code=code, reason=reason)

    # ---- broadcasting ---------------------------------------------------

    def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        """Fan one frame out to every client. Non-blocking; never raises.

        `kind` is the frame's `type` discriminator: "fast", "slow" or "event"
        (the loops' vocabulary). Events are protected by the queue policy;
        clients that cannot even take an event are disconnected.
        """
        if not self._clients:
            return
        frame = _Frame(kind, _encode(payload, kind))
        doomed: list[_Client] = []
        for client in self._clients:
            if client.queue.offer(frame) is _Offer.OVERFLOW:
                doomed.append(client)
        for client in doomed:
            log.warning("ws client %d evicted: queue saturated with events", client.id)
            self._kill(client, CLOSE_TRY_LATER, "client too slow")

    async def _periodic_loop(
        self,
        kind: str,
        hz: float,
        getter: Callable[[], Frame | None],
    ) -> None:
        """Publish `getter()` at `hz`, skipping missing and unchanged frames.

        The tick is scheduled against absolute deadlines so the loop keeps its
        average rate instead of drifting by one scheduling delay per iteration.
        A frame whose `t` matches the previous one means the state layer has had
        no new telemetry, so there is nothing to say.

        The body is defensive because this task is the telemetry stream: if one
        malformed frame killed it, the dashboard would go quiet for the rest of
        the session with nothing but a traceback to show for it. A bad tick is
        logged and skipped; the loop keeps its schedule.
        """
        period = 1.0 / hz
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        last_t: Any = _MISSING
        while True:
            next_at += period
            delay = next_at - loop.time()
            if delay <= 0:  # fell behind (debugger, GC, overloaded box): resync
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(delay)
            try:
                payload = getter()
                if payload is None:
                    continue
                marker = payload.get("t", _MISSING)
                if marker is not _MISSING and marker == last_t:
                    continue
                last_t = marker
                self.publish(kind, payload)
            except Exception:
                log.exception("ws %s tick failed; skipping", kind)

    async def _event_loop(self) -> None:
        """Drain the state layer's event queue straight onto every client."""
        queue = self._live.events
        while True:
            payload = await queue.get()
            try:
                self.publish("event", payload)
            except Exception:
                log.exception("ws event fan-out failed; dropping one event")
            finally:
                with contextlib.suppress(ValueError):
                    queue.task_done()

    def _snapshot(self) -> Frame:
        """Snapshot payload, degraded rather than fatal if the state layer trips."""
        try:
            payload = self._live.get_snapshot()
        except Exception:
            log.exception("get_snapshot() failed; serving an empty snapshot")
            payload = {}
        # Fill in the envelope without touching the state layer's own dict; what
        # it does supply wins, since it is the authority on its own frames.
        return {"type": "snapshot", "protocol_version": PROTOCOL_VERSION, **payload}

    # ---- observability ---------------------------------------------------

    def stats(self) -> dict[str, float | int | str]:
        """Counters for `/metrics` (rendered as `f126_ws_*`)."""
        sent = self._totals.sent
        live_dropped = sum(c.queue.dropped for c in self._clients)
        return {
            "clients": len(self._clients),
            "max_clients": self._cfg.ws_max_clients,
            "sent_total": sum(sent.values()),
            "sent_snapshot_total": sent["snapshot"],
            "sent_fast_total": sent["fast"],
            "sent_slow_total": sent["slow"],
            "sent_event_total": sent["event"],
            "dropped_frames_total": self._totals.dropped + live_dropped,
            "disconnects_total": self._totals.disconnects,
            "rejected_total": self._totals.rejected,
        }


async def _cancel(tasks: Iterable[asyncio.Task[None] | None]) -> None:
    """Cancel tasks and wait for them, swallowing the cancellation."""
    pending = [t for t in tasks if t is not None and not t.done()]
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _encode(payload: Mapping[str, Any], kind: str | None = None) -> str:
    """Serialise one frame, compactly and safely.

    `allow_nan=False` is the point: Python happily emits bare `NaN`/`Infinity`,
    which `JSON.parse` rejects — one stray non-finite float would break every
    frame for every client with no clue why. The fallback pass scrubs those to
    null and stringifies anything else json cannot take, so a single odd field
    costs one degraded frame rather than the whole stream. It runs only on the
    frames that need it.
    """
    if kind is not None:
        payload = _with_type(payload, kind)
    try:
        return json.dumps(payload, separators=(",", ":"), allow_nan=False)
    except ValueError, TypeError:
        log.warning("unserialisable value in %s frame; degrading it", kind or "snapshot")
        return json.dumps(_scrub(payload), separators=(",", ":"), allow_nan=False, default=str)


def _with_type(payload: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    """Guarantee the `type` discriminator without mutating the state layer's dict."""
    if payload.get("type") == kind:
        return payload
    return {"type": kind, **payload}


def _scrub(value: Any) -> Any:
    """Recursively replace non-finite floats with None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value

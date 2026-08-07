"""State layer: session lifecycle, live view, and DB row assembly.

The pipeline owns one `StateBundle`. Feed it every `ParsedPacket` and tick it at
about 1 Hz; everything else (WS payloads, DB rows, raw-log rotation) falls out of
that:

    bundle = build_state(cfg, emit_row=writer.emit, on_rotate=raw_logger.rotate)
    bundle.feed(pkt)                      # per packet
    bundle.tick(time.monotonic_ns())      # ~1 Hz
    bundle.live.get_fast()                # 10 Hz broadcaster
    bundle.live.get_slow()                # 1 Hz broadcaster
    await bundle.live.events.get()        # event frames

Ordering inside `feed()` matters and is deliberate: the tracker runs first so
lifecycle callbacks (open/segment/generation) land before the packet that caused
them is applied to the live view or written out. The one thing that has to
precede the tracker is the packet *header* reaching the row builder, because the
tracker's lap ledger publishes completed laps through `on_lap()` from inside
`tracker.feed()`, and `on_lap()` cannot attribute a lap to the player without
knowing which car index that is.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from f126.config import Config
from f126.state.live import PROTOCOL_VERSION, CompletedLap, LapTrace, LiveState, resolve_track_name
from f126.state.rows import EmitRow, NearestDecimator, RowBuilder
from f126.state.session import (
    CloseReason,
    LapLedger,
    LapRecord,
    SessionCallbacks,
    SessionContext,
    SessionKey,
    SessionKind,
    SessionTracker,
    TrackerStats,
    session_kind,
    session_type_name,
)
from f126.types import ParsedPacket

log = logging.getLogger(__name__)

__all__ = [
    "PROTOCOL_VERSION",
    "CloseReason",
    "CompletedLap",
    "EmitRow",
    "LapLedger",
    "LapRecord",
    "LapTrace",
    "LiveState",
    "NearestDecimator",
    "RowBuilder",
    "SessionCallbacks",
    "SessionContext",
    "SessionKey",
    "SessionKind",
    "SessionTracker",
    "StateBundle",
    "TrackerStats",
    "build_state",
    "resolve_track_name",
    "session_kind",
    "session_type_name",
]


@dataclass(slots=True)
class StateBundle:
    """Tracker + live view + row builder, wired together."""

    tracker: SessionTracker
    live: LiveState
    rows: RowBuilder | None = None

    def feed(self, pkt: ParsedPacket) -> bool:
        """Returns False when the reorder guard dropped the packet."""
        if self.rows is not None:
            # Header first, dispatch second. `tracker.feed()` publishes completed laps
            # through `on_lap()` from inside itself, and `on_lap()` has to know which car
            # is the player — which only the header says. Reading it after the tracker has
            # run left the row builder a packet behind its own callbacks.
            self.rows.observe_header(pkt.header)
        if not self.tracker.feed(pkt):
            return False
        self.live.update(pkt)
        if self.rows is not None:
            self.rows.feed(pkt)
        return True

    def stats(self) -> dict[str, float | int | str]:
        """Merged counters from every part of the state layer, for `/metrics`."""
        merged: dict[str, float | int | str] = dict(self.tracker.stats())
        merged.update(self.live.stats())
        if self.rows is not None:
            merged.update(self.rows.stats())
        return merged

    def tick(self, now_monotonic_ns: int) -> None:
        self.tracker.tick(now_monotonic_ns)

    def shutdown(self) -> None:
        self.tracker.shutdown()


def build_state(
    cfg: Config,
    *,
    emit_row: EmitRow | None = None,
    on_rotate: Callable[[int, int], Any] | None = None,
    events: Any = None,
    on_session_close: Callable[[SessionKey, CloseReason], None] | None = None,
) -> StateBundle:
    """Construct the state layer with all callbacks fanned out.

    `on_rotate` may return the path of the file it opened; when it does, that
    path is threaded into the `sessions` row as `raw_file`.

    `on_session_close` is an extra observer, invoked last — after the live view
    and the row builder have both handled the close, so anything it reads from
    the database sees the rows that close produced. It is for work that hangs off
    a session ending without being part of ending it (the post-session debrief);
    it must not raise, and a raise is logged and swallowed here rather than
    breaking the lifecycle for an optional extra.
    """
    live = LiveState(cfg, events=events)
    rows = RowBuilder(cfg, emit_row) if emit_row is not None else None

    def _fan_open(key: SessionKey, joined_in_progress: bool) -> None:
        live.on_session_open(key, joined_in_progress)
        if rows is not None:
            rows.on_session_open(key, joined_in_progress)

    def _fan_close(key: SessionKey, reason: CloseReason) -> None:
        live.on_session_close(key, reason)
        if rows is not None:
            rows.on_session_close(key, reason)
        if on_session_close is not None:
            try:
                on_session_close(key, reason)
            except Exception:
                log.exception("session-close observer failed for %s (continuing)", key)

    def _fan_segment(key: SessionKey, segment: int) -> None:
        live.on_segment(key, segment)
        if rows is not None:
            rows.on_segment(key, segment)

    def _fan_generation(generation: int, rewind_to_session_time: float) -> None:
        live.on_generation(generation, rewind_to_session_time)
        if rows is not None:
            rows.on_generation(generation, rewind_to_session_time)

    def _fan_rotate(session_uid: int, segment: int) -> None:
        if on_rotate is None:
            return
        path = on_rotate(session_uid, segment)
        if rows is not None and isinstance(path, str):
            rows.set_raw_file(path)

    def _fan_lap(key: SessionKey, record: LapRecord, corrected: bool) -> None:
        if rows is not None:
            rows.on_lap(key, record, corrected)

    callbacks = SessionCallbacks(
        on_session_open=_fan_open,
        on_session_close=_fan_close,
        on_segment=_fan_segment,
        on_generation=_fan_generation,
        on_rotate=_fan_rotate,
        on_stall=live.on_stall,
        on_lap=_fan_lap,
    )
    return StateBundle(tracker=SessionTracker(cfg, callbacks), live=live, rows=rows)

"""Session lifecycle: the identity model and everything that perturbs it.

Identity is a three-level tuple:

* **session** — one `session_uid` from the game. A new uid always means a new
  session; the previous one is closed (`finished` when a FinalClassification
  packet was seen for it, `superseded` otherwise).
* **segment** — a restart *within* the same uid (the game keeps the uid when you
  restart a practice/quali session, but `session_time` rewinds to ~0). Also used
  when a timed-out session starts producing packets again.
* **generation** — a flashback. Same session, same segment, but the clock went
  backwards and everything recorded after the rewind point is now counterfactual.

`SessionTracker.feed()` is driven by the pipeline for every parsed packet;
`SessionTracker.tick()` is called ~1 Hz so silence can be noticed even when no
packets arrive at all. Everything the tracker learns is published through
constructor-injected callbacks — the tracker never imports its consumers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from f126.config import Config
from f126.types import (
    ClassificationView,
    HistoryView,
    LapView,
    PacketId,
    ParsedPacket,
    SessionView,
)

CloseReason = Literal["finished", "superseded", "timeout", "shutdown"]
SessionKind = Literal["practice", "quali", "race", "time_trial", "other"]

# Session-time rewinds smaller than this are packet jitter, not a flashback.
_TIME_EPS_S = 0.05
# First packet we ever see past this session_time means we joined mid-session.
_JOIN_IN_PROGRESS_S = 60.0

# F1 24/25/26 session-type numbering (sprint shootout inserted at 10..14, race at
# 15..17). If the parser grows an authoritative table, prefer it there.
_SESSION_TYPES: dict[int, tuple[str, SessionKind]] = {
    0: ("Unknown", "other"),
    1: ("Practice 1", "practice"),
    2: ("Practice 2", "practice"),
    3: ("Practice 3", "practice"),
    4: ("Short Practice", "practice"),
    5: ("Qualifying 1", "quali"),
    6: ("Qualifying 2", "quali"),
    7: ("Qualifying 3", "quali"),
    8: ("Short Qualifying", "quali"),
    9: ("One-Shot Qualifying", "quali"),
    10: ("Sprint Shootout 1", "quali"),
    11: ("Sprint Shootout 2", "quali"),
    12: ("Sprint Shootout 3", "quali"),
    13: ("Short Sprint Shootout", "quali"),
    14: ("One-Shot Sprint Shootout", "quali"),
    15: ("Race", "race"),
    16: ("Race 2", "race"),
    17: ("Race 3", "race"),
    18: ("Time Trial", "time_trial"),
}


def session_type_name(session_type: int) -> str:
    """Human-readable name for a raw session-type enum value."""
    entry = _SESSION_TYPES.get(session_type)
    return entry[0] if entry else f"session_{session_type}"


def session_kind(session_type: int) -> SessionKind:
    """Coarse session family used for delta references and payload shaping."""
    entry = _SESSION_TYPES.get(session_type)
    return entry[1] if entry else "other"


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Primary key of a session segment. `session_uid` is the raw u64."""

    session_uid: int
    segment: int

    @property
    def uid_str(self) -> str:
        """u64 stringified — Postgres and JSON both need this."""
        return str(self.session_uid)

    def __str__(self) -> str:
        return f"{self.session_uid}#{self.segment}"


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Immutable snapshot of where the tracker currently is."""

    key: SessionKey | None
    generation: int
    joined_in_progress: bool
    stalled: bool


@dataclass(slots=True)
class LapRecord:
    """A completed lap for one car.

    `source` is `"history"` once SessionHistory has confirmed it; history always
    wins over the live LapData-derived value.
    """

    car_index: int
    lap_number: int
    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    s3_ms: int
    valid: bool
    source: str = "lap"

    def same_times(self, other: LapRecord) -> bool:
        return (
            self.lap_time_ms == other.lap_time_ms
            and self.s1_ms == other.s1_ms
            and self.s2_ms == other.s2_ms
            and self.s3_ms == other.s3_ms
            and self.valid == other.valid
        )


@dataclass(slots=True)
class _CarProgress:
    lap_number: int = 0
    s1_ms: int = 0
    s2_ms: int = 0
    invalid: bool = False


def _sector3(lap_time_ms: int, s1_ms: int, s2_ms: int) -> int:
    if lap_time_ms > 0 and s1_ms > 0 and s2_ms > 0 and lap_time_ms > s1_ms + s2_ms:
        return lap_time_ms - s1_ms - s2_ms
    return 0


class LapLedger:
    """Per-car completed-lap book with SessionHistory reconciliation.

    LapData drives the live view but its sector times are only as good as the
    packet we happened to catch at the line; SessionHistory is authoritative and
    arrives a beat later. When history disagrees with a lap we already published
    the corrected record is re-emitted with ``corrected=True`` so the DB writer
    can upsert over the earlier row.
    """

    __slots__ = ("_progress", "_records", "_resync", "on_lap")

    def __init__(self, on_lap: Callable[[LapRecord, bool], None] | None = None) -> None:
        self.on_lap = on_lap
        self._progress: dict[int, _CarProgress] = {}
        self._records: dict[tuple[int, int], LapRecord] = {}
        self._resync = False

    def reset(self) -> None:
        """Drop everything (new session or new segment)."""
        self._progress.clear()
        self._records.clear()
        self._resync = False

    def resync(self) -> None:
        """Re-seed per-car progress from the next LapData without emitting laps.

        Used after a flashback, where lap numbers, sector times and the invalid
        flag all jump backwards and a naive diff would fabricate a lap.
        """
        self._resync = True

    def record(self, car_index: int, lap_number: int) -> LapRecord | None:
        return self._records.get((car_index, lap_number))

    def records_for(self, car_index: int) -> list[LapRecord]:
        return [r for (car, _), r in sorted(self._records.items()) if car == car_index]

    def apply_lap_view(self, view: LapView) -> list[LapRecord]:
        if self._resync:
            self._resync = False
            for car in view.cars:
                self._progress[car.car_index] = _CarProgress(
                    car.lap_number, car.sector1_ms, car.sector2_ms, car.lap_invalid
                )
            return []

        emitted: list[LapRecord] = []
        for car in view.cars:
            progress = self._progress.get(car.car_index)
            if progress is None:
                self._progress[car.car_index] = _CarProgress(
                    car.lap_number, car.sector1_ms, car.sector2_ms, car.lap_invalid
                )
                continue
            if car.lap_number > progress.lap_number:
                if progress.lap_number > 0 and car.last_lap_ms > 0:
                    record = LapRecord(
                        car_index=car.car_index,
                        lap_number=progress.lap_number,
                        lap_time_ms=car.last_lap_ms,
                        s1_ms=progress.s1_ms,
                        s2_ms=progress.s2_ms,
                        s3_ms=_sector3(car.last_lap_ms, progress.s1_ms, progress.s2_ms),
                        valid=not progress.invalid,
                        source="lap",
                    )
                    if self._store(record):
                        emitted.append(record)
                progress.lap_number = car.lap_number
                progress.s1_ms = car.sector1_ms
                progress.s2_ms = car.sector2_ms
                progress.invalid = car.lap_invalid
            elif car.lap_number < progress.lap_number:
                # Rewound onto an earlier lap: re-seed, never synthesise a lap.
                progress.lap_number = car.lap_number
                progress.s1_ms = car.sector1_ms
                progress.s2_ms = car.sector2_ms
                progress.invalid = car.lap_invalid
            else:
                if car.sector1_ms > 0:
                    progress.s1_ms = car.sector1_ms
                if car.sector2_ms > 0:
                    progress.s2_ms = car.sector2_ms
                progress.invalid = progress.invalid or car.lap_invalid
        return emitted

    def apply_history(self, view: HistoryView) -> list[LapRecord]:
        emitted: list[LapRecord] = []
        for lap in view.laps:
            if lap.lap_time_ms <= 0 or lap.lap_number <= 0:
                continue
            record = LapRecord(
                car_index=view.car_index,
                lap_number=lap.lap_number,
                lap_time_ms=lap.lap_time_ms,
                s1_ms=lap.sector1_ms,
                s2_ms=lap.sector2_ms,
                s3_ms=lap.sector3_ms or _sector3(lap.lap_time_ms, lap.sector1_ms, lap.sector2_ms),
                valid=lap.valid,
                source="history",
            )
            if self._store(record):
                emitted.append(record)
        return emitted

    def _store(self, record: LapRecord) -> bool:
        """Insert/correct a record. Returns True when a callback was fired."""
        key = (record.car_index, record.lap_number)
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = record
            self._fire(record, corrected=False)
            return True
        if existing.same_times(record):
            if record.source == "history":
                existing.source = "history"
            return False
        if existing.source == "history" and record.source != "history":
            # Never let a late/lossy LapData sample overwrite authoritative times.
            return False
        self._records[key] = record
        self._fire(record, corrected=True)
        return True

    def _fire(self, record: LapRecord, *, corrected: bool) -> None:
        if self.on_lap is not None:
            self.on_lap(record, corrected)


@dataclass(slots=True)
class SessionCallbacks:
    """Everything the tracker publishes. All optional, all synchronous.

    `on_stall` and `on_lap` are additions to the originally specified set: the
    stall transition has to reach the WS layer as an edge (polling loses it) and
    lap reconciliation is useless unless corrected rows reach the writer.
    """

    on_session_open: Callable[[SessionKey, bool], None] | None = None
    on_session_close: Callable[[SessionKey, CloseReason], None] | None = None
    on_segment: Callable[[SessionKey, int], None] | None = None
    on_generation: Callable[[int, float], None] | None = None
    on_rotate: Callable[[int, int], None] | None = None
    on_stall: Callable[[bool], None] | None = None
    on_lap: Callable[[SessionKey, LapRecord, bool], None] | None = None


@dataclass(slots=True)
class _ClosedSession:
    segment: int
    generation: int


@dataclass(slots=True)
class TrackerStats:
    packets: int = 0
    discarded: int = 0
    menu_ignored: int = 0
    sessions_opened: int = 0
    segments: int = 0
    generations: int = 0
    by_packet_id: dict[int, int] = field(default_factory=dict)


class SessionTracker:
    """The lifecycle state machine. See module docstring for the identity model."""

    __slots__ = (
        "_callbacks",
        "_closed",
        "_last_frame_id",
        "_last_overall_by_packet",
        "_last_session_time",
        "_max_overall",
        "_saw_final_classification",
        "cfg",
        "generation",
        "joined_in_progress",
        "key",
        "laps",
        "last_packet_monotonic_ns",
        "last_packet_wall_ns",
        "session_type",
        "stalled",
        "counters",
    )

    def __init__(self, cfg: Config, callbacks: SessionCallbacks | None = None) -> None:
        self.cfg = cfg
        self._callbacks = callbacks or SessionCallbacks()
        self.key: SessionKey | None = None
        self.generation = 0
        self.joined_in_progress = False
        self.stalled = False
        self.session_type = 0
        self.counters = TrackerStats()
        self.laps = LapLedger(on_lap=self._on_ledger_lap)
        self.last_packet_monotonic_ns: int | None = None
        self.last_packet_wall_ns: int | None = None
        self._closed: dict[int, _ClosedSession] = {}
        self._last_overall_by_packet: dict[int, int] = {}
        self._max_overall = -1
        self._last_frame_id = -1
        self._last_session_time = 0.0
        self._saw_final_classification = False

    # -- introspection -------------------------------------------------------

    def stats(self) -> dict[str, float | int | str]:
        """Flat counters for `/metrics` (satisfies web.interfaces.StatsSource)."""
        key = self.key
        return {
            "packets_total": self.counters.packets,
            "discarded_total": self.counters.discarded,
            "menu_ignored_total": self.counters.menu_ignored,
            "sessions_opened_total": self.counters.sessions_opened,
            "segments_total": self.counters.segments,
            "generations_total": self.counters.generations,
            "stalled": int(self.stalled),
            "generation": self.generation,
            "segment": key.segment if key is not None else -1,
            "session_uid": key.uid_str if key is not None else "",
        }

    @property
    def discarded(self) -> int:
        """Packets dropped by the reorder guard since process start."""
        return self.counters.discarded

    @property
    def context(self) -> SessionContext:
        return SessionContext(
            key=self.key,
            generation=self.generation,
            joined_in_progress=self.joined_in_progress,
            stalled=self.stalled,
        )

    @property
    def is_open(self) -> bool:
        return self.key is not None

    # -- driving -------------------------------------------------------------

    def feed(self, pkt: ParsedPacket) -> bool:
        """Absorb one parsed packet. Returns False when the packet was discarded."""
        header = pkt.header

        # Menus and loading screens emit packets with sessionUID 0. They are not
        # sessions: opening one would fragment the real session around it (every
        # real->menu->real bounce forced a close + reopen-as-segment) and adopt
        # the menu's SessionHistory as phantom laps under a ghost session row.
        # The raw log upstream still captures these bytes; the lifecycle ignores
        # them, which also means menu time reads as STALLED on the dashboard —
        # accurate, since no drivable session is running.
        if header.session_uid == 0:
            self.counters.menu_ignored += 1
            return False

        new_uid = self.key is None or header.session_uid != self.key.session_uid

        # Reorder guard. Skipped across a uid change because overall_frame_identifier
        # is only monotonic within one run of the game.
        if not new_uid and header.packet_id != PacketId.EVENT:
            newest = self._last_overall_by_packet.get(header.packet_id)
            if newest is not None and header.overall_frame_identifier < newest:
                self.counters.discarded += 1
                return False

        self.counters.packets += 1
        self.counters.by_packet_id[header.packet_id] = (
            self.counters.by_packet_id.get(header.packet_id, 0) + 1
        )
        self.last_packet_monotonic_ns = pkt.recv_monotonic_ns
        self.last_packet_wall_ns = pkt.recv_wall_ns
        if self.stalled:
            self.stalled = False
            self._emit_stall(False)

        if new_uid:
            if self.key is not None:
                self._close("finished" if self._saw_final_classification else "superseded")
            self._open(pkt)
        else:
            self._detect_rewind(pkt)

        # High-water marks are advanced after the identity decision so that the
        # packet which opens a session seeds the guard rather than bypassing it.
        if header.packet_id != PacketId.EVENT:
            newest = self._last_overall_by_packet.get(header.packet_id)
            if newest is None or header.overall_frame_identifier > newest:
                self._last_overall_by_packet[header.packet_id] = header.overall_frame_identifier
        if header.overall_frame_identifier > self._max_overall:
            self._max_overall = header.overall_frame_identifier
        self._last_frame_id = header.frame_identifier
        self._last_session_time = header.session_time

        self._apply_view(pkt)
        return True

    def tick(self, now_monotonic_ns: int) -> None:
        """Silence detection. Call at roughly 1 Hz from the pipeline."""
        if self.last_packet_monotonic_ns is None:
            return
        age_s = (now_monotonic_ns - self.last_packet_monotonic_ns) / 1e9
        if age_s >= self.cfg.stall_after_s and not self.stalled:
            self.stalled = True
            self._emit_stall(True)
        if self.key is not None and age_s >= self.cfg.session_timeout_min * 60.0:
            self._close("timeout")

    def shutdown(self) -> None:
        """Close any open session because the process is going away."""
        if self.key is not None:
            self._close("shutdown")

    # -- transitions ---------------------------------------------------------

    def _open(self, pkt: ParsedPacket) -> None:
        header = pkt.header
        previous = self._closed.get(header.session_uid)
        if previous is None:
            segment, generation = 0, 0
        else:
            # Same uid coming back to life within this process: continue it as a
            # fresh segment rather than fabricating a second session row.
            segment, generation = previous.segment + 1, previous.generation

        self.key = SessionKey(header.session_uid, segment)
        self.generation = generation
        self.joined_in_progress = header.session_time > _JOIN_IN_PROGRESS_S
        self._saw_final_classification = False
        self._last_overall_by_packet.clear()
        self._max_overall = -1
        self._last_frame_id = header.frame_identifier
        self._last_session_time = header.session_time
        self.laps.reset()
        self.counters.sessions_opened += 1

        callbacks = self._callbacks
        if callbacks.on_session_open is not None:
            callbacks.on_session_open(self.key, self.joined_in_progress)
        if callbacks.on_rotate is not None:
            callbacks.on_rotate(self.key.session_uid, segment)

    def _detect_rewind(self, pkt: ParsedPacket) -> None:
        header = pkt.header
        frame_rewound = header.frame_identifier < self._last_frame_id
        time_rewound = header.session_time < self._last_session_time - _TIME_EPS_S
        stream_advanced = header.overall_frame_identifier > self._max_overall

        # Flashback: the game replays frames it already sent, so frame_identifier
        # and session_time both go backwards while overall_frame_identifier — which
        # never rewinds — keeps climbing. A merely reordered packet fails the last
        # condition, which is what keeps stale UDP out of this branch.
        if frame_rewound and time_rewound and stream_advanced:
            self.generation += 1
            self.counters.generations += 1
            self.laps.resync()
            if self._callbacks.on_generation is not None:
                self._callbacks.on_generation(self.generation, header.session_time)
            return

        if self._last_session_time - header.session_time > self.cfg.segment_rewind_s:
            self._new_segment()

    def _new_segment(self) -> None:
        assert self.key is not None
        self.key = SessionKey(self.key.session_uid, self.key.segment + 1)
        self._saw_final_classification = False
        self.laps.reset()
        self.counters.segments += 1
        callbacks = self._callbacks
        if callbacks.on_segment is not None:
            callbacks.on_segment(self.key, self.key.segment)
        if callbacks.on_rotate is not None:
            callbacks.on_rotate(self.key.session_uid, self.key.segment)

    def _close(self, reason: CloseReason) -> None:
        key = self.key
        if key is None:
            return
        self._closed[key.session_uid] = _ClosedSession(key.segment, self.generation)
        self.key = None
        self._saw_final_classification = False
        if self._callbacks.on_session_close is not None:
            self._callbacks.on_session_close(key, reason)

    def _emit_stall(self, stalled: bool) -> None:
        if self._callbacks.on_stall is not None:
            self._callbacks.on_stall(stalled)

    # -- view fan-in ---------------------------------------------------------

    def _apply_view(self, pkt: ParsedPacket) -> None:
        view = pkt.view
        if view is None:
            return
        if isinstance(view, LapView):
            self.laps.apply_lap_view(view)
        elif isinstance(view, HistoryView):
            self.laps.apply_history(view)
        elif isinstance(view, ClassificationView):
            self._saw_final_classification = True
        elif isinstance(view, SessionView):
            self.session_type = view.session_type

    def _on_ledger_lap(self, record: LapRecord, corrected: bool) -> None:
        if self._callbacks.on_lap is not None and self.key is not None:
            self._callbacks.on_lap(self.key, record, corrected)

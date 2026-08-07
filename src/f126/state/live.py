"""Live state and WebSocket payload assembly.

`LiveState` is the single place that knows "what is happening right now": it
absorbs the same parsed packets the tracker sees, keeps the derived numbers a
race engineer actually reads (delta to the reference lap, wear rate, fuel delta,
rolling pace, sector colours) and renders the three payload shapes defined in
`docs/ws-protocol.md` plus a queue of discrete events.

Nothing here is async except the event queue: the broadcaster drains
`LiveState.events` and polls `get_fast()` / `get_slow()` on its own timers.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from f126.config import Config
from f126.state.session import SessionKey, SessionKind, session_kind
from f126.types import (
    CarLap,
    DamageView,
    EventView,
    HistoryView,
    LapView,
    MotionView,
    ParsedPacket,
    ParticipantsView,
    ParticipantView,
    SessionView,
    StatusView,
    TelemetryView,
    TimeTrialView,
    WheelSet,
)

PROTOCOL_VERSION = 1

_RECENT_EVENTS = 20
_EVENT_QUEUE_MAX = 512
_WEAR_WINDOW_LAPS = 3
_PACE_WINDOW_LAPS = 3

# 2025 ERS store is 4 MJ; the 2026 energy store is larger and format-dependent, so
# the capacity used for the percentage readout floats up to the largest value seen.
_DEFAULT_ENERGY_CAPACITY_J = 4_000_000.0

_FLAG_NAMES: dict[int, str] = {0: "clear", 1: "green", 2: "blue", 3: "yellow", 4: "red"}
_SC_NAMES: dict[int, str] = {0: "in", 1: "deployed", 2: "virtual", 3: "deployed"}

_TRACK_NAME_FN: Callable[[int], str] | None = None
_TRACK_NAME_LOADED = False


def resolve_track_name(track_id: int) -> str:
    """Track name via the parser's enum table when it exists, else a stable stub.

    The parser is built in parallel with this module, so the import is lazy and
    defensive; a missing (or broken) `f126.parser.enums` must not take the state
    layer down.
    """
    global _TRACK_NAME_FN, _TRACK_NAME_LOADED
    if not _TRACK_NAME_LOADED:
        _TRACK_NAME_LOADED = True
        try:
            from f126.parser.enums import track_name  # noqa: PLC0415
        except Exception:
            _TRACK_NAME_FN = None
        else:
            _TRACK_NAME_FN = track_name
    if track_id is None or track_id < 0:
        return "unknown"
    if _TRACK_NAME_FN is not None:
        try:
            name = _TRACK_NAME_FN(track_id)
        except Exception:
            name = ""
        if name:
            return str(name)
    return f"track_{track_id}"


def wheels(value: WheelSet | None) -> list[float] | None:
    """WheelSet -> `[rl, rr, fl, fr]`, the wire order used everywhere downstream."""
    if value is None:
        return None
    return [value.rl, value.rr, value.fl, value.fr]


def _detail(details: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in details:
            return details[name]
    return default


_COERCE_ERRORS = (TypeError, ValueError)


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except _COERCE_ERRORS:
        return default


class LapTrace:
    """Distance-indexed elapsed-time trace for a single lap.

    Samples are appended only when lap distance strictly increases, which keeps
    `dist` sorted so the delta lookup is a bisect. `stime` carries the game clock
    for each sample purely so a flashback can chop the tail off exactly.
    """

    __slots__ = ("dist", "elapsed_ms", "stime")

    def __init__(self) -> None:
        self.dist: list[float] = []
        self.elapsed_ms: list[int] = []
        self.stime: list[float] = []

    def __len__(self) -> int:
        return len(self.dist)

    def append(self, dist: float, elapsed_ms: int, session_time: float) -> None:
        if dist <= 0.0 or elapsed_ms <= 0:
            return
        if self.dist and dist <= self.dist[-1]:
            return
        self.dist.append(dist)
        self.elapsed_ms.append(elapsed_ms)
        self.stime.append(session_time)

    def truncate_after(self, session_time: float) -> int:
        """Drop every sample recorded after `session_time`. Returns count dropped."""
        dropped = 0
        while self.stime and self.stime[-1] > session_time:
            self.dist.pop()
            self.elapsed_ms.pop()
            self.stime.pop()
            dropped += 1
        return dropped

    def clear(self) -> None:
        self.dist.clear()
        self.elapsed_ms.clear()
        self.stime.clear()

    def elapsed_at(self, dist: float) -> float | None:
        """Linearly interpolated elapsed ms at `dist`, or None if unusable."""
        if not self.dist:
            return None
        if dist <= self.dist[0]:
            # Interpolate from the start line rather than clamping: the first
            # sample can be tens of metres in.
            if self.dist[0] <= 0:
                return float(self.elapsed_ms[0])
            return float(self.elapsed_ms[0]) * (dist / self.dist[0])
        if dist >= self.dist[-1]:
            return float(self.elapsed_ms[-1])
        i = bisect.bisect_left(self.dist, dist)
        d0, d1 = self.dist[i - 1], self.dist[i]
        e0, e1 = self.elapsed_ms[i - 1], self.elapsed_ms[i]
        span = d1 - d0
        if span <= 0:
            return float(e1)
        return e0 + (e1 - e0) * (dist - d0) / span


@dataclass(slots=True)
class CompletedLap:
    car_index: int
    lap_number: int
    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    s3_ms: int
    valid: bool

    @property
    def sectors(self) -> list[int | None]:
        return [self.s1_ms or None, self.s2_ms or None, self.s3_ms or None]


@dataclass(slots=True)
class _CarState:
    seen: bool = False
    lap_number: int = 0
    s1_ms: int = 0
    s2_ms: int = 0
    sector: int = 0
    invalid: bool = False
    pit_status: int = 0
    best_lap_ms: int | None = None
    last3: deque[int] = field(default_factory=lambda: deque(maxlen=_PACE_WINDOW_LAPS))


class LiveState:
    """Live view state plus the `snapshot` / `fast` / `slow` / `event` payloads."""

    def __init__(self, cfg: Config, *, events: asyncio.Queue[dict[str, Any]] | None = None) -> None:
        self.cfg = cfg
        self.events: asyncio.Queue[dict[str, Any]] = (
            events if events is not None else asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
        )
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=_RECENT_EVENTS)
        self.events_dropped = 0

        self.session_uid: int | None = None
        self.segment = 0
        self.generation = 0
        self.joined_in_progress = False
        self.stalled = False
        self.packet_format = 0
        self.player_car_index = 0
        self.session_time = 0.0

        self._health: dict[str, int] = {
            "packets_per_sec": 0,
            "parse_errors_total": 0,
            "kernel_drops_total": 0,
            "ws_clients": 0,
        }
        self._last_packet_monotonic_ns: int | None = None
        self._participants: dict[int, ParticipantView] = {}
        self._reset_live()

    # -- lifecycle (wired to SessionCallbacks) -------------------------------

    def on_session_open(self, key: SessionKey, joined_in_progress: bool) -> None:
        self.session_uid = key.session_uid
        self.segment = key.segment
        self.generation = 0
        self.joined_in_progress = joined_in_progress
        self.session_time = 0.0
        self._participants.clear()
        self._reset_live()
        self._emit("SESSION_START", {})
        self._session_start_emitted = True

    def on_segment(self, key: SessionKey, segment: int) -> None:
        self.segment = segment
        self._reset_live()

    def on_generation(self, generation: int, rewind_to_session_time: float) -> None:
        self.generation = generation
        self._trace.truncate_after(rewind_to_session_time)
        for state in self._cars.values():
            # Lap number, sector times and the invalid flag all rewind; re-seed
            # them from the next LapData instead of diffing across the jump.
            state.seen = False
        self._last_flashback_t = rewind_to_session_time
        self._emit("FLASHBACK", {"to_session_time_s": rewind_to_session_time})

    def on_session_close(self, key: SessionKey, reason: str) -> None:
        if not self._session_end_emitted:
            self._session_end_emitted = True
            self._emit("SESSION_END", {})

    def on_stall(self, stalled: bool) -> None:
        if stalled == self.stalled:
            return
        self.stalled = stalled
        self._emit("STALLED", {"stalled": stalled})

    def stats(self) -> dict[str, float | int | str]:
        """Flat counters for `/metrics` (satisfies web.interfaces.StatsSource)."""
        return {
            "events_dropped_total": self.events_dropped,
            "event_queue_depth": self.events.qsize(),
            "cars_tracked": len(self._cars),
            "reference_lap_ms": self._reference_ms if self._reference_ms is not None else -1,
        }

    def update_health(self, **values: int) -> None:
        """Set health counters owned by other layers (capture, parser, web)."""
        for name, value in values.items():
            if name in self._health and value is not None:
                self._health[name] = int(value)

    # -- packet fan-in -------------------------------------------------------

    def update(self, pkt: ParsedPacket) -> None:
        view = pkt.view
        if view is None:
            return
        header = pkt.header
        self.session_time = header.session_time
        self.packet_format = header.packet_format
        self.player_car_index = header.player_car_index
        self._last_packet_monotonic_ns = pkt.recv_monotonic_ns

        if isinstance(view, TelemetryView):
            self._telemetry = view
        elif isinstance(view, LapView):
            self._on_lap_view(view)
        elif isinstance(view, StatusView):
            self._on_status(view)
        elif isinstance(view, DamageView):
            self._damage = view
        elif isinstance(view, MotionView):
            self._motion = view
        elif isinstance(view, SessionView):
            self._on_session_view(view)
        elif isinstance(view, ParticipantsView):
            for participant in view.cars:
                self._participants[participant.car_index] = participant
        elif isinstance(view, EventView):
            self._on_event(view)
        elif isinstance(view, HistoryView):
            self._on_history(view)
        elif isinstance(view, TimeTrialView):
            self._timetrial = view

    # -- payloads ------------------------------------------------------------

    def get_fast(self) -> dict[str, Any] | None:
        telemetry = self._telemetry
        if telemetry is None:
            return None
        player = self._lap.player if self._lap is not None else None
        delta_ms, delta_kind = self.delta()
        return {
            "type": "fast",
            "t": self.session_time,
            "speed_kmh": float(telemetry.speed_kmh),
            "gear": int(telemetry.gear),
            "rpm": int(telemetry.rpm),
            "throttle": float(telemetry.throttle),
            "brake": float(telemetry.brake),
            "steer": float(telemetry.steer),
            "drs_open": telemetry.drs_open,
            "aero_mode": telemetry.aero_mode,
            "ers_deploy_mode": self._deploy_mode(),
            "rev_lights_percent": int(telemetry.rev_lights_percent),
            "lap_number": int(player.lap_number) if player else 0,
            "lap_distance_m": float(player.lap_distance_m) if player else 0.0,
            "current_lap_ms": int(player.current_lap_ms) if player else 0,
            "delta_best_ms": delta_ms,
            "delta_kind": delta_kind,
            # Additive to the documented shape: the delta bar greys out on an
            # invalidated lap and the client needs to know without waiting for
            # the 1 Hz frame.
            "lap_invalid": self.lap_invalid,
        }

    def get_slow(self) -> dict[str, Any] | None:
        if self.session_uid is None:
            return None
        return {
            "type": "slow",
            "session": self._session_payload(),
            "tower": self._tower_payload(),
            "tyres": self._tyres_payload(),
            "fuel": self._fuel_payload(),
            "energy": self._energy_payload(),
            "damage": self._damage_payload(),
            "pace": self._pace_payload(),
            "sectors": self._sectors_payload(),
            "timetrial": self._timetrial_payload(),
            "health": self._health_payload(),
        }

    def get_snapshot(self) -> dict[str, Any]:
        slow = self.get_slow()
        return {
            "type": "snapshot",
            "protocol_version": PROTOCOL_VERSION,
            "session": slow["session"] if slow else self._session_payload(),
            "fast": self.get_fast(),
            "slow": slow,
            "recent_events": list(self.recent_events),
        }

    # -- derived readouts ----------------------------------------------------

    @property
    def session_kind(self) -> SessionKind:
        return session_kind(self._session.session_type if self._session else 0)

    @property
    def lap_invalid(self) -> bool:
        state = self._cars.get(self.player_car_index)
        return bool(state.invalid) if state is not None else False

    @property
    def reference_lap_ms(self) -> int | None:
        return self._reference_ms

    def delta(self) -> tuple[int | None, str | None]:
        """Live delta to the reference lap: `(delta_ms, delta_kind)`."""
        if self._reference is None or self._lap is None:
            return None, None
        player = self._lap.player
        reference_elapsed = self._reference.elapsed_at(player.lap_distance_m)
        if reference_elapsed is None or player.current_lap_ms <= 0:
            return None, self._delta_kind()
        return int(round(player.current_lap_ms - reference_elapsed)), self._delta_kind()

    def wear_rate_pct_per_lap(self) -> list[float] | None:
        if not self._wear_rates:
            return None
        count = len(self._wear_rates)
        return [round(sum(rates[i] for rates in self._wear_rates) / count, 4) for i in range(4)]

    def laps_left_in_session(self) -> int | None:
        """Laps still to be driven, current lap included. None in timed sessions."""
        if self._session is None or self._session.total_laps <= 0:
            return None
        lap_number = self._lap.player.lap_number if self._lap else 0
        return max(0, self._session.total_laps - max(lap_number, 1) + 1)

    # -- internals: reset ----------------------------------------------------

    def _reset_live(self) -> None:
        self._telemetry: TelemetryView | None = None
        self._status: StatusView | None = None
        self._damage: DamageView | None = None
        self._motion: MotionView | None = None
        self._session: SessionView | None = None
        self._lap: LapView | None = None
        self._timetrial: TimeTrialView | None = None

        self._cars: dict[int, _CarState] = {}
        self._session_best_sector: list[int | None] = [None, None, None]
        self._player_best_sector: list[int | None] = [None, None, None]
        self._session_best_lap_ms: int | None = None
        self._player_best_lap_ms: int | None = None
        self._reference_sectors: list[int | None] = [None, None, None]
        self._last_lap_valid: bool | None = None

        self._trace = LapTrace()
        self._reference: LapTrace | None = None
        self._reference_ms: int | None = None

        self._wear_at_lap_end: list[float] | None = None
        self._wear_rates: deque[list[float]] = deque(maxlen=_WEAR_WINDOW_LAPS)
        self._fuel_at_lap_start: float | None = None
        self._burn_last_lap_kg: float | None = None
        self._energy_capacity_j = _DEFAULT_ENERGY_CAPACITY_J

        self._last_fia_flag: int | None = None
        self._last_safety_car: int | None = None
        self._last_flashback_t = -1.0
        self._session_start_emitted = False
        self._session_end_emitted = False

    # -- internals: events ---------------------------------------------------

    def _emit(self, code: str, data: dict[str, Any]) -> dict[str, Any]:
        message = {"type": "event", "t": self.session_time, "code": code, "data": data}
        self.recent_events.append(message)
        try:
            self.events.put_nowait(message)
        except asyncio.QueueFull:
            # A wedged consumer must not stop the newest events getting through.
            self.events_dropped += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self.events.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.events.put_nowait(message)
        return message

    def _on_event(self, view: EventView) -> None:
        code = view.code.upper()
        details = view.details
        if code == "SSTA":
            if not self._session_start_emitted:
                self._session_start_emitted = True
                self._emit("SESSION_START", {})
        elif code == "SEND":
            if not self._session_end_emitted:
                self._session_end_emitted = True
                self._emit("SESSION_END", {})
        elif code == "CHQF":
            self._emit("CHEQUERED", {})
        elif code == "FTLP":
            car_index = _as_int(
                _detail(details, "vehicle_idx", "vehicleIdx", "car_index", "carIndex"), -1
            )
            self._emit(
                "FASTEST_LAP",
                {
                    "car_index": car_index,
                    "name": self._car_name(car_index),
                    "time_ms": self._lap_time_ms(details),
                },
            )
        elif code == "PENA":
            self._emit(
                "PENALTY",
                {
                    "penalty_type": _as_int(_detail(details, "penalty_type", "penaltyType"), None),
                    "infringement_type": _as_int(
                        _detail(details, "infringement_type", "infringementType"), None
                    ),
                    "time_s": _as_int(_detail(details, "time_s", "time", "timeS"), None),
                    "other_car": _as_int(
                        _detail(details, "other_car", "other_vehicle_idx", "otherVehicleIdx"),
                        None,
                    ),
                },
            )
        elif code in ("DRSE", "DRSD"):
            self._emit("DRS", {"enabled": code == "DRSE"})
        elif code == "RDFL":
            self._emit("FLAG", {"flag": "red"})
            self._last_fia_flag = 4
        elif code == "FLBK":
            self._on_flashback_event(details)
        elif code == "SCAR":
            self._on_safety_car_event(details)

    def _on_flashback_event(self, details: dict[str, Any]) -> None:
        raw = _detail(details, "session_time", "sessionTime", "flashback_session_time")
        try:
            to_time = float(raw) if raw is not None else self.session_time
        except _COERCE_ERRORS:
            to_time = self.session_time
        # The header-signature detection in SessionTracker already emits FLASHBACK;
        # the FLBK packet and the rewind describe the same instant.
        if abs(to_time - self._last_flashback_t) <= 1.0:
            return
        self._last_flashback_t = to_time
        self._emit("FLASHBACK", {"to_session_time_s": to_time})

    def _on_safety_car_event(self, details: dict[str, Any]) -> None:
        event_type = _as_int(_detail(details, "event_type", "eventType"), 0) or 0
        sc_type = _as_int(_detail(details, "safety_car_type", "safetyCarType"), 0) or 0
        status = "ending" if event_type in (1, 2) else _SC_NAMES.get(sc_type, "deployed")
        self._emit("SC", {"status": status})
        self._last_safety_car = sc_type

    def _car_name(self, car_index: int) -> str:
        participant = self._participants.get(car_index)
        return participant.name if participant else f"CAR {car_index}"

    @staticmethod
    def _lap_time_ms(details: dict[str, Any]) -> int | None:
        raw_ms = _detail(details, "lap_time_ms", "lapTimeMs")
        value = _as_int(raw_ms, None)
        if value is not None:
            return value
        raw_s = _detail(details, "lap_time", "lapTime", "lap_time_s")
        try:
            return int(round(float(raw_s) * 1000.0)) if raw_s is not None else None
        except _COERCE_ERRORS:
            return None

    # -- internals: view handlers -------------------------------------------

    def _on_session_view(self, view: SessionView) -> None:
        self._session = view
        if self._last_safety_car is None:
            self._last_safety_car = view.safety_car_status
        elif view.safety_car_status != self._last_safety_car:
            self._last_safety_car = view.safety_car_status
            self._emit("SC", {"status": _SC_NAMES.get(view.safety_car_status, "deployed")})

    def _on_status(self, view: StatusView) -> None:
        self._status = view
        if view.ers_store_j is not None:
            self._energy_capacity_j = max(self._energy_capacity_j, view.ers_store_j)
        if self._fuel_at_lap_start is None:
            self._fuel_at_lap_start = view.fuel_in_tank_kg
        if view.fia_flags != self._last_fia_flag:
            self._last_fia_flag = view.fia_flags
            name = _FLAG_NAMES.get(view.fia_flags)
            if name is not None:
                self._emit("FLAG", {"flag": name})

    def _on_history(self, view: HistoryView) -> None:
        """SessionHistory is authoritative for bests, including opponents'."""
        is_player = view.car_index == self.player_car_index
        state = self._cars.setdefault(view.car_index, _CarState())
        for lap in view.laps:
            if lap.lap_time_ms <= 0 or not lap.valid:
                continue
            if state.best_lap_ms is None or lap.lap_time_ms < state.best_lap_ms:
                state.best_lap_ms = lap.lap_time_ms
            if self._session_best_lap_ms is None or lap.lap_time_ms < self._session_best_lap_ms:
                self._session_best_lap_ms = lap.lap_time_ms
            if is_player and (
                self._player_best_lap_ms is None or lap.lap_time_ms < self._player_best_lap_ms
            ):
                self._player_best_lap_ms = lap.lap_time_ms
            for index, value in enumerate((lap.sector1_ms, lap.sector2_ms, lap.sector3_ms)):
                if value <= 0:
                    continue
                best = self._session_best_sector[index]
                if best is None or value < best:
                    self._session_best_sector[index] = value
                if is_player:
                    personal = self._player_best_sector[index]
                    if personal is None or value < personal:
                        self._player_best_sector[index] = value

    def _on_lap_view(self, view: LapView) -> None:
        self._lap = view
        player_index = self.player_car_index
        for car in view.cars:
            self._update_car(car, is_player=car.car_index == player_index)
        player = view.player
        if not any(car.car_index == player_index for car in view.cars):
            self._update_car(player, is_player=True)
        self._trace.append(player.lap_distance_m, player.current_lap_ms, self.session_time)

    def _update_car(self, car: CarLap, *, is_player: bool) -> None:
        state = self._cars.setdefault(car.car_index, _CarState())
        if not state.seen:
            state.seen = True
            state.lap_number = car.lap_number
            state.s1_ms = car.sector1_ms
            state.s2_ms = car.sector2_ms
            state.sector = car.sector
            state.invalid = car.lap_invalid
            state.pit_status = car.pit_status
            return

        if car.lap_number > state.lap_number:
            self._complete_lap(car, state, is_player=is_player)
        elif car.lap_number < state.lap_number:
            state.lap_number = car.lap_number
            state.s1_ms = car.sector1_ms
            state.s2_ms = car.sector2_ms
            state.sector = car.sector
            state.invalid = car.lap_invalid
        else:
            self._update_sectors(car, state, is_player=is_player)

        if is_player:
            self._player_flags(car, state)
        state.pit_status = car.pit_status

    def _update_sectors(self, car: CarLap, state: _CarState, *, is_player: bool) -> None:
        if car.sector1_ms > 0:
            state.s1_ms = car.sector1_ms
        if car.sector2_ms > 0:
            state.s2_ms = car.sector2_ms
        was_invalid = state.invalid
        state.invalid = state.invalid or car.lap_invalid
        if is_player and state.invalid and not was_invalid:
            self._emit("LAP_INVALID", {"lap_number": car.lap_number})

        if car.sector > state.sector:
            for index in range(state.sector, min(car.sector, 2)):
                time_ms = state.s1_ms if index == 0 else state.s2_ms
                if time_ms <= 0:
                    continue
                colour = self._grade_sector(index, time_ms, not state.invalid, is_player)
                if is_player:
                    self._emit("SECTOR", {"sector": index + 1, "time_ms": time_ms, "color": colour})
        state.sector = car.sector

    def _complete_lap(self, car: CarLap, state: _CarState, *, is_player: bool) -> None:
        lap_time_ms = car.last_lap_ms
        s1_ms, s2_ms = state.s1_ms, state.s2_ms
        s3_ms = (
            lap_time_ms - s1_ms - s2_ms
            if lap_time_ms > 0 and s1_ms > 0 and s2_ms > 0 and lap_time_ms > s1_ms + s2_ms
            else 0
        )
        valid = not state.invalid
        completed = (
            CompletedLap(car.car_index, state.lap_number, lap_time_ms, s1_ms, s2_ms, s3_ms, valid)
            if lap_time_ms > 0 and state.lap_number > 0
            else None
        )

        if completed is not None:
            state.last3.append(lap_time_ms)
            if valid and (state.best_lap_ms is None or lap_time_ms < state.best_lap_ms):
                state.best_lap_ms = lap_time_ms
            if s3_ms > 0:
                colour3 = self._grade_sector(2, s3_ms, valid, is_player)
                if is_player:
                    self._emit("SECTOR", {"sector": 3, "time_ms": s3_ms, "color": colour3})
            lap_colour = self._grade_lap(lap_time_ms, valid, is_player)
            if is_player:
                self._last_lap_valid = valid
                self._emit(
                    "LAP",
                    {
                        "lap_number": completed.lap_number,
                        "time_ms": lap_time_ms,
                        "valid": valid,
                        "color": lap_colour,
                        "sectors": completed.sectors,
                    },
                )

        state.lap_number = car.lap_number
        state.s1_ms = car.sector1_ms
        state.s2_ms = car.sector2_ms
        state.sector = car.sector
        state.invalid = car.lap_invalid

        if is_player:
            self._roll_player_lap(completed)

    def _roll_player_lap(self, completed: CompletedLap | None) -> None:
        """Close out the player's lap: reference trace, wear rate, fuel burn."""
        track_length = self._session.track_length_m if self._session else 0
        if completed is not None and track_length > 0:
            self._trace.append(float(track_length), completed.lap_time_ms, self.session_time)

        if (
            completed is not None
            and completed.valid
            and len(self._trace) >= 2
            and (self._reference_ms is None or completed.lap_time_ms < self._reference_ms)
        ):
            self._reference = self._trace
            self._reference_ms = completed.lap_time_ms
            self._reference_sectors = completed.sectors
        self._trace = LapTrace()

        if self._damage is not None:
            current = wheels(self._damage.tyre_wear_pct) or [0.0] * 4
            if self._wear_at_lap_end is not None:
                self._wear_rates.append(
                    [max(0.0, current[i] - self._wear_at_lap_end[i]) for i in range(4)]
                )
            self._wear_at_lap_end = current

        if self._status is not None:
            fuel = self._status.fuel_in_tank_kg
            if self._fuel_at_lap_start is not None:
                self._burn_last_lap_kg = max(0.0, self._fuel_at_lap_start - fuel)
            self._fuel_at_lap_start = fuel

    def _player_flags(self, car: CarLap, state: _CarState) -> None:
        if car.pit_status != state.pit_status:
            if state.pit_status == 0 and car.pit_status != 0:
                self._emit("PIT_IN", {"lap_number": car.lap_number})
            elif state.pit_status != 0 and car.pit_status == 0:
                self._emit("PIT_OUT", {"lap_number": car.lap_number})

    def _grade_sector(self, index: int, time_ms: int, valid: bool, is_player: bool) -> str:
        """Purple = best anyone has set, green = the player's own best, else yellow."""
        if time_ms <= 0 or not valid:
            return "yellow"
        colour = "yellow"
        best = self._session_best_sector[index]
        if best is None or time_ms < best:
            self._session_best_sector[index] = time_ms
            colour = "purple"
        if is_player:
            personal = self._player_best_sector[index]
            if personal is None or time_ms < personal:
                self._player_best_sector[index] = time_ms
                if colour != "purple":
                    colour = "green"
        return colour

    def _grade_lap(self, time_ms: int, valid: bool, is_player: bool) -> str:
        if time_ms <= 0 or not valid:
            return "yellow"
        colour = "yellow"
        if self._session_best_lap_ms is None or time_ms < self._session_best_lap_ms:
            self._session_best_lap_ms = time_ms
            colour = "purple"
        if is_player and (self._player_best_lap_ms is None or time_ms < self._player_best_lap_ms):
            self._player_best_lap_ms = time_ms
            if colour != "purple":
                colour = "green"
        return colour

    def _delta_kind(self) -> str:
        kind = self.session_kind
        if kind == "race":
            return "race_best"
        if kind == "time_trial":
            return "personal_best"
        return "session_best"

    def _deploy_mode(self) -> int | None:
        if self._telemetry is not None and self._telemetry.energy_deploy_mode is not None:
            return self._telemetry.energy_deploy_mode
        if self._status is not None and self._status.ers_deploy_mode is not None:
            return self._status.ers_deploy_mode
        return None

    def _energy_store_j(self) -> float | None:
        if self._telemetry is not None and self._telemetry.energy_store_j is not None:
            return self._telemetry.energy_store_j
        if self._status is not None and self._status.ers_store_j is not None:
            return self._status.ers_store_j
        return None

    # -- internals: payload sections ----------------------------------------

    def _session_payload(self) -> dict[str, Any]:
        view = self._session
        track_id = view.track_id if view else -1
        return {
            "session_uid": str(self.session_uid) if self.session_uid is not None else "",
            "segment": self.segment,
            "packet_format": self.packet_format,
            "session_type": view.session_type if view else 0,
            "session_kind": self.session_kind,
            "track_id": track_id,
            "track_name": resolve_track_name(track_id),
            "time_left_s": view.session_time_left_s if view else None,
            "duration_s": view.session_duration_s if view else None,
            "total_laps": view.total_laps if view else None,
            "safety_car": view.safety_car_status if view else 0,
            "fia_flag": self._status.fia_flags if self._status else 0,
            "weather": view.weather if view else 0,
            "track_temp_c": view.track_temp_c if view else None,
            "air_temp_c": view.air_temp_c if view else None,
            "forecast": [
                {
                    "offset_min": sample.offset_min,
                    "weather": sample.weather,
                    "rain_pct": sample.rain_percentage,
                }
                for sample in (view.forecast if view else [])
            ],
            "stalled": self.stalled,
            "joined_in_progress": self.joined_in_progress,
        }

    def _tower_payload(self) -> list[dict[str, Any]]:
        if self._lap is None:
            return []
        status = self._status
        rows: list[dict[str, Any]] = []
        for car in self._lap.cars:
            if car.position <= 0:
                continue
            is_player = car.car_index == self.player_car_index
            participant = self._participants.get(car.car_index)
            rows.append(
                {
                    "car_index": car.car_index,
                    "position": car.position,
                    "name": participant.name if participant else f"CAR {car.car_index}",
                    "team_id": participant.team_id if participant else -1,
                    "is_player": is_player,
                    "lap_number": car.lap_number,
                    "last_lap_ms": car.last_lap_ms or None,
                    "gap_ahead_ms": None if car.position == 1 else car.delta_to_car_ahead_ms,
                    "gap_leader_ms": car.delta_to_leader_ms,
                    # Opponent tyre state is not carried by the parser contract.
                    "compound_visual": (
                        status.tyre_compound_visual if is_player and status else None
                    ),
                    "tyre_age_laps": status.tyre_age_laps if is_player and status else None,
                    "pit_status": car.pit_status,
                    "penalties_s": car.penalties_s,
                    "result_status": car.result_status,
                }
            )
        rows.sort(key=lambda row: row["position"])
        return rows

    def _tyres_payload(self) -> dict[str, Any]:
        damage = self._damage
        status = self._status
        telemetry = self._telemetry
        rate = self.wear_rate_pct_per_lap()
        wear = wheels(damage.tyre_wear_pct) if damage else None
        projected: list[float] | None = None
        laps_left = self.laps_left_in_session()
        if self.session_kind == "race" and wear is not None and rate is not None and laps_left:
            projected = [round(min(100.0, wear[i] + rate[i] * laps_left), 2) for i in range(4)]
        return {
            "surface_temp_c": wheels(telemetry.tyre_surface_temp) if telemetry else None,
            "inner_temp_c": wheels(telemetry.tyre_inner_temp) if telemetry else None,
            "pressure_psi": wheels(telemetry.tyre_pressure) if telemetry else None,
            "wear_pct": wear,
            "wear_rate_pct_per_lap": rate,
            "projected_wear_end_pct": projected,
            "compound_actual": status.tyre_compound_actual if status else None,
            "compound_visual": status.tyre_compound_visual if status else None,
            # The game's own counter, passed through unmodified. Measured against the
            # Jeddah race capture it is *laps completed on this set*, not laps started:
            # it reads 0 for the whole lap the tyres are fitted on and ticks to 1 as that
            # lap ends. So a set fitted during lap 6 reads 7 while lap 13 is running.
            #
            # This is deliberately NOT recomputed from the stint boundaries. The stint
            # strip answers a different question -- which laps were *driven* on each set,
            # where the pit lap is attributed to the stint it started on -- and the two
            # only look inconsistent if the strip counts the in-lap twice. It no longer
            # does (see `displayStintRanges` in the frontend), so a set fitted on lap 6
            # shows a 7-lap stint here and a 7-lap range there.
            "age_laps": status.tyre_age_laps if status else None,
        }

    def _fuel_payload(self) -> dict[str, Any]:
        status = self._status
        laps_left = self.laps_left_in_session()
        remaining = status.fuel_remaining_laps if status else None
        delta = None
        if remaining is not None and laps_left is not None:
            delta = round(remaining - laps_left, 3)
        return {
            "in_tank_kg": status.fuel_in_tank_kg if status else None,
            "remaining_laps": remaining,
            "laps_left_in_session": laps_left,
            "delta_laps": delta,
            "burn_last_lap_kg": (
                round(self._burn_last_lap_kg, 3) if self._burn_last_lap_kg is not None else None
            ),
        }

    def _harvested_lap_j(self) -> float | None:
        """2026 routes the per-lap energy through TelemetryView; 2025 via StatusView."""
        if self._telemetry is not None and self._telemetry.energy_harvested_lap_j is not None:
            return self._telemetry.energy_harvested_lap_j
        if self._status is not None and self._status.ers_harvested_lap_j is not None:
            return self._status.ers_harvested_lap_j
        return None

    def _deployed_lap_j(self) -> float | None:
        if self._telemetry is not None and self._telemetry.energy_deployed_lap_j is not None:
            return self._telemetry.energy_deployed_lap_j
        if self._status is not None and self._status.ers_deployed_lap_j is not None:
            return self._status.ers_deployed_lap_j
        return None

    def _energy_payload(self) -> dict[str, Any]:
        store = self._energy_store_j()
        capacity = self._energy_capacity_j
        if store is not None:
            capacity = self._energy_capacity_j = max(capacity, store)
        return {
            "store_j": store,
            "store_pct": (
                round(100.0 * store / capacity, 2) if store is not None and capacity > 0 else None
            ),
            "deploy_mode": self._deploy_mode(),
            "harvested_lap_j": self._harvested_lap_j(),
            "deployed_lap_j": self._deployed_lap_j(),
        }

    def _damage_payload(self) -> dict[str, Any]:
        damage = self._damage
        return {
            "front_left_wing_pct": damage.front_left_wing_pct if damage else None,
            "front_right_wing_pct": damage.front_right_wing_pct if damage else None,
            "rear_wing_pct": damage.rear_wing_pct if damage else None,
            "floor_pct": damage.floor_pct if damage else None,
            "diffuser_pct": damage.diffuser_pct if damage else None,
            "sidepod_pct": damage.sidepod_pct if damage else None,
            "gearbox_pct": damage.gearbox_pct if damage else None,
            "engine_pct": damage.engine_pct if damage else None,
        }

    def _pace_payload(self) -> dict[str, Any]:
        return {
            "last_3_avg_ms": self._avg_pace(self.player_car_index),
            "ahead_last_3_avg_ms": self._avg_pace(self._neighbour(-1)),
            "behind_last_3_avg_ms": self._avg_pace(self._neighbour(1)),
        }

    def _neighbour(self, offset: int) -> int | None:
        if self._lap is None:
            return None
        player = self._lap.player
        target = player.position + offset
        if target < 1:
            return None
        for car in self._lap.cars:
            if car.position == target:
                return car.car_index
        return None

    def _avg_pace(self, car_index: int | None) -> int | None:
        if car_index is None:
            return None
        state = self._cars.get(car_index)
        if state is None or not state.last3:
            return None
        return int(round(sum(state.last3) / len(state.last3)))

    def _sectors_payload(self) -> dict[str, Any]:
        state = self._cars.get(self.player_car_index)
        current: list[int | None] = [None, None, None]
        if state is not None:
            current = [state.s1_ms or None, state.s2_ms or None, None]
        return {
            "current_lap": current,
            "best_lap": list(self._reference_sectors),
            "session_best": list(self._session_best_sector),
            "last_lap_valid": self._last_lap_valid,
        }

    def _timetrial_payload(self) -> dict[str, Any] | None:
        if self.session_kind != "time_trial" or self._timetrial is None:
            return None
        view = self._timetrial
        personal = view.personal_best
        rival = view.rival
        return {
            "pb_ms": personal.lap_time_ms if personal else None,
            "rival_ms": rival.lap_time_ms if rival else None,
            "pb_sectors": (
                [personal.sector1_ms, personal.sector2_ms, personal.sector3_ms]
                if personal
                else [None, None, None]
            ),
            "rival_sectors": (
                [rival.sector1_ms, rival.sector2_ms, rival.sector3_ms]
                if rival
                else [None, None, None]
            ),
        }

    def _health_payload(self) -> dict[str, Any]:
        age_ms: int | None = None
        if self._last_packet_monotonic_ns is not None:
            elapsed_ns = time.monotonic_ns() - self._last_packet_monotonic_ns
            age_ms = max(0, int(elapsed_ns / 1_000_000))
        return {
            "packets_per_sec": self._health["packets_per_sec"],
            "parse_errors_total": self._health["parse_errors_total"],
            "kernel_drops_total": self._health["kernel_drops_total"],
            "last_packet_age_ms": age_ms,
            "ws_clients": self._health["ws_clients"],
        }

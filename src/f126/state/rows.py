"""Row-dict builders for the DB writer.

Every table is fed through a single injected `emit(table, row)` callback so the
writer owns batching, retries and the actual SQL. Column names here are frozen —
the store layer builds tables to match.

Rows are written optimistically and re-emitted when better information arrives
(SessionHistory correcting a lap, a session gaining its track once the Session
packet lands, a tyre stint closing), so every table is an upsert target:

* `sessions` — PK `(session_uid, segment)`
* `participants` — PK `(session_key_uid, segment, car_index)`
* `laps` — PK `(session_key_uid, segment, car_index, lap_number)`
* `tyre_stints` — PK `(session_key_uid, segment, car_index, stint_no)`
* `telemetry_samples`, `wear_samples`, `events` — append-only
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from f126.config import Config
from f126.state.live import resolve_track_name, wheels
from f126.state.session import (
    CloseReason,
    LapRecord,
    SessionKey,
    session_type_name,
)
from f126.types import (
    ClassificationView,
    DamageView,
    EventView,
    LapView,
    MotionView,
    ParsedPacket,
    ParticipantsView,
    SessionView,
    StatusView,
    TelemetryView,
)

EmitRow = Callable[[str, dict[str, Any]], None]

# A synthetic flashback event row is suppressed if the game already sent FLBK
# within this many seconds of game time.
_FLBK_DEDUPE_S = 2.0

_EMPTY_LAP_CONTEXT: dict[str, Any] = {
    "compound_actual": None,
    "compound_visual": None,
    "tyre_age_laps": None,
    "fuel_start_kg": None,
    "fuel_end_kg": None,
    "ers_deployed_j": None,
    "ers_harvested_j": None,
    "top_speed_kmh": None,
    "penalties_s": None,
}


class NearestDecimator:
    """Fixed-rate sampler that keeps the packet nearest each target instant.

    Straight thresholding ("emit the first packet past the deadline") biases every
    sample late by up to a full source interval. Holding one packet of lookahead
    and picking whichever of the two straddling packets is closer keeps the
    recorded values exact — no averaging — while landing on grid.
    """

    __slots__ = ("_base_t", "_pending", "_slot", "interval_s")

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._base_t: float | None = None
        self._slot = 0
        self._pending: tuple[float, Any] | None = None

    def reset(self) -> None:
        self._base_t = None
        self._slot = 0
        self._pending = None

    @property
    def _next_t(self) -> float:
        # Multiplication, not repeated addition: the grid must not drift over the
        # hundreds of thousands of samples in a race.
        assert self._base_t is not None
        return self._base_t + self._slot * self.interval_s

    def _start(self, t: float) -> None:
        self._base_t = t
        self._slot = 1
        self._pending = None

    def offer(self, t: float, sample: Any) -> Any | None:
        """Returns the sample to keep, or None while waiting for the next slot."""
        if self.interval_s <= 0:
            return sample
        if self._base_t is None:
            self._start(t)
            return sample
        if t < self._next_t - self.interval_s * 2:
            # Clock went backwards (flashback / segment restart): re-grid.
            self._start(t)
            return sample
        if t < self._next_t:
            self._pending = (t, sample)
            return None
        target = self._next_t
        chosen = sample
        if self._pending is not None and abs(self._pending[0] - target) < abs(t - target):
            chosen = self._pending[1]
        self._pending = None
        while self._next_t <= t:
            self._slot += 1
        return chosen


class RowBuilder:
    """Turns parsed packets plus lifecycle callbacks into DB row dicts."""

    def __init__(self, cfg: Config, emit: EmitRow) -> None:
        self.cfg = cfg
        self.emit = emit
        self.rows_emitted = 0

        self._key: SessionKey | None = None
        self._generation = 0
        self._joined_in_progress = False
        self._started_wall: float | None = None
        self._last_wall: float | None = None
        self._raw_file: str | None = None
        self._packet_format = 0
        self._player_index = 0

        self._session_view: SessionView | None = None
        self._classification: ClassificationView | None = None
        self._telemetry: TelemetryView | None = None
        self._status: StatusView | None = None
        self._damage: DamageView | None = None
        self._motion: MotionView | None = None
        self._lap_view: LapView | None = None

        self._telemetry_decimator = NearestDecimator(
            1.0 / cfg.telemetry_hz if cfg.telemetry_hz > 0 else 0.0
        )
        self._wear_decimator = NearestDecimator(1.0 / cfg.wear_hz if cfg.wear_hz > 0 else 0.0)

        self._last_session_row: dict[str, Any] | None = None
        self._participants_sent: dict[int, tuple[Any, ...]] = {}
        self._reset_session_scoped()

    # -- lifecycle (wired to SessionCallbacks) -------------------------------

    def on_session_open(self, key: SessionKey, joined_in_progress: bool) -> None:
        self._key = key
        self._generation = 0
        self._joined_in_progress = joined_in_progress
        self._started_wall = None
        self._reset_session_scoped()

    def on_segment(self, key: SessionKey, segment: int) -> None:
        self._close_stint("session_end")
        self._finish_session_row("superseded")
        self._key = key
        self._started_wall = self._last_wall
        self._reset_session_scoped()
        self._emit_session_row()

    def on_generation(self, generation: int, rewind_to_session_time: float) -> None:
        self._generation = generation
        self._telemetry_decimator.reset()
        self._wear_decimator.reset()
        if abs(rewind_to_session_time - self._last_flbk_t) > _FLBK_DEDUPE_S:
            self._last_flbk_t = rewind_to_session_time
            self._emit_event_row(
                code="FLBK",
                session_time_s=rewind_to_session_time,
                frame_id=None,
                details={
                    "to_session_time_s": rewind_to_session_time,
                    "generation": generation,
                    "synthetic": True,
                },
            )

    def on_session_close(self, key: SessionKey, reason: CloseReason) -> None:
        self._close_stint("session_end")
        self._finish_session_row(reason)
        self._key = None

    def on_lap(self, key: SessionKey, record: LapRecord, corrected: bool) -> None:
        """Emit (or re-emit, after history reconciliation) one `laps` row."""
        context = _EMPTY_LAP_CONTEXT
        if record.car_index == self._player_index:
            if record.lap_number not in self._lap_context and record.lap_number == self._player_lap:
                # The tracker's ledger sees the crossing one call before our own
                # feed() does, so the per-lap accumulators are still the finished
                # lap's. Snapshot them here rather than shipping an empty row.
                self._capture_lap_context(
                    record.lap_number, self._penalties.get(record.car_index, 0)
                )
            context = self._lap_context.get(record.lap_number, _EMPTY_LAP_CONTEXT)
        penalties = context["penalties_s"]
        if penalties is None:
            penalties = self._penalties.get(record.car_index)
        row = {
            "session_key_uid": key.uid_str,
            "segment": key.segment,
            "car_index": record.car_index,
            "lap_number": record.lap_number,
            "generation": self._generation,
            "lap_time_ms": record.lap_time_ms,
            "s1_ms": record.s1_ms or None,
            "s2_ms": record.s2_ms or None,
            "s3_ms": record.s3_ms or None,
            "valid": record.valid,
            "compound_actual": context["compound_actual"],
            "compound_visual": context["compound_visual"],
            "tyre_age_laps": context["tyre_age_laps"],
            "fuel_start_kg": context["fuel_start_kg"],
            "fuel_end_kg": context["fuel_end_kg"],
            "ers_deployed_j": context["ers_deployed_j"],
            "ers_harvested_j": context["ers_harvested_j"],
            "top_speed_kmh": context["top_speed_kmh"],
            "penalties_s": penalties,
            "wall_ts": self._last_wall,
        }
        self._emit("laps", row)

    def stats(self) -> dict[str, float | int | str]:
        """Flat counters for `/metrics` (satisfies web.interfaces.StatsSource)."""
        return {
            "rows_emitted_total": self.rows_emitted,
            "laps_tracked": len(self._lap_context),
            "stint_no": self._stint_no,
        }

    def set_raw_file(self, path: str | None) -> None:
        """Told by the raw logger which capture file this segment is landing in."""
        self._raw_file = path
        if self._key is not None and self._started_wall is not None:
            self._emit_session_row()

    # -- packet fan-in -------------------------------------------------------

    def feed(self, pkt: ParsedPacket) -> None:
        header = pkt.header
        self._last_wall = pkt.recv_wall_ns / 1e9
        self._packet_format = header.packet_format
        self._player_index = header.player_car_index
        if self._key is None:
            return
        if self._started_wall is None:
            self._started_wall = self._last_wall
            self._emit_session_row()

        view = pkt.view
        if view is None:
            return
        if isinstance(view, TelemetryView):
            self._on_telemetry(pkt, view)
        elif isinstance(view, LapView):
            self._on_lap_view(view)
        elif isinstance(view, StatusView):
            self._on_status(view)
        elif isinstance(view, DamageView):
            self._on_damage(pkt, view)
        elif isinstance(view, MotionView):
            self._motion = view
        elif isinstance(view, SessionView):
            self._session_view = view
            self._emit_session_row()
        elif isinstance(view, ParticipantsView):
            self._on_participants(view)
        elif isinstance(view, EventView):
            self._on_event(pkt, view)
        elif isinstance(view, ClassificationView):
            self._classification = view
            self._emit_session_row()

    # -- internals -----------------------------------------------------------

    def _reset_session_scoped(self) -> None:
        self._session_view = None
        self._classification = None
        self._last_session_row = None
        self._participants_sent = {}
        self._telemetry_decimator.reset()
        self._wear_decimator.reset()
        self._lap_context: dict[int, dict[str, Any]] = {}
        self._penalties: dict[int, int] = {}
        self._player_lap = 0
        self._player_pit_status = 0
        self._top_speed = 0.0
        self._fuel_max: float | None = None
        self._fuel_min: float | None = None
        self._ers_deployed: float | None = None
        self._ers_harvested: float | None = None
        self._stint: dict[str, Any] | None = None
        self._stint_no = 0
        self._stint_age = 0
        self._last_flbk_t = -1e9

    def _emit(self, table: str, row: dict[str, Any]) -> None:
        self.rows_emitted += 1
        self.emit(table, row)

    # sessions ---------------------------------------------------------------

    def _build_session_row(
        self, ended_at_wall: float | None, ended_reason: str | None
    ) -> dict[str, Any] | None:
        key = self._key
        if key is None:
            return None
        view = self._session_view
        weather: dict[str, Any] = {}
        if view is not None:
            weather = {
                "weather": view.weather,
                "track_temp_c": view.track_temp_c,
                "air_temp_c": view.air_temp_c,
                "forecast": [asdict(sample) for sample in view.forecast],
            }
        classification = None
        if self._classification is not None:
            classification = {"rows": [asdict(row) for row in self._classification.rows]}
        track_id = view.track_id if view else -1
        session_type = view.session_type if view else 0
        return {
            "session_uid": key.uid_str,
            "segment": key.segment,
            "packet_format": self._packet_format,
            "session_type": session_type,
            "session_type_name": session_type_name(session_type),
            "track_id": track_id,
            "track_name": resolve_track_name(track_id),
            "started_at_wall": self._started_wall,
            "ended_at_wall": ended_at_wall,
            "ended_reason": ended_reason,
            "joined_in_progress": self._joined_in_progress,
            "player_car_index": self._player_index,
            "total_laps": view.total_laps if view else None,
            "session_duration_s": view.session_duration_s if view else None,
            "weather_json": weather,
            "final_classification_json": classification,
            "raw_file": self._raw_file,
        }

    def _emit_session_row(self) -> None:
        row = self._build_session_row(None, None)
        if row is None or row == self._last_session_row:
            return
        self._last_session_row = row
        self._emit("sessions", dict(row))

    def _finish_session_row(self, reason: str) -> None:
        row = self._build_session_row(self._last_wall, reason)
        if row is None:
            return
        self._last_session_row = row
        self._emit("sessions", dict(row))

    # per-view ---------------------------------------------------------------

    def _on_participants(self, view: ParticipantsView) -> None:
        key = self._key
        assert key is not None
        for participant in view.cars:
            signature = (
                participant.name,
                participant.team_id,
                participant.race_number,
                participant.is_ai,
            )
            if self._participants_sent.get(participant.car_index) == signature:
                continue
            self._participants_sent[participant.car_index] = signature
            self._emit(
                "participants",
                {
                    "session_key_uid": key.uid_str,
                    "segment": key.segment,
                    "car_index": participant.car_index,
                    "name": participant.name,
                    "team_id": participant.team_id,
                    "race_number": participant.race_number,
                    "is_ai": participant.is_ai,
                    "is_player": participant.car_index == self._player_index,
                },
            )

    def _on_status(self, view: StatusView) -> None:
        self._status = view
        fuel = view.fuel_in_tank_kg
        self._fuel_max = fuel if self._fuel_max is None else max(self._fuel_max, fuel)
        self._fuel_min = fuel if self._fuel_min is None else min(self._fuel_min, fuel)
        # Per-lap ERS counters reset at the line, so the max over the lap is the
        # end-of-lap total regardless of which packet we catch at the crossing.
        if view.ers_deployed_lap_j is not None:
            self._ers_deployed = (
                view.ers_deployed_lap_j
                if self._ers_deployed is None
                else max(self._ers_deployed, view.ers_deployed_lap_j)
            )
        if view.ers_harvested_lap_j is not None:
            self._ers_harvested = (
                view.ers_harvested_lap_j
                if self._ers_harvested is None
                else max(self._ers_harvested, view.ers_harvested_lap_j)
            )
        self._update_stint(view)

    def _on_telemetry(self, pkt: ParsedPacket, view: TelemetryView) -> None:
        self._telemetry = view
        self._top_speed = max(self._top_speed, view.speed_kmh)
        row = self._build_telemetry_row(pkt, view)
        chosen = self._telemetry_decimator.offer(pkt.header.session_time, row)
        if chosen is not None:
            self._emit("telemetry_samples", chosen)

    def _build_telemetry_row(self, pkt: ParsedPacket, view: TelemetryView) -> dict[str, Any]:
        key = self._key
        assert key is not None
        header = pkt.header
        player = self._lap_view.player if self._lap_view is not None else None
        status = self._status
        motion = self._motion
        if view.aero_mode is not None:
            drs_or_aero = int(view.aero_mode)
        else:
            drs_or_aero = 1 if view.drs_open else 0
        energy_store = view.energy_store_j
        if energy_store is None and status is not None:
            energy_store = status.ers_store_j
        deploy_mode = view.energy_deploy_mode
        if deploy_mode is None and status is not None:
            deploy_mode = status.ers_deploy_mode
        return {
            "session_key_uid": key.uid_str,
            "segment": key.segment,
            "generation": self._generation,
            "session_time_s": header.session_time,
            "frame_id": header.frame_identifier,
            "overall_frame_id": header.overall_frame_identifier,
            "lap_number": player.lap_number if player else None,
            "lap_distance_m": player.lap_distance_m if player else None,
            "speed_kmh": view.speed_kmh,
            "throttle": view.throttle,
            "brake": view.brake,
            "steer": view.steer,
            "gear": view.gear,
            "rpm": view.rpm,
            "drs_or_aero": drs_or_aero,
            "tyre_surface_temp": wheels(view.tyre_surface_temp),
            "tyre_inner_temp": wheels(view.tyre_inner_temp),
            "tyre_pressure": wheels(view.tyre_pressure),
            "brake_temp": wheels(view.brake_temp),
            "fuel_kg": status.fuel_in_tank_kg if status else None,
            "energy_store_j": energy_store,
            "energy_deploy_mode": deploy_mode,
            "world_x": motion.world_x if motion else None,
            "world_z": motion.world_z if motion else None,
        }

    def _on_damage(self, pkt: ParsedPacket, view: DamageView) -> None:
        key = self._key
        assert key is not None
        self._damage = view
        row = {
            "session_key_uid": key.uid_str,
            "segment": key.segment,
            "session_time_s": pkt.header.session_time,
            "lap_number": self._player_lap or None,
            "tyre_wear_pct": wheels(view.tyre_wear_pct),
            "damage_json": {
                "tyre_damage_pct": wheels(view.tyre_damage_pct),
                "brake_damage_pct": wheels(view.brake_damage_pct),
                "front_left_wing_pct": view.front_left_wing_pct,
                "front_right_wing_pct": view.front_right_wing_pct,
                "rear_wing_pct": view.rear_wing_pct,
                "floor_pct": view.floor_pct,
                "diffuser_pct": view.diffuser_pct,
                "sidepod_pct": view.sidepod_pct,
                "gearbox_pct": view.gearbox_pct,
                "engine_pct": view.engine_pct,
            },
        }
        chosen = self._wear_decimator.offer(pkt.header.session_time, row)
        if chosen is not None:
            self._emit("wear_samples", chosen)

    def _on_event(self, pkt: ParsedPacket, view: EventView) -> None:
        code = view.code.upper()
        if code == "FLBK":
            self._last_flbk_t = pkt.header.session_time
        self._emit_event_row(
            code=code,
            session_time_s=pkt.header.session_time,
            frame_id=pkt.header.frame_identifier,
            details=dict(view.details),
        )

    def _emit_event_row(
        self,
        *,
        code: str,
        session_time_s: float,
        frame_id: int | None,
        details: dict[str, Any],
    ) -> None:
        key = self._key
        if key is None:
            return
        self._emit(
            "events",
            {
                "session_key_uid": key.uid_str,
                "segment": key.segment,
                "session_time_s": session_time_s,
                "frame_id": frame_id,
                "wall_ts": self._last_wall,
                "code": code,
                "details_json": details,
            },
        )

    def _on_lap_view(self, view: LapView) -> None:
        self._lap_view = view
        for car in view.cars:
            self._penalties[car.car_index] = car.penalties_s
        player = view.player
        self._penalties[player.car_index] = player.penalties_s
        self._player_pit_status = player.pit_status
        if self._player_lap == 0:
            self._player_lap = player.lap_number
            return
        if player.lap_number > self._player_lap:
            self._capture_lap_context(self._player_lap, player.penalties_s)
            self._player_lap = player.lap_number
        elif player.lap_number < self._player_lap:
            self._player_lap = player.lap_number
            self._reset_lap_accumulators()

    def _capture_lap_context(self, lap_number: int, penalties_s: int) -> None:
        if lap_number in self._lap_context:
            # Already snapshotted from on_lap(); only the roll-over is left to do.
            self._reset_lap_accumulators()
            return
        status = self._status
        self._lap_context[lap_number] = {
            "compound_actual": status.tyre_compound_actual if status else None,
            "compound_visual": status.tyre_compound_visual if status else None,
            "tyre_age_laps": status.tyre_age_laps if status else None,
            "fuel_start_kg": self._fuel_max,
            "fuel_end_kg": self._fuel_min,
            "ers_deployed_j": self._ers_deployed,
            "ers_harvested_j": self._ers_harvested,
            "top_speed_kmh": self._top_speed or None,
            "penalties_s": penalties_s,
        }
        self._reset_lap_accumulators()

    def _reset_lap_accumulators(self) -> None:
        self._top_speed = 0.0
        fuel = self._status.fuel_in_tank_kg if self._status else None
        self._fuel_max = fuel
        self._fuel_min = fuel
        self._ers_deployed = None
        self._ers_harvested = None

    # tyre stints ------------------------------------------------------------

    def _update_stint(self, status: StatusView) -> None:
        if self._stint is None:
            self._open_stint(status)
            return
        changed = (
            status.tyre_compound_actual != self._stint["compound_actual"]
            or status.tyre_compound_visual != self._stint["compound_visual"]
            or status.tyre_age_laps < self._stint_age
        )
        if changed:
            self._close_stint("pit_stop" if self._player_pit_status != 0 else "compound_change")
            self._open_stint(status)
        self._stint_age = status.tyre_age_laps

    def _open_stint(self, status: StatusView) -> None:
        self._stint_no += 1
        self._stint_age = status.tyre_age_laps
        self._stint = {
            "compound_actual": status.tyre_compound_actual,
            "compound_visual": status.tyre_compound_visual,
            "stint_no": self._stint_no,
            "lap_start": max(1, self._player_lap),
        }
        self._emit_stint_row(lap_end=None, wear=None, end_reason=None)

    def _close_stint(self, end_reason: str) -> None:
        if self._stint is None:
            return
        wear = wheels(self._damage.tyre_wear_pct) if self._damage is not None else None
        self._emit_stint_row(
            lap_end=self._player_lap or None,
            wear={"tyre_wear_pct": wear} if wear is not None else None,
            end_reason=end_reason,
        )
        self._stint = None

    def _emit_stint_row(
        self, *, lap_end: int | None, wear: dict[str, Any] | None, end_reason: str | None
    ) -> None:
        key = self._key
        stint = self._stint
        if key is None or stint is None:
            return
        self._emit(
            "tyre_stints",
            {
                "session_key_uid": key.uid_str,
                "segment": key.segment,
                "car_index": self._player_index,
                "stint_no": stint["stint_no"],
                "compound_actual": stint["compound_actual"],
                "compound_visual": stint["compound_visual"],
                "lap_start": stint["lap_start"],
                "lap_end": lap_end,
                "wear_at_end_json": wear,
                "end_reason": end_reason,
            },
        )

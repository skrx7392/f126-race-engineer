"""Per-packet decoders: raw datagram -> the normalized views in ``types.py``.

Two decode strategies, per the packet's cost:

* The player's car is decoded in full, via one ``unpack_from`` at
  ``header + stride * player_car_index``.
* The 22/24-car arrays are swept with a *single* precompiled
  :class:`struct.Struct` that skips, with pad codes, every field no view needs.
  For CarTelemetry that means 2 bytes read per opponent (speed) out of 59-60;
  for LapData 46 out of 57. Motion decodes the player only.

Decoders raise :class:`ParseError` on anything malformed; the dispatcher in
``__init__`` catches and counts. They never mutate parser state -- the 2026
cross-packet merges are handled by the caller, which passes the merged values
in as :class:`Extras2026`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, NamedTuple

from ..types import (
    CarLap,
    ClassificationRow,
    ClassificationView,
    DamageView,
    EventView,
    HistoryLap,
    HistoryView,
    LapView,
    MotionView,
    PacketHeader,
    ParseError,
    ParticipantsView,
    ParticipantView,
    SessionView,
    StatusView,
    TelemetryView,
    TimeTrialSet,
    TimeTrialView,
    WeatherSample,
    WheelSet,
)
from .spec_2025 import (
    HEADER_SIZE,
    MAX_FORECAST_SAMPLES,
    MAX_HISTORY_LAPS,
    FormatSpec,
)
from .spec_2026 import car_telemetry_2_table

__all__ = [
    "CarTelemetry2Player",
    "Extras2026",
    "StatusResult",
    "decode_car_telemetry_2",
    "decode_classification",
    "decode_damage",
    "decode_event",
    "decode_history",
    "decode_lap",
    "decode_motion",
    "decode_participants",
    "decode_session",
    "decode_status",
    "decode_telemetry",
    "decode_time_trial",
]

_INVALID_CAR_INDEX = 255
#: A sector/delta "minutes" byte multiplies out to this many milliseconds.
_MS_PER_MINUTE = 60_000


@dataclass(slots=True)
class Extras2026:
    """2026-only values that reach ``TelemetryView`` from *other* packets.

    ``aero_mode`` arrives on CarTelemetry2 (id 16); the energy quartet arrives
    on CarStatus (id 7), which ``types.py`` routes into ``TelemetryView`` for
    2026 (``StatusView.ers_*`` is ``None`` there). All stay ``None`` until the
    corresponding packet has been seen at least once in the current session.
    """

    aero_mode: int | None = None
    energy_store_j: float | None = None
    energy_deploy_mode: int | None = None
    energy_harvested_lap_j: float | None = None
    energy_deployed_lap_j: float | None = None


class StatusResult(NamedTuple):
    """``decode_status`` output plus the raw energy values for the 2026 cache."""

    view: StatusView
    ers_store_j: float
    ers_deploy_mode: int
    ers_harvested_lap_j: float
    ers_deployed_lap_j: float


@dataclass(slots=True)
class CarTelemetry2Player:
    """Player-car slice of the 2026-only CarTelemetry2 packet (id 16)."""

    aero_mode: int
    aero_available: bool
    aero_activation_distance: int
    overtake_available: bool
    overtake_active: bool
    overtake_activation_distance: int
    regulations_2026_applicable: bool
    is_driving_wrong_way: bool


def _player_index(spec: FormatSpec, header: PacketHeader) -> int:
    """Validate and return the player car index for this packet."""
    index = header.player_car_index
    if not 0 <= index < spec.num_cars:
        raise ParseError(
            packet_id=header.packet_id,
            reason=(
                f"player_car_index {index} out of range for format "
                f"{spec.packet_format} ({spec.num_cars} cars)"
            ),
        )
    return index


def _unpack(
    fmt: struct.Struct, data: bytes, offset: int, header: PacketHeader
) -> tuple[Any, ...]:
    """``unpack_from`` that reports short buffers as :class:`ParseError`."""
    try:
        return fmt.unpack_from(data, offset)
    except struct.error as exc:
        raise ParseError(
            packet_id=header.packet_id,
            reason=f"truncated at offset {offset} (need {fmt.size}B): {exc}",
        ) from exc


def _wheelset(values, idx, prefix: str) -> WheelSet:
    return WheelSet(
        rl=values[idx[f"{prefix}_rl"]],
        rr=values[idx[f"{prefix}_rr"]],
        fl=values[idx[f"{prefix}_fl"]],
        fr=values[idx[f"{prefix}_fr"]],
    )


def _name(raw: bytes) -> str:
    """Decode a fixed-width, NUL-terminated UTF-8 participant name."""
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# id 0 -- Motion
# --------------------------------------------------------------------------


def decode_motion(spec: FormatSpec, data: bytes, header: PacketHeader) -> MotionView:
    """Player car only: position, lateral/longitudinal g, yaw."""
    index = _player_index(spec, header)
    offset = HEADER_SIZE + spec.motion_stride * index
    values = _unpack(spec.motion_player.one, data, offset, header)
    idx = spec.motion_player.idx
    # 2026 packs the g-forces as int16 fixed point; 2025 sends float32 and the
    # divisor is 1.0. Sign convention is EA's and is passed through unchanged
    # so both formats agree downstream.
    divisor = spec.g_force_divisor
    return MotionView(
        world_x=values[idx["world_position_x"]],
        world_z=values[idx["world_position_z"]],
        world_y=values[idx["world_position_y"]],
        g_lat=values[idx["g_force_lateral"]] / divisor,
        g_lon=values[idx["g_force_longitudinal"]] / divisor,
        yaw=values[idx["yaw"]],
    )


# --------------------------------------------------------------------------
# id 1 -- Session
# --------------------------------------------------------------------------


def decode_session(spec: FormatSpec, data: bytes, header: PacketHeader) -> SessionView:
    head = _unpack(spec.session_head.one, data, HEADER_SIZE, header)
    hidx = spec.session_head.idx
    mid = _unpack(spec.session_mid.one, data, spec.session_mid_offset, header)
    midx = spec.session_mid.idx

    num_samples = min(mid[midx["num_weather_forecast_samples"]], MAX_FORECAST_SAMPLES)
    sample = spec.forecast_sample
    sidx = sample.idx
    forecast: list[WeatherSample] = []
    for i in range(num_samples):
        offset = spec.forecast_offset + sample.size * i
        values = _unpack(sample.one, data, offset, header)
        forecast.append(
            WeatherSample(
                offset_min=values[sidx["time_offset"]],
                weather=values[sidx["weather"]],
                track_temp_c=values[sidx["track_temperature"]],
                air_temp_c=values[sidx["air_temperature"]],
                rain_percentage=values[sidx["rain_percentage"]],
            )
        )

    return SessionView(
        session_type=head[hidx["session_type"]],
        track_id=head[hidx["track_id"]],
        track_length_m=head[hidx["track_length"]],
        session_time_left_s=head[hidx["session_time_left"]],
        session_duration_s=head[hidx["session_duration"]],
        total_laps=head[hidx["total_laps"]],
        weather=head[hidx["weather"]],
        track_temp_c=head[hidx["track_temperature"]],
        air_temp_c=head[hidx["air_temperature"]],
        safety_car_status=mid[midx["safety_car_status"]],
        pit_speed_limit_kmh=head[hidx["pit_speed_limit"]],
        forecast=forecast,
    )


# --------------------------------------------------------------------------
# id 2 -- LapData
# --------------------------------------------------------------------------


def decode_lap(spec: FormatSpec, data: bytes, header: PacketHeader) -> LapView:
    """All cars, in one ``unpack_from`` over the whole 22/24-car array."""
    index = _player_index(spec, header)
    values = _unpack(spec.lap_all, data, HEADER_SIZE, header)
    idx = spec.lap_carlap.idx
    stride = spec.lap_carlap.count

    # Bind the per-car field positions once rather than per iteration.
    p_last = idx["last_lap_time_ms"]
    p_current = idx["current_lap_time_ms"]
    p_s1_ms = idx["sector1_time_ms_part"]
    p_s1_min = idx["sector1_time_minutes_part"]
    p_s2_ms = idx["sector2_time_ms_part"]
    p_s2_min = idx["sector2_time_minutes_part"]
    p_front_ms = idx["delta_to_car_in_front_ms_part"]
    p_front_min = idx["delta_to_car_in_front_minutes_part"]
    p_leader_ms = idx["delta_to_race_leader_ms_part"]
    p_leader_min = idx["delta_to_race_leader_minutes_part"]
    p_lap_distance = idx["lap_distance"]
    p_total_distance = idx["total_distance"]
    p_position = idx["car_position"]
    p_lap_num = idx["current_lap_num"]
    p_pit_status = idx["pit_status"]
    p_sector = idx["sector"]
    p_invalid = idx["current_lap_invalid"]
    p_penalties = idx["penalties"]
    p_result = idx["result_status"]

    cars: list[CarLap] = []
    for car_index in range(spec.num_cars):
        base = car_index * stride
        cars.append(
            CarLap(
                car_index=car_index,
                position=values[base + p_position],
                lap_number=values[base + p_lap_num],
                lap_distance_m=values[base + p_lap_distance],
                total_distance_m=values[base + p_total_distance],
                last_lap_ms=values[base + p_last],
                current_lap_ms=values[base + p_current],
                sector=values[base + p_sector],
                sector1_ms=(
                    values[base + p_s1_min] * _MS_PER_MINUTE + values[base + p_s1_ms]
                ),
                sector2_ms=(
                    values[base + p_s2_min] * _MS_PER_MINUTE + values[base + p_s2_ms]
                ),
                lap_invalid=bool(values[base + p_invalid]),
                penalties_s=values[base + p_penalties],
                pit_status=values[base + p_pit_status],
                result_status=values[base + p_result],
                delta_to_car_ahead_ms=(
                    values[base + p_front_min] * _MS_PER_MINUTE
                    + values[base + p_front_ms]
                ),
                delta_to_leader_ms=(
                    values[base + p_leader_min] * _MS_PER_MINUTE
                    + values[base + p_leader_ms]
                ),
            )
        )
    return LapView(player=cars[index], cars=cars)


# --------------------------------------------------------------------------
# id 3 -- Event
# --------------------------------------------------------------------------

_EV_FASTEST_LAP = struct.Struct("<Bf")
_EV_RETIREMENT = struct.Struct("<BB")
_EV_VEHICLE_IDX = struct.Struct("<B")
_EV_PENALTY = struct.Struct("<7B")
_EV_SPEED_TRAP = struct.Struct("<BfBBBf")
_EV_STOP_GO = struct.Struct("<Bf")
_EV_FLASHBACK = struct.Struct("<If")
_EV_BUTTONS = struct.Struct("<I")
_EV_OVERTAKE = struct.Struct("<BB")
_EV_SAFETY_CAR = struct.Struct("<BB")
_EV_COLLISION_2025 = struct.Struct("<BB")
_EV_COLLISION_2026 = struct.Struct("<BBB")


def decode_event(spec: FormatSpec, data: bytes, header: PacketHeader) -> EventView:
    """4-char code plus the union payload interpreted for that code.

    Unrecognised codes yield an ``EventView`` with an empty ``details`` rather
    than an error -- a future game patch adding an event must not break capture.
    """
    end = HEADER_SIZE + 4
    if len(data) < end:
        raise ParseError(packet_id=header.packet_id, reason="event packet has no code")
    code = data[HEADER_SIZE:end].decode("ascii", errors="replace")
    payload = end
    details: dict[str, float | int | str] = {}

    match code:
        case "FTLP":
            vehicle_idx, lap_time = _unpack(_EV_FASTEST_LAP, data, payload, header)
            details = {"vehicle_idx": vehicle_idx, "lap_time_s": lap_time}
        case "RTMT":
            vehicle_idx, reason = _unpack(_EV_RETIREMENT, data, payload, header)
            details = {"vehicle_idx": vehicle_idx, "reason": reason}
        case "DRSD":
            (reason,) = _unpack(_EV_VEHICLE_IDX, data, payload, header)
            details = {"reason": reason}
        case "TMPT" | "RCWN" | "DTSV":
            (vehicle_idx,) = _unpack(_EV_VEHICLE_IDX, data, payload, header)
            details = {"vehicle_idx": vehicle_idx}
        case "PENA":
            (
                penalty_type,
                infringement_type,
                vehicle_idx,
                other_vehicle_idx,
                time_s,
                lap_num,
                places_gained,
            ) = _unpack(_EV_PENALTY, data, payload, header)
            details = {
                "penalty_type": penalty_type,
                "infringement_type": infringement_type,
                "vehicle_idx": vehicle_idx,
                "other_vehicle_idx": other_vehicle_idx,
                "time_s": time_s,
                "lap_num": lap_num,
                "places_gained": places_gained,
            }
        case "SPTP":
            (
                vehicle_idx,
                speed,
                overall_fastest,
                driver_fastest,
                fastest_vehicle_idx,
                fastest_speed,
            ) = _unpack(_EV_SPEED_TRAP, data, payload, header)
            details = {
                "vehicle_idx": vehicle_idx,
                "speed_kmh": speed,
                "overall_fastest_in_session": overall_fastest,
                "driver_fastest_in_session": driver_fastest,
                "fastest_vehicle_idx_in_session": fastest_vehicle_idx,
                "fastest_speed_in_session": fastest_speed,
            }
        case "STLG":
            (num_lights,) = _unpack(_EV_VEHICLE_IDX, data, payload, header)
            details = {"num_lights": num_lights}
        case "SGSV":
            vehicle_idx, stop_time = _unpack(_EV_STOP_GO, data, payload, header)
            details = {"vehicle_idx": vehicle_idx, "stop_time_s": stop_time}
        case "FLBK":
            frame_identifier, session_time = _unpack(_EV_FLASHBACK, data, payload, header)
            details = {
                "flashback_frame_identifier": frame_identifier,
                "flashback_session_time": session_time,
            }
        case "BUTN":
            (button_status,) = _unpack(_EV_BUTTONS, data, payload, header)
            details = {"button_status": button_status}
        case "OVTK":
            overtaking, being_overtaken = _unpack(_EV_OVERTAKE, data, payload, header)
            details = {
                "overtaking_vehicle_idx": overtaking,
                "being_overtaken_vehicle_idx": being_overtaken,
            }
        case "SCAR":
            safety_car_type, event_type = _unpack(_EV_SAFETY_CAR, data, payload, header)
            details = {"safety_car_type": safety_car_type, "event_type": event_type}
        case "COLL":
            # The 2026 Season Pack appended a severity byte to this payload.
            if spec.collision_has_severity:
                vehicle1, vehicle2, severity = _unpack(
                    _EV_COLLISION_2026, data, payload, header
                )
                details = {
                    "vehicle1_idx": vehicle1,
                    "vehicle2_idx": vehicle2,
                    "severity": severity,
                }
            else:
                vehicle1, vehicle2 = _unpack(_EV_COLLISION_2025, data, payload, header)
                details = {"vehicle1_idx": vehicle1, "vehicle2_idx": vehicle2}
        case _:
            # SSTA, SEND, DRSE, CHQF, LGOT, RDFL carry no payload; anything
            # else is a code this build does not know about.
            details = {}

    return EventView(code=code, details=details)


# --------------------------------------------------------------------------
# id 4 -- Participants
# --------------------------------------------------------------------------


def decode_participants(
    spec: FormatSpec, data: bytes, header: PacketHeader
) -> ParticipantsView:
    """All car slots, so list index == car_index; ``num_active`` says how many
    of them are real."""
    if len(data) <= HEADER_SIZE:
        raise ParseError(packet_id=header.packet_id, reason="participants packet is empty")
    num_active = data[HEADER_SIZE]
    base = HEADER_SIZE + 1
    entry = spec.participant
    idx = entry.idx
    cars: list[ParticipantView] = []
    for car_index in range(spec.num_cars):
        values = _unpack(entry.one, data, base + spec.participant_stride * car_index, header)
        cars.append(
            ParticipantView(
                car_index=car_index,
                name=_name(values[idx["name"]]),
                team_id=values[idx["team_id"]],
                race_number=values[idx["race_number"]],
                is_ai=bool(values[idx["ai_controlled"]]),
            )
        )
    return ParticipantsView(num_active=num_active, cars=cars)


# --------------------------------------------------------------------------
# id 6 -- CarTelemetry
# --------------------------------------------------------------------------


def decode_telemetry(
    spec: FormatSpec,
    data: bytes,
    header: PacketHeader,
    extras: Extras2026 | None = None,
) -> TelemetryView:
    """Full decode of the player car; opponents contribute speed only."""
    index = _player_index(spec, header)
    entry = spec.telemetry_car
    values = _unpack(
        entry.one, data, HEADER_SIZE + spec.telemetry_stride * index, header
    )
    idx = entry.idx
    speeds = _unpack(spec.telemetry_speeds, data, HEADER_SIZE, header)

    is_2026 = spec.packet_format == 2026
    extras = extras or Extras2026()
    return TelemetryView(
        speed_kmh=float(values[idx["speed"]]),
        throttle=values[idx["throttle"]],
        brake=values[idx["brake"]],
        steer=values[idx["steer"]],
        gear=values[idx["gear"]],
        rpm=values[idx["engine_rpm"]],
        # 2026 replaces DRS with active aero, so the DRS bit is not meaningful.
        drs_open=None if is_2026 else bool(values[idx["drs"]]),
        aero_mode=extras.aero_mode if is_2026 else None,
        tyre_surface_temp=_wheelset(values, idx, "tyres_surface_temperature"),
        tyre_inner_temp=_wheelset(values, idx, "tyres_inner_temperature"),
        tyre_pressure=_wheelset(values, idx, "tyres_pressure"),
        brake_temp=_wheelset(values, idx, "brakes_temperature"),
        engine_temp=float(values[idx["engine_temperature"]]),
        rev_lights_percent=values[idx["rev_lights_percent"]],
        opponent_speeds_kmh=[float(speed) for speed in speeds],
        energy_store_j=extras.energy_store_j if is_2026 else None,
        energy_deploy_mode=extras.energy_deploy_mode if is_2026 else None,
        energy_harvested_lap_j=extras.energy_harvested_lap_j if is_2026 else None,
        energy_deployed_lap_j=extras.energy_deployed_lap_j if is_2026 else None,
    )


# --------------------------------------------------------------------------
# id 7 -- CarStatus
# --------------------------------------------------------------------------


def decode_status(spec: FormatSpec, data: bytes, header: PacketHeader) -> StatusResult:
    """Player car only.

    On format 2026 the ERS fields are reported as ``None`` on ``StatusView``:
    ``types.py`` puts the 2026 energy system on ``TelemetryView`` instead. The
    raw values still come back on the :class:`StatusResult` so the dispatcher
    can forward them there.
    """
    index = _player_index(spec, header)
    entry = spec.status_car
    values = _unpack(entry.one, data, HEADER_SIZE + spec.status_stride * index, header)
    idx = entry.idx

    ers_store_j = values[idx["ers_store_energy"]]
    ers_deploy_mode = values[idx["ers_deploy_mode"]]
    harvested = (
        values[idx["ers_harvested_this_lap_mguk"]]
        + values[idx["ers_harvested_this_lap_mguh"]]
    )
    deployed = values[idx["ers_deployed_this_lap"]]
    fia_flags = values[idx["vehicle_fia_flags"]]
    is_2026 = spec.packet_format == 2026

    view = StatusView(
        fuel_in_tank_kg=values[idx["fuel_in_tank"]],
        fuel_capacity_kg=values[idx["fuel_capacity"]],
        fuel_remaining_laps=values[idx["fuel_remaining_laps"]],
        tyre_compound_actual=values[idx["actual_tyre_compound"]],
        tyre_compound_visual=values[idx["visual_tyre_compound"]],
        tyre_age_laps=values[idx["tyres_age_laps"]],
        fia_flags=fia_flags,
        ers_store_j=None if is_2026 else ers_store_j,
        ers_deploy_mode=None if is_2026 else ers_deploy_mode,
        ers_harvested_lap_j=None if is_2026 else harvested,
        ers_deployed_lap_j=None if is_2026 else deployed,
        drs_allowed=bool(values[idx["drs_allowed"]]),
        vehicle_fia_flags=fia_flags,
    )
    return StatusResult(
        view=view,
        ers_store_j=ers_store_j,
        ers_deploy_mode=ers_deploy_mode,
        ers_harvested_lap_j=harvested,
        ers_deployed_lap_j=deployed,
    )


# --------------------------------------------------------------------------
# id 8 -- FinalClassification
# --------------------------------------------------------------------------


def decode_classification(
    spec: FormatSpec, data: bytes, header: PacketHeader
) -> ClassificationView:
    if len(data) <= HEADER_SIZE:
        raise ParseError(
            packet_id=header.packet_id, reason="classification packet is empty"
        )
    num_cars = min(data[HEADER_SIZE], spec.num_cars)
    base = HEADER_SIZE + 1
    entry = spec.classification_row
    idx = entry.idx
    rows: list[ClassificationRow] = []
    for car_index in range(num_cars):
        values = _unpack(
            entry.one, data, base + spec.classification_stride * car_index, header
        )
        rows.append(
            ClassificationRow(
                car_index=car_index,
                position=values[idx["position"]],
                num_laps=values[idx["num_laps"]],
                grid_position=values[idx["grid_position"]],
                points=values[idx["points"]],
                num_pit_stops=values[idx["num_pit_stops"]],
                result_status=values[idx["result_status"]],
                best_lap_ms=values[idx["best_lap_time_ms"]],
                total_race_time_s=values[idx["total_race_time"]],
                penalties_s=values[idx["penalties_time"]],
            )
        )
    return ClassificationView(rows=rows)


# --------------------------------------------------------------------------
# id 10 -- CarDamage
# --------------------------------------------------------------------------


def decode_damage(spec: FormatSpec, data: bytes, header: PacketHeader) -> DamageView:
    index = _player_index(spec, header)
    entry = spec.damage_car
    values = _unpack(entry.one, data, HEADER_SIZE + spec.damage_stride * index, header)
    idx = entry.idx
    return DamageView(
        tyre_wear_pct=_wheelset(values, idx, "tyres_wear"),
        tyre_damage_pct=_wheelset(values, idx, "tyres_damage"),
        brake_damage_pct=_wheelset(values, idx, "brakes_damage"),
        front_left_wing_pct=values[idx["front_left_wing_damage"]],
        front_right_wing_pct=values[idx["front_right_wing_damage"]],
        rear_wing_pct=values[idx["rear_wing_damage"]],
        floor_pct=values[idx["floor_damage"]],
        diffuser_pct=values[idx["diffuser_damage"]],
        sidepod_pct=values[idx["sidepod_damage"]],
        gearbox_pct=values[idx["gear_box_damage"]],
        engine_pct=values[idx["engine_damage"]],
    )


# --------------------------------------------------------------------------
# id 11 -- SessionHistory
# --------------------------------------------------------------------------


def decode_history(spec: FormatSpec, data: bytes, header: PacketHeader) -> HistoryView:
    head = _unpack(spec.history_head.one, data, HEADER_SIZE, header)
    hidx = spec.history_head.idx
    num_laps = min(head[hidx["num_laps"]], MAX_HISTORY_LAPS)

    entry = spec.history_lap
    idx = entry.idx
    laps: list[HistoryLap] = []
    for lap in range(num_laps):
        values = _unpack(entry.one, data, spec.history_laps_offset + entry.size * lap, header)
        laps.append(
            HistoryLap(
                lap_number=lap + 1,
                lap_time_ms=values[idx["lap_time_ms"]],
                sector1_ms=(
                    values[idx["sector1_time_minutes"]] * _MS_PER_MINUTE
                    + values[idx["sector1_time_ms"]]
                ),
                sector2_ms=(
                    values[idx["sector2_time_minutes"]] * _MS_PER_MINUTE
                    + values[idx["sector2_time_ms"]]
                ),
                sector3_ms=(
                    values[idx["sector3_time_minutes"]] * _MS_PER_MINUTE
                    + values[idx["sector3_time_ms"]]
                ),
                # bit 0 = lap valid; bits 1-3 are the per-sector valid flags.
                valid=bool(values[idx["lap_valid_bit_flags"]] & 0x01),
            )
        )
    return HistoryView(
        car_index=head[hidx["car_idx"]],
        num_laps=head[hidx["num_laps"]],
        best_lap_number=head[hidx["best_lap_time_lap_num"]],
        laps=laps,
    )


# --------------------------------------------------------------------------
# id 14 -- TimeTrial
# --------------------------------------------------------------------------


def decode_time_trial(
    spec: FormatSpec, data: bytes, header: PacketHeader
) -> TimeTrialView:
    entry = spec.time_trial_set
    idx = entry.idx

    def one(slot: int) -> TimeTrialSet | None:
        values = _unpack(
            entry.one, data, HEADER_SIZE + spec.time_trial_stride * slot, header
        )
        car_idx = values[idx["car_idx"]]
        if car_idx == _INVALID_CAR_INDEX:
            return None
        return TimeTrialSet(
            car_index=car_idx,
            team_id=values[idx["team_id"]],
            lap_time_ms=values[idx["lap_time_ms"]],
            sector1_ms=values[idx["sector1_time_ms"]],
            sector2_ms=values[idx["sector2_time_ms"]],
            sector3_ms=values[idx["sector3_time_ms"]],
            valid=bool(values[idx["valid"]]),
        )

    return TimeTrialView(
        player_session_best=one(0), personal_best=one(1), rival=one(2)
    )


# --------------------------------------------------------------------------
# id 16 -- CarTelemetry2 (2026 only)
# --------------------------------------------------------------------------


def decode_car_telemetry_2(
    spec: FormatSpec, data: bytes, header: PacketHeader
) -> CarTelemetry2Player:
    """Player slice of the 2026 active-aero / overtake-mode packet.

    This packet produces no view of its own -- ``TelemetryView`` is emitted by
    CarTelemetry (id 6), which merges the ``aero_mode`` cached from here.
    """
    index = _player_index(spec, header)
    entry = car_telemetry_2_table
    values = _unpack(entry.one, data, HEADER_SIZE + entry.size * index, header)
    idx = entry.idx
    return CarTelemetry2Player(
        aero_mode=values[idx["active_aero_mode"]],
        aero_available=bool(values[idx["active_aero_available"]]),
        aero_activation_distance=values[idx["active_aero_activation_distance"]],
        overtake_available=bool(values[idx["overtake_available"]]),
        overtake_active=bool(values[idx["overtake_active"]]),
        overtake_activation_distance=values[idx["overtake_activation_distance"]],
        regulations_2026_applicable=bool(values[idx["regulations_2026_applicable"]]),
        is_driving_wrong_way=bool(values[idx["is_driving_wrong_way"]]),
    )

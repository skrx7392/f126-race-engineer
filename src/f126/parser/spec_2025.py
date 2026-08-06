"""Wire layout tables for packet format 2025 (base F1 25).

Source of truth: the MacManley/f1-25-udp C headers (``src/*.h``), which are
``#pragma pack(push, 1)`` little-endian structs with no padding. Field names
below mirror the C member names with the ``m_`` prefix dropped and snake_cased.

Everything downstream -- offsets, per-record strides, packet wire sizes -- is
*derived* from these tables at import time rather than hardcoded, so a layout
edit cannot silently disagree with a size constant. The tests pin the derived
sizes against integer literals taken from the reference.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "HEADER_SIZE",
    "MAX_FORECAST_SAMPLES",
    "MAX_HISTORY_LAPS",
    "MAX_MARSHAL_ZONES",
    "MAX_TYRE_SETS",
    "MAX_TYRE_STINTS",
    "SPEC_2025",
    "Field",
    "FormatSpec",
    "Table",
    "repeated",
    "table",
]

HEADER_SIZE = 29

MAX_MARSHAL_ZONES = 21
MAX_FORECAST_SAMPLES = 64
MAX_HISTORY_LAPS = 100
MAX_TYRE_STINTS = 8
MAX_TYRE_SETS = 20
MAX_LAP_POSITION_LAPS = 50
MAX_LIVERY_COLOURS = 4

#: ``(field_name, struct format code)``. Codes are the little-endian, unaligned
#: subset of :mod:`struct`: ``b B h H i I q Q f d`` and ``<n>s``.
Field = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Table:
    """A compiled run of contiguous wire fields.

    ``size`` is the byte length of the fields in this table, which for a table
    describing only the *prefix* of a larger record is shorter than that
    record's stride. ``idx`` maps a field name to its position in the tuple
    returned by ``one.unpack_from``.
    """

    fields: tuple[Field, ...]
    size: int
    count: int
    idx: Mapping[str, int]
    one: struct.Struct

    def at(self, values: Sequence[object], name: str) -> object:
        """Pull ``name`` out of a tuple produced by ``self.one.unpack_from``."""
        return values[self.idx[name]]


def table(fields: Sequence[Field]) -> Table:
    """Compile a field sequence into a :class:`Table`."""
    fields = tuple(fields)
    fmt = "<" + "".join(code for _, code in fields)
    names = [name for name, _ in fields]
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate field names in table: {duplicates}")
    compiled = struct.Struct(fmt)
    return Table(
        fields=fields,
        size=compiled.size,
        count=len(fields),
        idx=MappingProxyType({name: i for i, name in enumerate(names)}),
        one=compiled,
    )


def repeated(entry: Table, count: int, stride: int) -> struct.Struct:
    """One :class:`struct.Struct` reading ``entry`` out of ``count`` records.

    Each record occupies ``stride`` bytes; the trailing ``stride - entry.size``
    bytes are skipped with pad codes. This lets the hot packets (Motion, LapData,
    CarTelemetry) pull every car's fields in a single ``unpack_from`` call
    instead of ``count`` separate calls over fields we do not need.
    """
    pad = stride - entry.size
    if pad < 0:
        raise ValueError(f"entry of {entry.size}B does not fit stride {stride}B")
    unit = "".join(code for _, code in entry.fields) + (f"{pad}x" if pad else "")
    return struct.Struct("<" + unit * count)


def _wheels(prefix: str, code: str) -> list[Field]:
    """Four wheel-ordered fields. Wire order is always RL, RR, FL, FR."""
    return [(f"{prefix}_{corner}", code) for corner in ("rl", "rr", "fl", "fr")]


def _size(fields: Sequence[Field]) -> int:
    return struct.calcsize("<" + "".join(code for _, code in fields))


# --------------------------------------------------------------------------
# Packet header (identical in both formats, 29 bytes)
# --------------------------------------------------------------------------

HEADER_FIELDS: list[Field] = [
    ("packet_format", "H"),
    ("game_year", "B"),
    ("game_major_version", "B"),
    ("game_minor_version", "B"),
    ("packet_version", "B"),
    ("packet_id", "B"),
    ("session_uid", "Q"),
    ("session_time", "f"),
    ("frame_identifier", "I"),
    ("overall_frame_identifier", "I"),
    ("player_car_index", "B"),
    ("secondary_player_car_index", "B"),
]

# --------------------------------------------------------------------------
# id 0 -- Motion / CarMotionData (60 B per car in 2025)
# --------------------------------------------------------------------------

MOTION_CAR_FIELDS: list[Field] = [
    ("world_position_x", "f"),
    ("world_position_y", "f"),
    ("world_position_z", "f"),
    ("world_velocity_x", "f"),
    ("world_velocity_y", "f"),
    ("world_velocity_z", "f"),
    ("world_forward_dir_x", "h"),
    ("world_forward_dir_y", "h"),
    ("world_forward_dir_z", "h"),
    ("world_right_dir_x", "h"),
    ("world_right_dir_y", "h"),
    ("world_right_dir_z", "h"),
    ("g_force_lateral", "f"),
    ("g_force_longitudinal", "f"),
    ("g_force_vertical", "f"),
    ("yaw", "f"),
    ("pitch", "f"),
    ("roll", "f"),
]

#: MotionView needs everything up to and including yaw; pitch/roll are skipped.
MOTION_PLAYER_FIELDS: list[Field] = MOTION_CAR_FIELDS[:16]

# --------------------------------------------------------------------------
# id 1 -- Session
# --------------------------------------------------------------------------

SESSION_HEAD_FIELDS: list[Field] = [
    ("weather", "B"),
    ("track_temperature", "b"),
    ("air_temperature", "b"),
    ("total_laps", "B"),
    ("track_length", "H"),
    ("session_type", "B"),
    ("track_id", "b"),
    ("formula", "B"),
    ("session_time_left", "H"),
    ("session_duration", "H"),
    ("pit_speed_limit", "B"),
    ("game_paused", "B"),
    ("is_spectating", "B"),
    ("spectator_car_index", "B"),
    ("sli_pro_native_support", "B"),
    ("num_marshal_zones", "B"),
]

MARSHAL_ZONE_FIELDS: list[Field] = [("zone_start", "f"), ("zone_flag", "b")]

SESSION_MID_FIELDS: list[Field] = [
    ("safety_car_status", "B"),
    ("network_game", "B"),
    ("num_weather_forecast_samples", "B"),
]

FORECAST_SAMPLE_FIELDS: list[Field] = [
    ("session_type", "B"),
    ("time_offset", "B"),
    ("weather", "B"),
    ("track_temperature", "b"),
    ("track_temperature_change", "b"),
    ("air_temperature", "b"),
    ("air_temperature_change", "b"),
    ("rain_percentage", "B"),
]

#: Everything after the weather forecast array. Not decoded into any view, but
#: enumerated so the Session packet's wire size is derived rather than asserted.
SESSION_TAIL_FIELDS: list[Field] = [
    ("forecast_accuracy", "B"),
    ("ai_difficulty", "B"),
    ("season_link_identifier", "I"),
    ("weekend_link_identifier", "I"),
    ("session_link_identifier", "I"),
    ("pit_stop_window_ideal_lap", "B"),
    ("pit_stop_window_latest_lap", "B"),
    ("pit_stop_rejoin_position", "B"),
    ("steering_assist", "B"),
    ("braking_assist", "B"),
    ("gearbox_assist", "B"),
    ("pit_assist", "B"),
    ("pit_release_assist", "B"),
    ("ers_assist", "B"),
    ("drs_assist", "B"),
    ("dynamic_racing_line", "B"),
    ("dynamic_racing_line_type", "B"),
    ("game_mode", "B"),
    ("rule_set", "B"),
    ("time_of_day", "I"),
    ("session_length", "B"),
    ("speed_units_lead_player", "B"),
    ("temperature_units_lead_player", "B"),
    ("speed_units_secondary_player", "B"),
    ("temperature_units_secondary_player", "B"),
    ("num_safety_car_periods", "B"),
    ("num_virtual_safety_car_periods", "B"),
    ("num_red_flag_periods", "B"),
    ("equal_car_performance", "B"),
    ("recovery_mode", "B"),
    ("flashback_limit", "B"),
    ("surface_type", "B"),
    ("low_fuel_mode", "B"),
    ("race_starts", "B"),
    ("tyre_temperature", "B"),
    ("pit_lane_tyre_sim", "B"),
    ("car_damage", "B"),
    ("car_damage_rate", "B"),
    ("collisions", "B"),
    ("collisions_off_for_first_lap_only", "B"),
    ("mp_unsafe_pit_release", "B"),
    ("mp_off_for_griefing", "B"),
    ("corner_cutting_stringency", "B"),
    ("parc_ferme_rules", "B"),
    ("pit_stop_experience", "B"),
    ("safety_car", "B"),
    ("safety_car_experience", "B"),
    ("formation_lap", "B"),
    ("formation_lap_experience", "B"),
    ("red_flags", "B"),
    ("affects_licence_level_solo", "B"),
    ("affects_licence_level_mp", "B"),
    ("num_sessions_in_weekend", "B"),
    ("weekend_structure", "12s"),
    ("sector2_lap_distance_start", "f"),
    ("sector3_lap_distance_start", "f"),
]

# --------------------------------------------------------------------------
# id 2 -- LapData (57 B per car, identical in both formats)
# --------------------------------------------------------------------------

LAP_CAR_FIELDS: list[Field] = [
    ("last_lap_time_ms", "I"),
    ("current_lap_time_ms", "I"),
    ("sector1_time_ms_part", "H"),
    ("sector1_time_minutes_part", "B"),
    ("sector2_time_ms_part", "H"),
    ("sector2_time_minutes_part", "B"),
    ("delta_to_car_in_front_ms_part", "H"),
    ("delta_to_car_in_front_minutes_part", "B"),
    ("delta_to_race_leader_ms_part", "H"),
    ("delta_to_race_leader_minutes_part", "B"),
    ("lap_distance", "f"),
    ("total_distance", "f"),
    ("safety_car_delta", "f"),
    ("car_position", "B"),
    ("current_lap_num", "B"),
    ("pit_status", "B"),
    ("num_pit_stops", "B"),
    ("sector", "B"),
    ("current_lap_invalid", "B"),
    ("penalties", "B"),
    ("total_warnings", "B"),
    ("corner_cutting_warnings", "B"),
    ("num_unserved_drive_through_pens", "B"),
    ("num_unserved_stop_go_pens", "B"),
    ("grid_position", "B"),
    ("driver_status", "B"),
    ("result_status", "B"),
    ("pit_lane_timer_active", "B"),
    ("pit_lane_time_in_lane_ms", "H"),
    ("pit_stop_timer_ms", "H"),
    ("pit_stop_should_serve_pen", "B"),
    ("speed_trap_fastest_speed", "f"),
    ("speed_trap_fastest_lap", "B"),
]

#: ``CarLap`` needs the first 46 bytes only -- through ``result_status``. The
#: pit-lane timers and speed trap tail are not part of the timing tower view,
#: so the opponent sweep skips them.
LAP_CARLAP_FIELDS: list[Field] = LAP_CAR_FIELDS[:27]

LAP_TAIL_FIELDS: list[Field] = [
    ("time_trial_pb_car_idx", "B"),
    ("time_trial_rival_car_idx", "B"),
]

# --------------------------------------------------------------------------
# id 3 -- Event (4-char code + 12-byte union payload, 16 B body, both formats)
# --------------------------------------------------------------------------

EVENT_CODE_SIZE = 4
EVENT_PAYLOAD_SIZE = 12

# --------------------------------------------------------------------------
# id 4 -- Participants (57 B per car in 2025)
# --------------------------------------------------------------------------

PARTICIPANT_FIELDS: list[Field] = [
    ("ai_controlled", "B"),
    ("driver_id", "B"),
    ("network_id", "B"),
    ("team_id", "B"),
    ("my_team", "B"),
    ("race_number", "B"),
    ("nationality", "B"),
    ("name", "32s"),
    ("your_telemetry", "B"),
    ("show_online_names", "B"),
    ("tech_level", "H"),
    ("platform", "B"),
    ("num_colours", "B"),
    *[
        (f"livery_{i}_{channel}", "B")
        for i in range(MAX_LIVERY_COLOURS)
        for channel in ("r", "g", "b")
    ],
]

# --------------------------------------------------------------------------
# id 5 -- CarSetups (skipped; size only)
# --------------------------------------------------------------------------

CAR_SETUP_FIELDS: list[Field] = [
    ("front_wing", "B"),
    ("rear_wing", "B"),
    ("on_throttle", "B"),
    ("off_throttle", "B"),
    ("front_camber", "f"),
    ("rear_camber", "f"),
    ("front_toe", "f"),
    ("rear_toe", "f"),
    ("front_suspension", "B"),
    ("rear_suspension", "B"),
    ("front_anti_roll_bar", "B"),
    ("rear_anti_roll_bar", "B"),
    ("front_suspension_height", "B"),
    ("rear_suspension_height", "B"),
    ("brake_pressure", "B"),
    ("brake_bias", "B"),
    ("engine_braking", "B"),
    *_wheels("tyre_pressure", "f"),
    ("ballast", "B"),
    ("fuel_load", "f"),
]

# --------------------------------------------------------------------------
# id 6 -- CarTelemetry (60 B per car in 2025)
# --------------------------------------------------------------------------

TELEMETRY_CAR_FIELDS: list[Field] = [
    ("speed", "H"),
    ("throttle", "f"),
    ("steer", "f"),
    ("brake", "f"),
    ("clutch", "B"),
    ("gear", "b"),
    ("engine_rpm", "H"),
    ("drs", "B"),
    ("rev_lights_percent", "B"),
    ("rev_lights_bit_value", "H"),
    *_wheels("brakes_temperature", "H"),
    *_wheels("tyres_surface_temperature", "B"),
    *_wheels("tyres_inner_temperature", "B"),
    ("engine_temperature", "H"),
    *_wheels("tyres_pressure", "f"),
    *_wheels("surface_type", "B"),
]

TELEMETRY_TAIL_FIELDS: list[Field] = [
    ("mfd_panel_index", "B"),
    ("mfd_panel_index_secondary_player", "B"),
    ("suggested_gear", "b"),
]

# --------------------------------------------------------------------------
# id 7 -- CarStatus (55 B per car in 2025)
# --------------------------------------------------------------------------

STATUS_CAR_FIELDS: list[Field] = [
    ("traction_control", "B"),
    ("anti_lock_brakes", "B"),
    ("fuel_mix", "B"),
    ("front_brake_bias", "B"),
    ("pit_limiter_status", "B"),
    ("fuel_in_tank", "f"),
    ("fuel_capacity", "f"),
    ("fuel_remaining_laps", "f"),
    ("max_rpm", "H"),
    ("idle_rpm", "H"),
    ("max_gears", "B"),
    ("drs_allowed", "B"),
    ("drs_activation_distance", "H"),
    ("actual_tyre_compound", "B"),
    ("visual_tyre_compound", "B"),
    ("tyres_age_laps", "B"),
    ("vehicle_fia_flags", "b"),
    ("engine_power_ice", "f"),
    ("engine_power_mguk", "f"),
    ("ers_store_energy", "f"),
    ("ers_deploy_mode", "B"),
    ("ers_harvested_this_lap_mguk", "f"),
    ("ers_harvested_this_lap_mguh", "f"),
    ("ers_deployed_this_lap", "f"),
    ("network_paused", "B"),
]

# --------------------------------------------------------------------------
# id 8 -- FinalClassification (46 B per car, identical in both formats)
# --------------------------------------------------------------------------

CLASSIFICATION_ROW_FIELDS: list[Field] = [
    ("position", "B"),
    ("num_laps", "B"),
    ("grid_position", "B"),
    ("points", "B"),
    ("num_pit_stops", "B"),
    ("result_status", "B"),
    ("result_reason", "B"),
    ("best_lap_time_ms", "I"),
    ("total_race_time", "d"),
    ("penalties_time", "B"),
    ("num_penalties", "B"),
    ("num_tyre_stints", "B"),
    ("tyre_stints_actual", "8s"),
    ("tyre_stints_visual", "8s"),
    ("tyre_stints_end_laps", "8s"),
]

# --------------------------------------------------------------------------
# id 9 -- LobbyInfo (skipped; size only). 42 B per car in 2025.
# --------------------------------------------------------------------------

LOBBY_INFO_FIELDS: list[Field] = [
    ("ai_controlled", "B"),
    ("team_id", "B"),
    ("nationality", "B"),
    ("platform", "B"),
    ("name", "32s"),
    ("car_number", "B"),
    ("your_telemetry", "B"),
    ("show_online_names", "B"),
    ("tech_level", "H"),
    ("ready_status", "B"),
]

# --------------------------------------------------------------------------
# id 10 -- CarDamage (46 B per car, identical in both formats)
# --------------------------------------------------------------------------

DAMAGE_CAR_FIELDS: list[Field] = [
    *_wheels("tyres_wear", "f"),
    *_wheels("tyres_damage", "B"),
    *_wheels("brakes_damage", "B"),
    *_wheels("tyre_blisters", "B"),
    ("front_left_wing_damage", "B"),
    ("front_right_wing_damage", "B"),
    ("rear_wing_damage", "B"),
    ("floor_damage", "B"),
    ("diffuser_damage", "B"),
    ("sidepod_damage", "B"),
    ("drs_fault", "B"),
    ("ers_fault", "B"),
    ("gear_box_damage", "B"),
    ("engine_damage", "B"),
    ("engine_mguh_wear", "B"),
    ("engine_es_wear", "B"),
    ("engine_ce_wear", "B"),
    ("engine_ice_wear", "B"),
    ("engine_mguk_wear", "B"),
    ("engine_tc_wear", "B"),
    ("engine_blown", "B"),
    ("engine_seized", "B"),
]

# --------------------------------------------------------------------------
# id 11 -- SessionHistory (identical in both formats)
# --------------------------------------------------------------------------

HISTORY_HEAD_FIELDS: list[Field] = [
    ("car_idx", "B"),
    ("num_laps", "B"),
    ("num_tyre_stints", "B"),
    ("best_lap_time_lap_num", "B"),
    ("best_sector1_lap_num", "B"),
    ("best_sector2_lap_num", "B"),
    ("best_sector3_lap_num", "B"),
]

HISTORY_LAP_FIELDS: list[Field] = [
    ("lap_time_ms", "I"),
    ("sector1_time_ms", "H"),
    ("sector1_time_minutes", "B"),
    ("sector2_time_ms", "H"),
    ("sector2_time_minutes", "B"),
    ("sector3_time_ms", "H"),
    ("sector3_time_minutes", "B"),
    ("lap_valid_bit_flags", "B"),
]

TYRE_STINT_FIELDS: list[Field] = [
    ("end_lap", "B"),
    ("tyre_actual_compound", "B"),
    ("tyre_visual_compound", "B"),
]

# --------------------------------------------------------------------------
# id 12 -- TyreSets (skipped; size only). Identical in both formats.
# --------------------------------------------------------------------------

TYRE_SET_FIELDS: list[Field] = [
    ("actual_tyre_compound", "B"),
    ("visual_tyre_compound", "B"),
    ("wear", "B"),
    ("available", "B"),
    ("recommended_session", "B"),
    ("life_span", "B"),
    ("useable_life", "B"),
    ("lap_delta_time", "h"),
    ("fitted", "B"),
]

# --------------------------------------------------------------------------
# id 13 -- MotionEx (skipped; size only). 61 consecutive floats, both formats.
# --------------------------------------------------------------------------

MOTION_EX_FLOAT_COUNT = 61

# --------------------------------------------------------------------------
# id 14 -- TimeTrial (24 B per set in 2025)
# --------------------------------------------------------------------------

TIME_TRIAL_SET_FIELDS: list[Field] = [
    ("car_idx", "B"),
    ("team_id", "B"),
    ("lap_time_ms", "I"),
    ("sector1_time_ms", "I"),
    ("sector2_time_ms", "I"),
    ("sector3_time_ms", "I"),
    ("traction_control", "B"),
    ("gearbox_assist", "B"),
    ("anti_lock_brakes", "B"),
    ("equal_car_performance", "B"),
    ("custom_setup", "B"),
    ("valid", "B"),
]

# --------------------------------------------------------------------------
# id 15 -- LapPositions (skipped; size only)
# --------------------------------------------------------------------------

LAP_POSITIONS_HEAD_SIZE = 2  # num_laps + lap_start


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """Everything the decoders need to read one packet format.

    A single instance is built per format at import time; decoders take it as
    their first argument and are otherwise format-agnostic.
    """

    packet_format: int
    num_cars: int
    #: packet id -> exact wire size in bytes (header included).
    sizes: Mapping[int, int]
    #: Divisor applied to the packed int16 g-force fields. 1.0 where the wire
    #: already carries float32 (format 2025).
    g_force_divisor: float

    motion_stride: int
    motion_player: Table

    lap_stride: int
    lap_carlap: Table
    lap_all: struct.Struct

    telemetry_stride: int
    telemetry_car: Table
    telemetry_speeds: struct.Struct

    status_stride: int
    status_car: Table

    damage_stride: int
    damage_car: Table

    session_head: Table
    session_mid: Table
    session_mid_offset: int
    forecast_sample: Table
    forecast_offset: int

    participant_stride: int
    participant: Table

    history_head: Table
    history_lap: Table
    history_laps_offset: int

    classification_stride: int
    classification_row: Table

    time_trial_stride: int
    time_trial_set: Table

    #: Whether the COLL event payload carries the 2026 severity byte.
    collision_has_severity: bool

    def car_offset(self, base: int, stride: int, car_index: int) -> int:
        """Byte offset of ``car_index``'s record in an array starting at ``base``."""
        return base + stride * car_index


def build_spec(
    *,
    packet_format: int,
    num_cars: int,
    g_force_divisor: float,
    motion_car_fields: Sequence[Field],
    motion_player_fields: Sequence[Field],
    session_head_fields: Sequence[Field],
    session_tail_fields: Sequence[Field],
    participant_fields: Sequence[Field],
    car_setup_fields: Sequence[Field],
    telemetry_car_fields: Sequence[Field],
    status_car_fields: Sequence[Field],
    lobby_info_fields: Sequence[Field],
    time_trial_set_fields: Sequence[Field],
    collision_has_severity: bool,
    has_car_telemetry_2: bool,
    car_telemetry_2_stride: int = 0,
) -> FormatSpec:
    """Derive a :class:`FormatSpec` (offsets, strides, wire sizes) from tables.

    The 2026 spec calls this with the same arguments as 2025 apart from the
    handful of tables the Season Pack actually changed.
    """
    motion_stride = _size(motion_car_fields)
    lap_stride = _size(LAP_CAR_FIELDS)
    telemetry_stride = _size(telemetry_car_fields)
    status_stride = _size(status_car_fields)
    damage_stride = _size(DAMAGE_CAR_FIELDS)
    participant_stride = _size(participant_fields)
    classification_stride = _size(CLASSIFICATION_ROW_FIELDS)
    time_trial_stride = _size(time_trial_set_fields)
    setup_stride = _size(car_setup_fields)
    lobby_stride = _size(lobby_info_fields)

    session_head = table(session_head_fields)
    marshal_block = _size(MARSHAL_ZONE_FIELDS) * MAX_MARSHAL_ZONES
    session_mid_offset = HEADER_SIZE + session_head.size + marshal_block
    session_mid = table(SESSION_MID_FIELDS)
    forecast_sample = table(FORECAST_SAMPLE_FIELDS)
    forecast_offset = session_mid_offset + session_mid.size
    forecast_block = forecast_sample.size * MAX_FORECAST_SAMPLES
    session_body = (
        session_head.size
        + marshal_block
        + session_mid.size
        + forecast_block
        + _size(session_tail_fields)
    )

    history_head = table(HISTORY_HEAD_FIELDS)
    history_lap = table(HISTORY_LAP_FIELDS)
    history_body = (
        history_head.size
        + history_lap.size * MAX_HISTORY_LAPS
        + _size(TYRE_STINT_FIELDS) * MAX_TYRE_STINTS
    )

    sizes: dict[int, int] = {
        0: HEADER_SIZE + motion_stride * num_cars,
        1: HEADER_SIZE + session_body,
        2: HEADER_SIZE + lap_stride * num_cars + _size(LAP_TAIL_FIELDS),
        3: HEADER_SIZE + EVENT_CODE_SIZE + EVENT_PAYLOAD_SIZE,
        4: HEADER_SIZE + 1 + participant_stride * num_cars,
        5: HEADER_SIZE + setup_stride * num_cars + 4,  # + next_front_wing_value
        6: HEADER_SIZE + telemetry_stride * num_cars + _size(TELEMETRY_TAIL_FIELDS),
        7: HEADER_SIZE + status_stride * num_cars,
        8: HEADER_SIZE + 1 + classification_stride * num_cars,
        9: HEADER_SIZE + 1 + lobby_stride * num_cars,
        10: HEADER_SIZE + damage_stride * num_cars,
        11: HEADER_SIZE + history_body,
        12: HEADER_SIZE + 1 + _size(TYRE_SET_FIELDS) * MAX_TYRE_SETS + 1,
        13: HEADER_SIZE + 4 * MOTION_EX_FLOAT_COUNT,
        14: HEADER_SIZE + time_trial_stride * 3,
        15: HEADER_SIZE + LAP_POSITIONS_HEAD_SIZE + MAX_LAP_POSITION_LAPS * num_cars,
    }
    if has_car_telemetry_2:
        sizes[16] = HEADER_SIZE + car_telemetry_2_stride * num_cars

    lap_carlap = table(LAP_CARLAP_FIELDS)
    telemetry_car = table(telemetry_car_fields)

    return FormatSpec(
        packet_format=packet_format,
        num_cars=num_cars,
        sizes=MappingProxyType(sizes),
        g_force_divisor=g_force_divisor,
        motion_stride=motion_stride,
        motion_player=table(motion_player_fields),
        lap_stride=lap_stride,
        lap_carlap=lap_carlap,
        lap_all=repeated(lap_carlap, num_cars, lap_stride),
        telemetry_stride=telemetry_stride,
        telemetry_car=telemetry_car,
        telemetry_speeds=repeated(
            table([("speed", "H")]), num_cars, telemetry_stride
        ),
        status_stride=status_stride,
        status_car=table(status_car_fields),
        damage_stride=damage_stride,
        damage_car=table(DAMAGE_CAR_FIELDS),
        session_head=session_head,
        session_mid=session_mid,
        session_mid_offset=session_mid_offset,
        forecast_sample=forecast_sample,
        forecast_offset=forecast_offset,
        participant_stride=participant_stride,
        participant=table(participant_fields),
        history_head=history_head,
        history_lap=history_lap,
        history_laps_offset=HEADER_SIZE + history_head.size,
        classification_stride=classification_stride,
        classification_row=table(CLASSIFICATION_ROW_FIELDS),
        time_trial_stride=time_trial_stride,
        time_trial_set=table(time_trial_set_fields),
        collision_has_severity=collision_has_severity,
    )


SPEC_2025 = build_spec(
    packet_format=2025,
    num_cars=22,
    g_force_divisor=1.0,  # 2025 carries g-forces as float32 already
    motion_car_fields=MOTION_CAR_FIELDS,
    motion_player_fields=MOTION_PLAYER_FIELDS,
    session_head_fields=SESSION_HEAD_FIELDS,
    session_tail_fields=SESSION_TAIL_FIELDS,
    participant_fields=PARTICIPANT_FIELDS,
    car_setup_fields=CAR_SETUP_FIELDS,
    telemetry_car_fields=TELEMETRY_CAR_FIELDS,
    status_car_fields=STATUS_CAR_FIELDS,
    lobby_info_fields=LOBBY_INFO_FIELDS,
    time_trial_set_fields=TIME_TRIAL_SET_FIELDS,
    collision_has_severity=False,
    has_car_telemetry_2=False,
)

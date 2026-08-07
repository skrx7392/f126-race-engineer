"""Shared types: the seam between the packet parser and everything downstream.

The parser (parser/) decodes raw UDP datagrams into these normalized views.
The state machine (state/), DB writer (store/), and WS broadcaster (web/)
consume ONLY these types — never raw structs. Field names here are a frozen
contract; extending with new fields is fine, renaming/removing is not.

Units: speeds km/h, temperatures °C, pressures PSI, times milliseconds unless
suffixed otherwise, distances metres, fuel kg, energy Joules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class PacketId(IntEnum):
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14
    LAP_POSITIONS = 15
    CAR_TELEMETRY_2 = 16  # 2026 Season Pack only


@dataclass(slots=True)
class PacketHeader:
    packet_format: int  # 2025 | 2026
    game_year: int
    packet_version: int
    packet_id: int
    session_uid: int  # u64
    session_time: float  # seconds
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int
    secondary_player_car_index: int


# ---- Per-packet normalized views (player-centric; opponents minimal) ----


@dataclass(slots=True)
class WheelSet:
    """Order: RL, RR, FL, FR (matches the spec's wheel array order)."""

    rl: float
    rr: float
    fl: float
    fr: float


@dataclass(slots=True)
class TelemetryView:
    """From CarTelemetry (+ CarTelemetry2 fields when format 2026)."""

    speed_kmh: float
    throttle: float  # 0..1
    brake: float  # 0..1
    steer: float  # -1..1
    gear: int  # -1 reverse, 0 neutral
    rpm: int
    drs_open: bool | None  # 2025; None in 2026 (aero_mode instead)
    aero_mode: int | None  # 2026 active-aero state; None in 2025
    tyre_surface_temp: WheelSet
    tyre_inner_temp: WheelSet
    tyre_pressure: WheelSet
    brake_temp: WheelSet
    engine_temp: float
    rev_lights_percent: int
    opponent_speeds_kmh: list[float] = field(default_factory=list)  # index = car_index
    # 2026 energy-system extras (None on 2025 format)
    energy_store_j: float | None = None
    energy_deploy_mode: int | None = None
    energy_harvested_lap_j: float | None = None
    energy_deployed_lap_j: float | None = None


@dataclass(slots=True)
class CarLap:
    """Minimal per-car lap info for the timing tower (all 22 cars)."""

    car_index: int
    position: int
    lap_number: int
    lap_distance_m: float
    total_distance_m: float
    last_lap_ms: int
    current_lap_ms: int
    sector: int  # 0..2
    sector1_ms: int
    sector2_ms: int
    lap_invalid: bool
    penalties_s: int
    pit_status: int  # 0 none, 1 pitting, 2 in pit area
    result_status: int
    delta_to_car_ahead_ms: int
    delta_to_leader_ms: int


@dataclass(slots=True)
class LapView:
    player: CarLap
    cars: list[CarLap]  # all cars incl. player, index = car_index


@dataclass(slots=True)
class StatusView:
    """From CarStatus (player car)."""

    fuel_in_tank_kg: float
    fuel_capacity_kg: float
    fuel_remaining_laps: float
    tyre_compound_actual: int
    tyre_compound_visual: int
    tyre_age_laps: int
    fia_flags: int  # -1 invalid, 0 none, 1 green, 2 blue, 3 yellow, 4 red
    ers_store_j: float | None  # None on 2026 (energy system lives in TelemetryView)
    ers_deploy_mode: int | None
    ers_harvested_lap_j: float | None
    ers_deployed_lap_j: float | None
    drs_allowed: bool | None
    vehicle_fia_flags: int = 0


@dataclass(slots=True)
class DamageView:
    """From CarDamage (player car)."""

    tyre_wear_pct: WheelSet
    tyre_damage_pct: WheelSet
    brake_damage_pct: WheelSet
    front_left_wing_pct: int
    front_right_wing_pct: int
    rear_wing_pct: int
    floor_pct: int
    diffuser_pct: int
    sidepod_pct: int
    gearbox_pct: int
    engine_pct: int


@dataclass(slots=True)
class MotionView:
    """From Motion (player car)."""

    world_x: float
    world_z: float
    world_y: float
    g_lat: float
    g_lon: float
    yaw: float


@dataclass(slots=True)
class WeatherSample:
    offset_min: int
    weather: int
    track_temp_c: int
    air_temp_c: int
    rain_percentage: int


@dataclass(slots=True)
class SessionView:
    """From Session packet (2 Hz)."""

    session_type: int
    track_id: int
    track_length_m: int
    session_time_left_s: int
    session_duration_s: int
    total_laps: int
    weather: int
    track_temp_c: int
    air_temp_c: int
    safety_car_status: int
    pit_speed_limit_kmh: int
    forecast: list[WeatherSample] = field(default_factory=list)


@dataclass(slots=True)
class ParticipantView:
    car_index: int
    name: str
    team_id: int
    race_number: int
    is_ai: bool


@dataclass(slots=True)
class ParticipantsView:
    num_active: int
    cars: list[ParticipantView]


@dataclass(slots=True)
class EventView:
    """From Event packet. code is the 4-char spec code, e.g. SSTA, SEND,
    FTLP, RTMT, DRSE, DRSD, CHQF, RCWN, PENA, SPTP, STLG, LGOT, DTSV,
    SGSV, FLBK, BUTN, RDFL, OVTK, SCAR, COLL."""

    code: str
    details: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(slots=True)
class HistoryLap:
    lap_number: int
    lap_time_ms: int
    sector1_ms: int
    sector2_ms: int
    sector3_ms: int
    valid: bool


@dataclass(slots=True)
class HistoryView:
    """From SessionHistory (one car per packet, cycles through cars)."""

    car_index: int
    num_laps: int
    best_lap_number: int
    laps: list[HistoryLap]


@dataclass(slots=True)
class ClassificationRow:
    car_index: int
    position: int
    num_laps: int
    grid_position: int
    points: int
    num_pit_stops: int
    result_status: int
    best_lap_ms: int
    total_race_time_s: float
    penalties_s: int


@dataclass(slots=True)
class ClassificationView:
    rows: list[ClassificationRow]


@dataclass(slots=True)
class TimeTrialSet:
    car_index: int
    team_id: int
    lap_time_ms: int
    sector1_ms: int
    sector2_ms: int
    sector3_ms: int
    valid: bool


@dataclass(slots=True)
class TimeTrialView:
    player_session_best: TimeTrialSet | None
    personal_best: TimeTrialSet | None
    rival: TimeTrialSet | None


View = (
    TelemetryView
    | LapView
    | StatusView
    | DamageView
    | MotionView
    | SessionView
    | ParticipantsView
    | EventView
    | HistoryView
    | ClassificationView
    | TimeTrialView
)


@dataclass(slots=True)
class ParsedPacket:
    header: PacketHeader
    view: View | None  # None for packet types we deliberately skip (setups, lobby, ...)
    recv_monotonic_ns: int
    recv_wall_ns: int


@dataclass(slots=True)
class ParseError(Exception):
    """Raised internally by per-packet decoders; dispatch catches and counts."""

    packet_id: int | None
    reason: str

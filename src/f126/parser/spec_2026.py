"""Wire layout for packet format 2026 (F1 25 "2026 Season Pack" DLC).

Expressed as a diff against :mod:`spec_2025`: every table the Season Pack left
alone is imported and reused, so the two formats cannot drift apart by
accident. Source of truth is volodymyr-fed/F1Game.UDP v26
(``F1Game.UDP/Packets/*.cs``, ``F1Game.UDP/Data/*.cs``), cross-checked against a
real PS5 capture (see ``docs/spec-2026.md``).

The nine changes, all verified against live capture sizes:

1. 24 car slots instead of 22 (``ProtocolLimits.MaxCars``). This alone moves
   every array-shaped packet.
2. ``CarMotionData``: the three g-force components become packed int16
   (60 -> 54 B per car).
3. ``CarTelemetryData``: ``engineTemperature`` narrows uint16 -> uint8
   (60 -> 59 B per car).
4. ``CarStatusData``: gains ``ersHarvestLimitPerLap`` (float)
   (55 -> 59 B per car).
5. ``ParticipantData``: ``driverId``, ``networkId`` and ``teamId`` all widen
   uint8 -> uint16 (57 -> 60 B per car).
6. ``LobbyInfoData``: ``teamId`` widens uint8 -> uint16 (42 -> 43 B per car).
7. ``TimeTrialDataSet``: ``teamId`` widens uint8 -> uint16 (24 -> 25 B).
8. ``PacketSessionData``: 173 B of new tail (active-aero zones, explicit DRS
   zones, start reaction time, extra assist settings).
9. New packet id 16, ``CarTelemetry2``, 10 B per car.

Plus one payload change: the ``COLL`` event gains a third ``severity`` byte.
"""

from __future__ import annotations

from .spec_2025 import (
    CAR_SETUP_FIELDS,
    LOBBY_INFO_FIELDS,
    MOTION_CAR_FIELDS,
    PARTICIPANT_FIELDS,
    SESSION_HEAD_FIELDS,
    SESSION_TAIL_FIELDS,
    STATUS_CAR_FIELDS,
    TELEMETRY_CAR_FIELDS,
    TIME_TRIAL_SET_FIELDS,
    Field,
    build_spec,
    table,
)

__all__ = [
    "CAR_TELEMETRY_2_FIELDS",
    "G_FORCE_DIVISOR_2026",
    "NUM_CARS_2026",
    "SPEC_2026",
    "car_telemetry_2_table",
]

NUM_CARS_2026 = 24

#: Counts per g in the 2026 packed int16 g-force fields, i.e. the wire value is
#: fixed-point with a 1/1024 g quantum (representable range +-32 g).
#:
#: The EA spec page for the 2026 Season Pack is not reachable from this
#: environment (HTTP 403) and F1Game.UDP exposes these fields as raw ``short``
#: without documenting a conversion, so this constant was determined
#: empirically from the Bahrain P1 capture: longitudinal acceleration was
#: derived from the packet's own float32 world-velocity vector projected onto
#: the car's forward axis, and compared against the packed value. At 1024
#: counts/g the median residual is 0.022 m/s^2 across 835 samples; the next
#: best candidates (512 and 2048) are off by 1.5 and 0.8 m/s^2 respectively.
#: The resulting peaks -- 4.5 g braking, 4.0 g lateral -- are physically right
#: for an F1 car. See ``tests/test_parser_2026.py::test_g_force_scale_*``.
G_FORCE_DIVISOR_2026 = 1024.0


def _replace(fields: list[Field], name: str, code: str) -> list[Field]:
    """Copy ``fields`` with the wire type of ``name`` changed to ``code``."""
    out = [(n, code if n == name else c) for n, c in fields]
    if out == fields:
        raise ValueError(f"field {name!r} not present or already {code!r}")
    return out


def _insert_after(fields: list[Field], after: str, new: Field) -> list[Field]:
    """Copy ``fields`` with ``new`` spliced in directly after ``after``."""
    names = [n for n, _ in fields]
    if after not in names:
        raise ValueError(f"field {after!r} not present")
    at = names.index(after) + 1
    return [*fields[:at], new, *fields[at:]]


def _zones(prefix: str, count: int) -> list[Field]:
    """``count`` track zones, each a ``(start, end)`` lap-fraction float pair."""
    return [
        (f"{prefix}_{i}_{edge}", "f") for i in range(count) for edge in ("start", "end")
    ]


# -- 2. Motion: g-forces packed to int16 -----------------------------------

MOTION_CAR_FIELDS_2026: list[Field] = _replace(
    _replace(
        _replace(MOTION_CAR_FIELDS, "g_force_lateral", "h"),
        "g_force_longitudinal",
        "h",
    ),
    "g_force_vertical",
    "h",
)
MOTION_PLAYER_FIELDS_2026: list[Field] = MOTION_CAR_FIELDS_2026[:16]

# -- 3. CarTelemetry: engine temperature narrows to uint8 -------------------

TELEMETRY_CAR_FIELDS_2026: list[Field] = _replace(
    TELEMETRY_CAR_FIELDS, "engine_temperature", "B"
)

# -- 4. CarStatus: new per-lap ERS harvest limit ----------------------------

STATUS_CAR_FIELDS_2026: list[Field] = _insert_after(
    STATUS_CAR_FIELDS, "ers_harvested_this_lap_mguh", ("ers_harvest_limit_per_lap", "f")
)

# -- 5/6/7. Widened id fields ----------------------------------------------

PARTICIPANT_FIELDS_2026: list[Field] = _replace(
    _replace(_replace(PARTICIPANT_FIELDS, "driver_id", "H"), "network_id", "H"),
    "team_id",
    "H",
)
LOBBY_INFO_FIELDS_2026: list[Field] = _replace(LOBBY_INFO_FIELDS, "team_id", "H")
TIME_TRIAL_SET_FIELDS_2026: list[Field] = _replace(TIME_TRIAL_SET_FIELDS, "team_id", "H")

# -- 8. Session gains a 173-byte tail --------------------------------------

MAX_ACTIVE_AERO_ZONES = 8
MAX_DRS_ZONES = 4

SESSION_TAIL_FIELDS_2026: list[Field] = [
    *SESSION_TAIL_FIELDS,
    ("active_aero_track_status", "B"),
    ("num_active_aero_zones_full", "B"),
    *_zones("active_aero_zone_full", MAX_ACTIVE_AERO_ZONES),
    ("num_active_aero_zones_partial", "B"),
    *_zones("active_aero_zone_partial", MAX_ACTIVE_AERO_ZONES),
    ("num_drs_zones", "B"),
    *_zones("drs_zone", MAX_DRS_ZONES),
    ("start_reaction_time", "f"),
    ("anti_lock_brakes_assist", "B"),
    ("traction_control_assist", "B"),
    ("dynamic_racing_line_hi_vis", "B"),
    ("dynamic_racing_line_colour_blind", "B"),
    ("recurring_rewind_prompt", "B"),
]

# -- 9. New packet id 16, CarTelemetry2 (10 B per car) ---------------------

CAR_TELEMETRY_2_FIELDS: list[Field] = [
    ("active_aero_mode", "B"),  # 0 = corner mode, 1 = straight mode
    ("active_aero_available", "B"),
    ("active_aero_activation_distance", "H"),
    ("overtake_available", "B"),
    ("overtake_active", "B"),
    ("overtake_activation_distance", "H"),
    ("regulations_2026_applicable", "B"),
    ("is_driving_wrong_way", "B"),
]

car_telemetry_2_table = table(CAR_TELEMETRY_2_FIELDS)
CAR_TELEMETRY_2_STRIDE = car_telemetry_2_table.size


SPEC_2026 = build_spec(
    packet_format=2026,
    num_cars=NUM_CARS_2026,
    g_force_divisor=G_FORCE_DIVISOR_2026,
    motion_car_fields=MOTION_CAR_FIELDS_2026,
    motion_player_fields=MOTION_PLAYER_FIELDS_2026,
    session_head_fields=SESSION_HEAD_FIELDS,  # unchanged
    session_tail_fields=SESSION_TAIL_FIELDS_2026,
    participant_fields=PARTICIPANT_FIELDS_2026,
    car_setup_fields=CAR_SETUP_FIELDS,  # unchanged (24 cars is the only delta)
    telemetry_car_fields=TELEMETRY_CAR_FIELDS_2026,
    status_car_fields=STATUS_CAR_FIELDS_2026,
    lobby_info_fields=LOBBY_INFO_FIELDS_2026,
    time_trial_set_fields=TIME_TRIAL_SET_FIELDS_2026,
    collision_has_severity=True,
    has_car_telemetry_2=True,
    car_telemetry_2_stride=CAR_TELEMETRY_2_STRIDE,
)

"""Synthetic packet builders for both wire formats.

Every packet is assembled by ``struct.pack``-ing one field at a time, in
declared wire order, and concatenating. The builders deliberately do *not*
reuse the parser's compiled ``struct.Struct`` objects, its derived offsets, or
its padded per-car sweep formats -- those are exactly the things a round-trip
test needs to prove. What they do share with the parser is the declarative
``(name, format_code)`` field tables, which are independently pinned against
the reference documentation by the size tests.

Usage::

    data = build_car_telemetry(2026, player_index=3, cars={3: {"speed": 250}})

Unset fields default to zero (or NULs for fixed-width strings), so a test only
names the fields it actually asserts on.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from typing import Any

from f126.parser.spec_2025 import (
    CAR_SETUP_FIELDS,
    CLASSIFICATION_ROW_FIELDS,
    DAMAGE_CAR_FIELDS,
    FORECAST_SAMPLE_FIELDS,
    HEADER_FIELDS,
    HISTORY_HEAD_FIELDS,
    HISTORY_LAP_FIELDS,
    LAP_CAR_FIELDS,
    LAP_TAIL_FIELDS,
    LOBBY_INFO_FIELDS,
    MARSHAL_ZONE_FIELDS,
    MAX_FORECAST_SAMPLES,
    MAX_HISTORY_LAPS,
    MAX_LAP_POSITION_LAPS,
    MAX_MARSHAL_ZONES,
    MAX_TYRE_SETS,
    MAX_TYRE_STINTS,
    MOTION_CAR_FIELDS,
    MOTION_EX_FLOAT_COUNT,
    PARTICIPANT_FIELDS,
    SESSION_HEAD_FIELDS,
    SESSION_MID_FIELDS,
    SESSION_TAIL_FIELDS,
    STATUS_CAR_FIELDS,
    TELEMETRY_CAR_FIELDS,
    TELEMETRY_TAIL_FIELDS,
    TIME_TRIAL_SET_FIELDS,
    TYRE_SET_FIELDS,
    TYRE_STINT_FIELDS,
    Field,
)
from f126.parser.spec_2026 import (
    CAR_TELEMETRY_2_FIELDS,
    LOBBY_INFO_FIELDS_2026,
    MOTION_CAR_FIELDS_2026,
    NUM_CARS_2026,
    PARTICIPANT_FIELDS_2026,
    SESSION_TAIL_FIELDS_2026,
    STATUS_CAR_FIELDS_2026,
    TELEMETRY_CAR_FIELDS_2026,
    TIME_TRIAL_SET_FIELDS_2026,
)

Values = Mapping[str, Any]

NUM_CARS = {2025: 22, 2026: NUM_CARS_2026}

#: Per-format field tables. Entries absent from the 2026 dict are identical to
#: 2025 and are resolved by :func:`_fields`.
_TABLES_2026: dict[str, Sequence[Field]] = {
    "motion_car": MOTION_CAR_FIELDS_2026,
    "telemetry_car": TELEMETRY_CAR_FIELDS_2026,
    "status_car": STATUS_CAR_FIELDS_2026,
    "participant": PARTICIPANT_FIELDS_2026,
    "lobby_info": LOBBY_INFO_FIELDS_2026,
    "time_trial_set": TIME_TRIAL_SET_FIELDS_2026,
    "session_tail": SESSION_TAIL_FIELDS_2026,
}

_TABLES_COMMON: dict[str, Sequence[Field]] = {
    "header": HEADER_FIELDS,
    "motion_car": MOTION_CAR_FIELDS,
    "session_head": SESSION_HEAD_FIELDS,
    "session_mid": SESSION_MID_FIELDS,
    "session_tail": SESSION_TAIL_FIELDS,
    "marshal_zone": MARSHAL_ZONE_FIELDS,
    "forecast_sample": FORECAST_SAMPLE_FIELDS,
    "lap_car": LAP_CAR_FIELDS,
    "lap_tail": LAP_TAIL_FIELDS,
    "participant": PARTICIPANT_FIELDS,
    "car_setup": CAR_SETUP_FIELDS,
    "telemetry_car": TELEMETRY_CAR_FIELDS,
    "telemetry_tail": TELEMETRY_TAIL_FIELDS,
    "status_car": STATUS_CAR_FIELDS,
    "classification_row": CLASSIFICATION_ROW_FIELDS,
    "lobby_info": LOBBY_INFO_FIELDS,
    "damage_car": DAMAGE_CAR_FIELDS,
    "history_head": HISTORY_HEAD_FIELDS,
    "history_lap": HISTORY_LAP_FIELDS,
    "tyre_stint": TYRE_STINT_FIELDS,
    "tyre_set": TYRE_SET_FIELDS,
    "time_trial_set": TIME_TRIAL_SET_FIELDS,
    "car_telemetry_2": CAR_TELEMETRY_2_FIELDS,
}


def _fields(packet_format: int, name: str) -> Sequence[Field]:
    if packet_format == 2026 and name in _TABLES_2026:
        return _TABLES_2026[name]
    return _TABLES_COMMON[name]


def _default(code: str) -> Any:
    if code.endswith("s"):
        return b""
    if code in ("f", "d"):
        return 0.0
    return 0


def pack_fields(fields: Sequence[Field], values: Values | None = None) -> bytes:
    """Pack ``fields`` one at a time, in order. Unset fields default to zero."""
    values = values or {}
    unknown = set(values) - {name for name, _ in fields}
    if unknown:
        raise KeyError(f"no such field(s) in this table: {sorted(unknown)}")
    out = bytearray()
    for name, code in fields:
        out += struct.pack("<" + code, values.get(name, _default(code)))
    return bytes(out)


def _array(
    fields: Sequence[Field], count: int, per_index: Mapping[int, Values] | None
) -> bytes:
    per_index = per_index or {}
    return b"".join(pack_fields(fields, per_index.get(i)) for i in range(count))


def build_header(
    packet_format: int,
    packet_id: int,
    *,
    game_year: int = 25,
    game_major_version: int = 1,
    game_minor_version: int = 24,
    packet_version: int = 1,
    session_uid: int = 0xF126_0BADC0DE,
    session_time: float = 12.5,
    frame_identifier: int = 4321,
    overall_frame_identifier: int = 8642,
    player_car_index: int = 0,
    secondary_player_car_index: int = 255,
) -> bytes:
    """The 29-byte header, identical in layout for both formats."""
    return pack_fields(
        _TABLES_COMMON["header"],
        {
            "packet_format": packet_format,
            "game_year": game_year,
            "game_major_version": game_major_version,
            "game_minor_version": game_minor_version,
            "packet_version": packet_version,
            "packet_id": packet_id,
            "session_uid": session_uid,
            "session_time": session_time,
            "frame_identifier": frame_identifier,
            "overall_frame_identifier": overall_frame_identifier,
            "player_car_index": player_car_index,
            "secondary_player_car_index": secondary_player_car_index,
        },
    )


def wheels(prefix: str, rl: Any, rr: Any, fl: Any, fr: Any) -> dict[str, Any]:
    """Expand a wheel-ordered quad into the four ``<prefix>_<corner>`` fields."""
    return {f"{prefix}_rl": rl, f"{prefix}_rr": rr, f"{prefix}_fl": fl, f"{prefix}_fr": fr}


# --------------------------------------------------------------------------
# Decoded packet types
# --------------------------------------------------------------------------


def build_motion(
    packet_format: int, *, cars: Mapping[int, Values] | None = None, **header: Any
) -> bytes:
    """id 0. ``cars`` maps car index -> ``MOTION_CAR_FIELDS`` values."""
    return build_header(packet_format, 0, **header) + _array(
        _fields(packet_format, "motion_car"), NUM_CARS[packet_format], cars
    )


def build_session(
    packet_format: int,
    *,
    head: Values | None = None,
    mid: Values | None = None,
    forecast: Sequence[Values] = (),
    tail: Values | None = None,
    marshal_zones: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 1. ``forecast`` is a sequence of weather samples; the packet always
    carries all 64 slots and ``mid["num_weather_forecast_samples"]`` says how
    many are meaningful (set automatically unless overridden)."""
    mid = dict(mid or {})
    mid.setdefault("num_weather_forecast_samples", len(forecast))
    samples = {i: s for i, s in enumerate(forecast)}
    return (
        build_header(packet_format, 1, **header)
        + pack_fields(_fields(packet_format, "session_head"), head)
        + _array(_fields(packet_format, "marshal_zone"), MAX_MARSHAL_ZONES, marshal_zones)
        + pack_fields(_fields(packet_format, "session_mid"), mid)
        + _array(
            _fields(packet_format, "forecast_sample"), MAX_FORECAST_SAMPLES, samples
        )
        + pack_fields(_fields(packet_format, "session_tail"), tail)
    )


def build_lap_data(
    packet_format: int,
    *,
    cars: Mapping[int, Values] | None = None,
    tail: Values | None = None,
    **header: Any,
) -> bytes:
    """id 2. ``cars`` maps car index -> ``LAP_CAR_FIELDS`` values."""
    return (
        build_header(packet_format, 2, **header)
        + _array(_fields(packet_format, "lap_car"), NUM_CARS[packet_format], cars)
        + pack_fields(_fields(packet_format, "lap_tail"), tail)
    )


def build_event(
    packet_format: int, code: str, payload: bytes = b"", **header: Any
) -> bytes:
    """id 3. ``payload`` is zero-padded to the 12-byte union width."""
    if len(code) != 4:
        raise ValueError("event code must be 4 characters")
    if len(payload) > 12:
        raise ValueError("event payload exceeds the 12-byte union")
    return (
        build_header(packet_format, 3, **header)
        + code.encode("ascii")
        + payload.ljust(12, b"\x00")
    )


def build_participants(
    packet_format: int,
    *,
    num_active: int = 0,
    cars: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 4."""
    return (
        build_header(packet_format, 4, **header)
        + struct.pack("<B", num_active)
        + _array(_fields(packet_format, "participant"), NUM_CARS[packet_format], cars)
    )


def build_car_telemetry(
    packet_format: int,
    *,
    cars: Mapping[int, Values] | None = None,
    tail: Values | None = None,
    **header: Any,
) -> bytes:
    """id 6."""
    return (
        build_header(packet_format, 6, **header)
        + _array(_fields(packet_format, "telemetry_car"), NUM_CARS[packet_format], cars)
        + pack_fields(_fields(packet_format, "telemetry_tail"), tail)
    )


def build_car_status(
    packet_format: int, *, cars: Mapping[int, Values] | None = None, **header: Any
) -> bytes:
    """id 7."""
    return build_header(packet_format, 7, **header) + _array(
        _fields(packet_format, "status_car"), NUM_CARS[packet_format], cars
    )


def build_final_classification(
    packet_format: int,
    *,
    num_cars: int = 0,
    rows: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 8."""
    return (
        build_header(packet_format, 8, **header)
        + struct.pack("<B", num_cars)
        + _array(
            _fields(packet_format, "classification_row"), NUM_CARS[packet_format], rows
        )
    )


def build_car_damage(
    packet_format: int, *, cars: Mapping[int, Values] | None = None, **header: Any
) -> bytes:
    """id 10."""
    return build_header(packet_format, 10, **header) + _array(
        _fields(packet_format, "damage_car"), NUM_CARS[packet_format], cars
    )


def build_session_history(
    packet_format: int,
    *,
    head: Values | None = None,
    laps: Sequence[Values] = (),
    stints: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 11. ``head["num_laps"]`` is set from ``laps`` unless overridden."""
    head = dict(head or {})
    head.setdefault("num_laps", len(laps))
    return (
        build_header(packet_format, 11, **header)
        + pack_fields(_fields(packet_format, "history_head"), head)
        + _array(
            _fields(packet_format, "history_lap"),
            MAX_HISTORY_LAPS,
            dict(enumerate(laps)),
        )
        + _array(_fields(packet_format, "tyre_stint"), MAX_TYRE_STINTS, stints)
    )


def build_time_trial(
    packet_format: int,
    *,
    player_session_best: Values | None = None,
    personal_best: Values | None = None,
    rival: Values | None = None,
    **header: Any,
) -> bytes:
    """id 14. A set with ``car_idx == 255`` decodes to ``None``."""
    fields = _fields(packet_format, "time_trial_set")
    return (
        build_header(packet_format, 14, **header)
        + pack_fields(fields, player_session_best)
        + pack_fields(fields, personal_best)
        + pack_fields(fields, rival)
    )


def build_car_telemetry_2(
    packet_format: int, *, cars: Mapping[int, Values] | None = None, **header: Any
) -> bytes:
    """id 16, format 2026 only (build it on 2025 to exercise the error path)."""
    return build_header(packet_format, 16, **header) + _array(
        _TABLES_COMMON["car_telemetry_2"], NUM_CARS[packet_format], cars
    )


# --------------------------------------------------------------------------
# Deliberately skipped packet types (built so their sizes are still pinned)
# --------------------------------------------------------------------------


def build_car_setups(
    packet_format: int, *, cars: Mapping[int, Values] | None = None, **header: Any
) -> bytes:
    """id 5."""
    return (
        build_header(packet_format, 5, **header)
        + _array(_fields(packet_format, "car_setup"), NUM_CARS[packet_format], cars)
        + struct.pack("<f", 0.0)  # next_front_wing_value
    )


def build_lobby_info(
    packet_format: int,
    *,
    num_cars: int = 0,
    cars: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 9."""
    return (
        build_header(packet_format, 9, **header)
        + struct.pack("<B", num_cars)
        + _array(_fields(packet_format, "lobby_info"), NUM_CARS[packet_format], cars)
    )


def build_tyre_sets(
    packet_format: int,
    *,
    car_idx: int = 0,
    fitted_idx: int = 0,
    sets: Mapping[int, Values] | None = None,
    **header: Any,
) -> bytes:
    """id 12."""
    return (
        build_header(packet_format, 12, **header)
        + struct.pack("<B", car_idx)
        + _array(_fields(packet_format, "tyre_set"), MAX_TYRE_SETS, sets)
        + struct.pack("<B", fitted_idx)
    )


def build_motion_ex(packet_format: int, **header: Any) -> bytes:
    """id 13. 61 consecutive float32 values in both formats."""
    return build_header(packet_format, 13, **header) + struct.pack(
        "<" + "f" * MOTION_EX_FLOAT_COUNT, *([0.0] * MOTION_EX_FLOAT_COUNT)
    )


def build_lap_positions(
    packet_format: int, *, num_laps: int = 0, lap_start: int = 0, **header: Any
) -> bytes:
    """id 15."""
    grid = bytes(MAX_LAP_POSITION_LAPS * NUM_CARS[packet_format])
    return (
        build_header(packet_format, 15, **header)
        + struct.pack("<BB", num_laps, lap_start)
        + grid
    )


#: packet id -> builder, for tests that sweep every type.
BUILDERS = {
    0: build_motion,
    1: build_session,
    2: build_lap_data,
    3: lambda fmt, **kw: build_event(fmt, "SSTA", **kw),
    4: build_participants,
    5: build_car_setups,
    6: build_car_telemetry,
    7: build_car_status,
    8: build_final_classification,
    9: build_lobby_info,
    10: build_car_damage,
    11: build_session_history,
    12: build_tyre_sets,
    13: build_motion_ex,
    14: build_time_trial,
    15: build_lap_positions,
    16: build_car_telemetry_2,
}

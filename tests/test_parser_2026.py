"""Format 2026 (F1 25 "2026 Season Pack"): size pins, round-trips, real capture.

The size pins come from two independent places that agree exactly: the
``ISizeable.Size`` constants in volodymyr-fed/F1Game.UDP v26, and a live PS5
capture of the user's own session (see ``docs/spec-2026.md``).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from builders import (
    NUM_CARS,
    build_car_damage,
    build_car_status,
    build_car_telemetry,
    build_car_telemetry_2,
    build_event,
    build_final_classification,
    build_lap_data,
    build_motion,
    build_participants,
    build_session,
    build_session_history,
    build_time_trial,
    wheels,
)

from f126.parser import PacketParser
from f126.parser.spec_2026 import G_FORCE_DIVISOR_2026, SPEC_2026

FMT = 2026
RECV_MONO = 1_234_567_890
RECV_WALL = 1_700_000_000_000_000_000

# Wire sizes for format 2026. Left column: ``static int ISizeable.Size`` from
# F1Game.UDP v26 ``F1Game.UDP/Packets/*.cs``. Every one of these was also
# observed as the single fixed size for that id across 10,223 live packets
# (ids 8, 9 and 14 do not occur in a practice session and come from the C#
# library alone).
SIZES_2026 = {
    0: 1325,  # MotionDataPacket.cs               29 + 24 * CarMotionData(54)
    1: 926,  # SessionDataPacket.cs               29 + 897
    2: 1399,  # LapDataPacket.cs                  29 + 24 * LapData(57) + 2
    3: 45,  # EventDataPacket.cs                  29 + 4 + EventDetails payload(12)
    4: 1470,  # ParticipantsDataPacket.cs         29 + 1 + 24 * ParticipantData(60)
    5: 1233,  # CarSetupDataPacket.cs             29 + 24 * CarSetupData(50) + 4
    6: 1448,  # CarTelemetryDataPacket.cs         29 + 24 * CarTelemetryData(59) + 3
    7: 1445,  # CarStatusDataPacket.cs            29 + 24 * CarStatusData(59)
    8: 1134,  # FinalClassificationDataPacket.cs  29 + 1 + 24 * (46)
    9: 1062,  # LobbyInfoDataPacket.cs            29 + 1 + 24 * LobbyInfoData(43)
    10: 1133,  # CarDamageDataPacket.cs           29 + 24 * CarDamageData(46)
    11: 1460,  # SessionHistoryDataPacket.cs      29 + 7 + 100*14 + 8*3
    12: 231,  # TyreSetsDataPacket.cs             29 + 1 + 20 * TyreSetData(10) + 1
    13: 273,  # MotionExDataPacket.cs             29 + 61 * sizeof(float)
    14: 104,  # TimeTrialDataPacket.cs            29 + 3 * TimeTrialDataSet(25)
    15: 1231,  # LapPositionsDataPacket.cs        29 + 2 + 50 * 24
    16: 269,  # CarTelemetry2DataPacket.cs        29 + 24 * CarTelemetry2Data(10)
}

FIXTURES = Path(__file__).parent / "fixtures"
#: ~1 MB slice of the 10 Hz capture, committed so this test always runs.
TRIMMED_CAPTURE = FIXTURES / "bahrain_p1_2026_trimmed.cap"
#: Full captures are too large to commit; see tests/fixtures/.gitignore.
FULL_CAPTURES = (
    FIXTURES / "bahrain_p1_2026.cap",
    FIXTURES / "bahrain_p1_60hz_2026.cap",
)

_RECORD = struct.Struct("<QH")


def read_capture(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield ``(monotonic_ns, payload)`` from a ``<QH len> + payload`` capture."""
    blob = path.read_bytes()
    pos, end = 0, len(blob)
    while pos + _RECORD.size <= end:
        monotonic_ns, length = _RECORD.unpack_from(blob, pos)
        pos += _RECORD.size
        yield monotonic_ns, blob[pos : pos + length]
        pos += length


@pytest.fixture
def parser() -> PacketParser:
    return PacketParser()


def parse(parser: PacketParser, data: bytes):
    packet = parser.parse(data, RECV_MONO, RECV_WALL)
    assert packet is not None, f"packet was skipped or errored: {parser.counters}"
    return packet


# --------------------------------------------------------------------------
# Size pins
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("packet_id", "expected"), sorted(SIZES_2026.items()))
def test_wire_size_matches_reference(packet_id: int, expected: int) -> None:
    assert SPEC_2026.sizes[packet_id] == expected


def test_spec_covers_every_packet_id() -> None:
    assert set(SPEC_2026.sizes) == set(SIZES_2026)


def test_car_count_and_strides() -> None:
    # The single most consequential 2026 change: 24 car slots, not 22.
    assert SPEC_2026.num_cars == 24
    assert SPEC_2026.motion_stride == 54  # g-forces packed to int16
    assert SPEC_2026.lap_stride == 57  # unchanged
    assert SPEC_2026.telemetry_stride == 59  # engine temp narrowed to uint8
    assert SPEC_2026.status_stride == 59  # + ersHarvestLimitPerLap
    assert SPEC_2026.damage_stride == 46  # unchanged
    assert SPEC_2026.participant_stride == 60  # driver/network/team widened
    assert SPEC_2026.time_trial_stride == 25  # team widened


# --------------------------------------------------------------------------
# Round-trips
# --------------------------------------------------------------------------


def test_header_round_trip(parser: PacketParser) -> None:
    data = build_motion(
        FMT,
        session_uid=0x1234_5678_9ABC,
        session_time=99.5,
        frame_identifier=77,
        overall_frame_identifier=177,
        player_car_index=21,  # the real capture uses slot 21
        game_year=25,  # the Season Pack ships on F1 25, so gameYear stays 25
        game_minor_version=24,
    )
    header = parse(parser, data).header
    assert header.packet_format == 2026
    assert header.game_year == 25
    assert header.packet_id == 0
    assert header.session_uid == 0x1234_5678_9ABC
    assert header.session_time == pytest.approx(99.5)
    assert header.player_car_index == 21


def test_motion_round_trip_with_packed_g_forces(parser: PacketParser) -> None:
    # 2026 packs g-forces into int16 at 1024 counts per g.
    data = build_motion(
        FMT,
        player_car_index=21,
        cars={
            21: {
                "world_position_x": 101.5,
                "world_position_y": 202.25,
                "world_position_z": 303.75,
                "g_force_lateral": 2048,  # +2.0 g
                "g_force_longitudinal": -4608,  # -4.5 g
                "yaw": -1.25,
            }
        },
    )
    view = parse(parser, data).view
    assert view.world_x == pytest.approx(101.5)
    assert view.world_y == pytest.approx(202.25)
    assert view.world_z == pytest.approx(303.75)
    assert view.g_lat == pytest.approx(2.0)
    assert view.g_lon == pytest.approx(-4.5)
    assert view.yaw == pytest.approx(-1.25)


def test_g_force_divisor_is_documented_value() -> None:
    assert G_FORCE_DIVISOR_2026 == 1024.0
    assert SPEC_2026.g_force_divisor == 1024.0


def test_lap_data_round_trip_uses_24_slots(parser: PacketParser) -> None:
    cars = {
        i: {
            "car_position": i + 1,
            "last_lap_time_ms": 90_000 + i,
            "current_lap_time_ms": 45_000 + i,
            "sector1_time_ms_part": 345,
            "sector1_time_minutes_part": 1,
            "sector2_time_ms_part": 678,
            "sector2_time_minutes_part": 2,
            "delta_to_car_in_front_ms_part": 250,
            "delta_to_car_in_front_minutes_part": 1,
            "delta_to_race_leader_ms_part": 500,
            "delta_to_race_leader_minutes_part": 3,
            "lap_distance": 1234.5,
            "total_distance": 6789.25,
            "current_lap_num": 12,
            "pit_status": 1,
            "sector": 2,
            "current_lap_invalid": 1,
            "penalties": 5,
            "result_status": 2,
        }
        for i in range(NUM_CARS[FMT])
    }
    data = build_lap_data(FMT, player_car_index=23, cars=cars)
    view = parse(parser, data).view

    assert len(view.cars) == 24
    assert view.player is view.cars[23]
    player = view.player
    assert player.car_index == 23
    assert player.position == 24
    assert player.lap_number == 12
    assert player.lap_distance_m == pytest.approx(1234.5)
    assert player.total_distance_m == pytest.approx(6789.25)
    assert player.last_lap_ms == 90_023
    assert player.current_lap_ms == 45_023
    assert player.sector == 2
    assert player.sector1_ms == 60_345
    assert player.sector2_ms == 120_678
    assert player.lap_invalid is True
    assert player.penalties_s == 5
    assert player.pit_status == 1
    assert player.result_status == 2
    assert player.delta_to_car_ahead_ms == 60_250
    assert player.delta_to_leader_ms == 180_500
    # The 23rd and 24th slots only exist on 2026; make sure they are read.
    assert view.cars[22].last_lap_ms == 90_022
    assert view.cars[23].last_lap_ms == 90_023


def test_car_telemetry_round_trip(parser: PacketParser) -> None:
    player = {
        "speed": 336,
        "throttle": 1.0,
        "steer": 0.5,
        "brake": 0.0,
        "gear": 8,
        "engine_rpm": 12800,
        "drs": 1,  # present on the wire but meaningless in 2026
        "rev_lights_percent": 95,
        "engine_temperature": 114,  # uint8 in 2026
        **wheels("tyres_surface_temperature", 90, 91, 92, 93),
        **wheels("tyres_inner_temperature", 100, 101, 102, 103),
        **wheels("tyres_pressure", 22.5, 22.75, 23.0, 23.25),
        **wheels("brakes_temperature", 400, 401, 402, 403),
    }
    cars = {i: {"speed": 100 + i} for i in range(NUM_CARS[FMT])}
    cars[21] = player
    view = parse(
        parser, build_car_telemetry(FMT, player_car_index=21, cars=cars)
    ).view

    assert view.speed_kmh == pytest.approx(336.0)
    assert view.throttle == pytest.approx(1.0)
    assert view.steer == pytest.approx(0.5)
    assert view.brake == pytest.approx(0.0)
    assert view.gear == 8
    assert view.rpm == 12800
    assert view.rev_lights_percent == 95
    assert view.engine_temp == pytest.approx(114.0)
    assert (view.tyre_surface_temp.rl, view.tyre_surface_temp.fr) == (90, 93)
    assert (view.tyre_inner_temp.rr, view.tyre_inner_temp.fl) == (101, 102)
    assert view.tyre_pressure.rl == pytest.approx(22.5)
    assert (view.brake_temp.rl, view.brake_temp.fr) == (400, 403)
    # 2026 replaces DRS with active aero.
    assert view.drs_open is None
    assert len(view.opponent_speeds_kmh) == 24
    assert view.opponent_speeds_kmh[21] == pytest.approx(336.0)
    assert view.opponent_speeds_kmh[23] == pytest.approx(123.0)


def test_car_telemetry_2_merges_aero_mode(parser: PacketParser) -> None:
    """id 16 feeds ``aero_mode`` into the TelemetryView emitted by id 6."""
    # Before any CarTelemetry2 has arrived, aero_mode is simply unknown.
    first = parse(parser, build_car_telemetry(FMT, player_car_index=21)).view
    assert first.aero_mode is None

    telemetry_2 = build_car_telemetry_2(
        FMT,
        player_car_index=21,
        cars={21: {"active_aero_mode": 1, "overtake_active": 1}, 0: {"active_aero_mode": 0}},
    )
    packet = parser.parse(telemetry_2, RECV_MONO, RECV_WALL)
    # id 16 is parsed and counted, but emits no view of its own.
    assert packet is not None
    assert packet.view is None
    assert parser.counters["by_packet_id"]["parsed"][16] == 1
    assert parser.counters["errors_total"] == 0

    merged = parse(parser, build_car_telemetry(FMT, player_car_index=21)).view
    assert merged.aero_mode == 1


def test_car_telemetry_2_reads_the_player_slot(parser: PacketParser) -> None:
    data = build_car_telemetry_2(
        FMT,
        player_car_index=7,
        cars={i: {"active_aero_mode": i % 2} for i in range(NUM_CARS[FMT])},
    )
    parser.parse(data, RECV_MONO, RECV_WALL)
    view = parse(parser, build_car_telemetry(FMT, player_car_index=7)).view
    assert view.aero_mode == 1  # slot 7 is odd


def test_car_status_routes_energy_into_telemetry(parser: PacketParser) -> None:
    """On 2026 the energy system lives on TelemetryView, per types.py."""
    status = build_car_status(
        FMT,
        player_car_index=21,
        cars={
            21: {
                "fuel_in_tank": 40.5,
                "fuel_capacity": 100.0,
                "fuel_remaining_laps": 8.5,
                "actual_tyre_compound": 22,  # C6, added by the Season Pack
                "visual_tyre_compound": 16,
                "tyres_age_laps": 4,
                "vehicle_fia_flags": 3,
                "ers_store_energy": 4_000_000.0,
                "ers_deploy_mode": 2,
                "ers_harvested_this_lap_mguk": 1000.0,
                "ers_harvested_this_lap_mguh": 500.0,
                "ers_harvest_limit_per_lap": 2_000_000.0,
                "ers_deployed_this_lap": 900_000.0,
                "drs_allowed": 1,
            }
        },
    )
    view = parse(parser, status).view
    assert view.fuel_in_tank_kg == pytest.approx(40.5)
    assert view.fuel_capacity_kg == pytest.approx(100.0)
    assert view.fuel_remaining_laps == pytest.approx(8.5)
    assert view.tyre_compound_actual == 22
    assert view.tyre_compound_visual == 16
    assert view.tyre_age_laps == 4
    assert view.fia_flags == 3
    assert view.vehicle_fia_flags == 3
    assert view.drs_allowed is True
    # ERS fields are deliberately None on 2026 ...
    assert view.ers_store_j is None
    assert view.ers_deploy_mode is None
    assert view.ers_harvested_lap_j is None
    assert view.ers_deployed_lap_j is None

    # ... and reappear on the next TelemetryView instead.
    telemetry = parse(parser, build_car_telemetry(FMT, player_car_index=21)).view
    assert telemetry.energy_store_j == pytest.approx(4_000_000.0)
    assert telemetry.energy_deploy_mode == 2


def test_merge_cache_resets_on_new_session(parser: PacketParser) -> None:
    parser.parse(
        build_car_telemetry_2(
            FMT, player_car_index=0, session_uid=111, cars={0: {"active_aero_mode": 1}}
        ),
        RECV_MONO,
        RECV_WALL,
    )
    same = parse(
        parser, build_car_telemetry(FMT, player_car_index=0, session_uid=111)
    ).view
    assert same.aero_mode == 1

    # A different session UID must not inherit the previous session's state.
    other = parse(
        parser, build_car_telemetry(FMT, player_car_index=0, session_uid=222)
    ).view
    assert other.aero_mode is None


def test_car_damage_round_trip(parser: PacketParser) -> None:
    view = parse(
        parser,
        build_car_damage(
            FMT,
            player_car_index=21,
            cars={
                21: {
                    **wheels("tyres_wear", 1.5, 2.5, 3.5, 4.5),
                    **wheels("tyres_damage", 10, 11, 12, 13),
                    **wheels("brakes_damage", 20, 21, 22, 23),
                    "front_left_wing_damage": 30,
                    "front_right_wing_damage": 31,
                    "rear_wing_damage": 32,
                    "floor_damage": 33,
                    "diffuser_damage": 34,
                    "sidepod_damage": 35,
                    "gear_box_damage": 36,
                    "engine_damage": 37,
                }
            },
        ),
    ).view
    assert view.tyre_wear_pct.rl == pytest.approx(1.5)
    assert view.tyre_wear_pct.fr == pytest.approx(4.5)
    assert (view.tyre_damage_pct.rl, view.tyre_damage_pct.fr) == (10, 13)
    assert (view.brake_damage_pct.rr, view.brake_damage_pct.fl) == (21, 22)
    assert view.front_left_wing_pct == 30
    assert view.front_right_wing_pct == 31
    assert view.rear_wing_pct == 32
    assert view.floor_pct == 33
    assert view.diffuser_pct == 34
    assert view.sidepod_pct == 35
    assert view.gearbox_pct == 36
    assert view.engine_pct == 37


def test_session_round_trip_with_2026_tail(parser: PacketParser) -> None:
    """The 173-byte 2026 tail must not disturb the fields before it."""
    data = build_session(
        FMT,
        head={
            "session_type": 1,
            "track_id": 3,
            "track_length": 5408,
            "session_time_left": 1405,
            "session_duration": 1800,
            "total_laps": 2,
            "weather": 0,
            "track_temperature": 48,
            "air_temperature": 31,
            "pit_speed_limit": 80,
        },
        mid={"safety_car_status": 0},
        forecast=[{"time_offset": 15, "weather": 1, "track_temperature": 47,
                   "air_temperature": 30, "rain_percentage": 5}],
        tail={"num_drs_zones": 3, "active_aero_track_status": 1, "start_reaction_time": 0.25},
    )
    view = parse(parser, data).view
    assert view.session_type == 1
    assert view.track_id == 3
    assert view.track_length_m == 5408
    assert view.session_time_left_s == 1405
    assert view.session_duration_s == 1800
    assert view.total_laps == 2
    assert view.weather == 0
    assert view.track_temp_c == 48
    assert view.air_temp_c == 31
    assert view.safety_car_status == 0
    assert view.pit_speed_limit_kmh == 80
    assert len(view.forecast) == 1
    assert view.forecast[0].offset_min == 15
    assert view.forecast[0].rain_percentage == 5


def test_participants_round_trip_with_widened_ids(parser: PacketParser) -> None:
    data = build_participants(
        FMT,
        num_active=20,
        cars={
            0: {
                "name": b"VERSTAPPEN",
                "team_id": 478,  # RedBullRacing26 -- needs the widened uint16
                "driver_id": 197,
                "network_id": 65535,
                "race_number": 1,
                "ai_controlled": 0,
            },
            23: {"name": b"ROOKIE", "team_id": 486, "race_number": 87,
                 "ai_controlled": 1},
        },
    )
    view = parse(parser, data).view
    assert view.num_active == 20
    assert len(view.cars) == 24
    assert view.cars[0].name == "VERSTAPPEN"
    assert view.cars[0].team_id == 478
    assert view.cars[0].race_number == 1
    assert view.cars[0].is_ai is False
    assert view.cars[23].name == "ROOKIE"
    assert view.cars[23].team_id == 486  # Cadillac26
    assert view.cars[23].is_ai is True


def test_session_history_round_trip(parser: PacketParser) -> None:
    laps = [
        {
            "lap_time_ms": 95_000 + i,
            "sector1_time_ms": 31_000 + i,
            "sector2_time_ms": 400,
            "sector2_time_minutes": 1,
            "sector3_time_ms": 800,
            "lap_valid_bit_flags": 0x01 if i != 2 else 0x00,
        }
        for i in range(5)
    ]
    view = parse(
        parser,
        build_session_history(FMT, head={"car_idx": 21, "best_lap_time_lap_num": 2}, laps=laps),
    ).view
    assert view.car_index == 21
    assert view.num_laps == 5
    assert view.best_lap_number == 2
    assert len(view.laps) == 5
    assert view.laps[0].lap_number == 1
    assert view.laps[0].lap_time_ms == 95_000
    assert view.laps[0].sector1_ms == 31_000
    assert view.laps[0].sector2_ms == 60_400
    assert view.laps[0].sector3_ms == 800
    assert view.laps[0].valid is True
    assert view.laps[2].valid is False


def test_final_classification_round_trip(parser: PacketParser) -> None:
    view = parse(
        parser,
        build_final_classification(
            FMT,
            num_cars=24,
            rows={
                23: {
                    "position": 24,
                    "num_laps": 56,
                    "grid_position": 22,
                    "points": 0,
                    "num_pit_stops": 3,
                    "result_status": 4,
                    "best_lap_time_ms": 92_000,
                    "total_race_time": 5555.5,
                    "penalties_time": 10,
                }
            },
        ),
    ).view
    assert len(view.rows) == 24
    row = view.rows[23]
    assert row.car_index == 23
    assert row.position == 24
    assert row.num_laps == 56
    assert row.grid_position == 22
    assert row.num_pit_stops == 3
    assert row.result_status == 4
    assert row.best_lap_ms == 92_000
    assert row.total_race_time_s == pytest.approx(5555.5)
    assert row.penalties_s == 10


def test_time_trial_round_trip_with_widened_team(parser: PacketParser) -> None:
    view = parse(
        parser,
        build_time_trial(
            FMT,
            player_session_best={
                "car_idx": 0,
                "team_id": 484,  # McLaren26, needs uint16
                "lap_time_ms": 89_500,
                "sector1_time_ms": 29_000,
                "sector2_time_ms": 31_000,
                "sector3_time_ms": 29_500,
                "valid": 1,
            },
            personal_best={"car_idx": 255},
            rival={"car_idx": 3, "team_id": 477, "lap_time_ms": 90_000},
        ),
    ).view
    assert view.player_session_best is not None
    assert view.player_session_best.team_id == 484
    assert view.player_session_best.lap_time_ms == 89_500
    assert view.player_session_best.sector1_ms == 29_000
    assert view.player_session_best.valid is True
    assert view.personal_best is None
    assert view.rival is not None
    assert view.rival.team_id == 477


def test_collision_event_carries_severity(parser: PacketParser) -> None:
    """The 2026 COLL payload gained a third byte."""
    view = parse(parser, build_event(FMT, "COLL", bytes([3, 14, 2]))).view
    assert view.code == "COLL"
    assert view.details == {"vehicle1_idx": 3, "vehicle2_idx": 14, "severity": 2}


# --------------------------------------------------------------------------
# Real capture validation
# --------------------------------------------------------------------------


def _validate_capture(path: Path) -> dict:
    """Parse a whole capture and assert plausibility on the decoded values."""
    parser = PacketParser()
    observed_sizes: dict[int, set[int]] = {}
    seen_views = 0
    speeds: list[float] = []
    lap_distances: list[float] = []
    tyre_temps: list[float] = []
    session_types: set[int] = set()
    track_ids: set[int] = set()
    track_lengths: set[int] = set()

    for monotonic_ns, payload in read_capture(path):
        packet_id = payload[6]
        observed_sizes.setdefault(packet_id, set()).add(len(payload))
        packet = parser.parse(payload, monotonic_ns, monotonic_ns)
        if packet is None:
            continue
        view = packet.view
        if view is None:
            continue
        seen_views += 1
        name = type(view).__name__
        if name == "TelemetryView":
            speeds.append(view.speed_kmh)
            assert 0.0 <= view.throttle <= 1.0
            assert 0.0 <= view.brake <= 1.0
            assert -1 <= view.gear <= 8
            assert view.drs_open is None  # 2026 has no DRS
            for corner in (
                view.tyre_surface_temp.rl,
                view.tyre_surface_temp.rr,
                view.tyre_surface_temp.fl,
                view.tyre_surface_temp.fr,
            ):
                tyre_temps.append(corner)
        elif name == "LapView":
            lap_distances.append(view.player.lap_distance_m)
            assert len(view.cars) == 24
        elif name == "SessionView":
            session_types.add(view.session_type)
            track_ids.add(view.track_id)
            track_lengths.add(view.track_length_m)

    counters = parser.counters
    assert counters["errors_total"] == 0, f"errors on real capture: {counters}"
    assert counters["size_mismatch_total"] == 0
    assert counters["unknown_packet_id_total"] == 0
    assert counters["unknown_format_total"] == 0
    assert counters["parsed_total"] > 0
    assert seen_views > 0

    # Every id must have had exactly one size, matching the 2026 spec.
    for packet_id, sizes in observed_sizes.items():
        assert len(sizes) == 1, f"id {packet_id} had varying sizes {sizes}"
        assert SPEC_2026.sizes[packet_id] == next(iter(sizes)), (
            f"id {packet_id}: spec says {SPEC_2026.sizes[packet_id]}, wire says {sizes}"
        )

    # Plausibility of the decoded player values.
    assert min(speeds) >= 0.0 and max(speeds) <= 400.0
    assert max(speeds) > 200.0, "expected a representative on-track speed"
    assert min(tyre_temps) >= 40.0 and max(tyre_temps) <= 150.0
    track_length = next(iter(track_lengths))
    assert 5000 <= track_length <= 5500  # Bahrain
    assert track_ids == {3}  # Track.BAHRAIN
    assert session_types == {1}  # SessionType.PRACTICE_1
    assert min(lap_distances) >= -50.0
    assert max(lap_distances) <= track_length + 5.0

    return counters


def test_trimmed_real_capture_parses_cleanly() -> None:
    assert TRIMMED_CAPTURE.exists(), (
        f"missing committed fixture {TRIMMED_CAPTURE}; see docs/spec-2026.md"
    )
    counters = _validate_capture(TRIMMED_CAPTURE)
    # The trimmed slice covers every id the live session emits.
    assert set(counters["by_packet_id"]["parsed"]) == {0, 1, 2, 3, 4, 6, 7, 10, 11, 16}
    assert set(counters["by_packet_id"]["skipped"]) == {5, 12, 13, 15}


@pytest.mark.parametrize("path", FULL_CAPTURES, ids=lambda p: p.name)
def test_full_real_capture_parses_cleanly(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} is not committed (too large); see tests/fixtures/.gitignore")
    counters = _validate_capture(path)
    assert counters["parsed_total"] + counters["skipped_total"] > 5_000


def test_real_capture_g_forces_are_physically_plausible() -> None:
    """Cross-check G_FORCE_DIVISOR_2026 against real motion data.

    At the correct scale an F1 car peaks around 4-6 g braking and 3-5 g
    lateral. A wrong power-of-two divisor lands an order of magnitude out, so
    this pins the constant even though the EA spec page is unreachable.
    """
    path = TRIMMED_CAPTURE
    assert path.exists()
    parser = PacketParser()
    g_lat: list[float] = []
    g_lon: list[float] = []
    for monotonic_ns, payload in read_capture(path):
        packet = parser.parse(payload, monotonic_ns, monotonic_ns)
        if packet is None or type(packet.view).__name__ != "MotionView":
            continue
        g_lat.append(packet.view.g_lat)
        g_lon.append(packet.view.g_lon)

    assert g_lat and g_lon
    peak_lat = max(abs(v) for v in g_lat)
    peak_lon = max(abs(v) for v in g_lon)
    assert 2.0 <= peak_lat <= 7.0, f"lateral peak {peak_lat:.2f} g is not F1-like"
    assert 2.0 <= peak_lon <= 7.0, f"longitudinal peak {peak_lon:.2f} g is not F1-like"

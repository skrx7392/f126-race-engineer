"""Format 2025 (base F1 25): wire-size pins and full round-trips."""

from __future__ import annotations

import struct

import pytest
from builders import (
    build_car_damage,
    build_car_status,
    build_car_telemetry,
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
from f126.parser.spec_2025 import SPEC_2025

FMT = 2025
RECV_MONO = 1_234_567_890
RECV_WALL = 1_700_000_000_000_000_000

# Wire sizes for format 2025, taken from the packed C structs in
# MacManley/f1-25-udp (``src/*.h``, all ``#pragma pack(push, 1)``). Each value
# is ``29 (PacketHeader) + sizeof(body)``; see docs/spec-2025.md for the
# per-packet derivation.
SIZES_2025 = {
    0: 1349,  # PacketMotionData.h:        29 + 22 * sizeof(CarMotionData=60)
    1: 753,  # PacketSessionData.h:        29 + 724
    2: 1285,  # PacketLapData.h:           29 + 22 * sizeof(LapData=57) + 2
    3: 45,  # PacketEventData.h:           29 + 4 + sizeof(EventDataDetails=12)
    4: 1284,  # PacketParticipantData.h:   29 + 1 + 22 * sizeof(ParticipantData=57)
    5: 1133,  # PacketCarSetupData.h:      29 + 22 * sizeof(CarSetupData=50) + 4
    6: 1352,  # PacketCarTelemetryData.h:  29 + 22 * sizeof(CarTelemetryData=60) + 3
    7: 1239,  # PacketCarStatusData.h:     29 + 22 * sizeof(CarStatusData=55)
    8: 1042,  # PacketFinalClassification: 29 + 1 + 22 * sizeof(...=46)
    9: 954,  # PacketLobbyInfo.h:          29 + 1 + 22 * sizeof(LobbyInfoData=42)
    10: 1041,  # PacketCarDamageData.h:    29 + 22 * sizeof(CarDamageData=46)
    11: 1460,  # PacketSessionHistoryData: 29 + 7 + 100*14 + 8*3
    12: 231,  # PacketTyreSetData.h:       29 + 1 + 20 * sizeof(TyreSetData=10) + 1
    13: 273,  # PacketMotionEX.h:          29 + 61 * sizeof(float)
    14: 101,  # PacketTimeTrialData.h:     29 + 3 * sizeof(TimeTrialDataSet=24)
    15: 1131,  # PacketLapPositions.h:     29 + 2 + 50 * 22
}


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


@pytest.mark.parametrize(("packet_id", "expected"), sorted(SIZES_2025.items()))
def test_wire_size_matches_reference(packet_id: int, expected: int) -> None:
    assert SPEC_2025.sizes[packet_id] == expected


def test_no_car_telemetry_2_in_2025() -> None:
    # CarTelemetry2 (id 16) is a 2026 Season Pack addition and must not exist here.
    assert 16 not in SPEC_2025.sizes


def test_spec_covers_every_packet_id() -> None:
    assert set(SPEC_2025.sizes) == set(SIZES_2025)


def test_car_count_and_strides() -> None:
    assert SPEC_2025.num_cars == 22
    assert SPEC_2025.motion_stride == 60
    assert SPEC_2025.lap_stride == 57
    assert SPEC_2025.telemetry_stride == 60
    assert SPEC_2025.status_stride == 55
    assert SPEC_2025.damage_stride == 46
    assert SPEC_2025.participant_stride == 57
    assert SPEC_2025.time_trial_stride == 24


# --------------------------------------------------------------------------
# Round-trips
# --------------------------------------------------------------------------


def test_header_round_trip(parser: PacketParser) -> None:
    data = build_motion(
        FMT,
        session_uid=0xDEADBEEFCAFE,
        session_time=61.25,
        frame_identifier=999,
        overall_frame_identifier=1999,
        player_car_index=7,
        secondary_player_car_index=255,
        packet_version=1,
        game_year=25,
    )
    packet = parse(parser, data)
    header = packet.header
    assert header.packet_format == 2025
    assert header.game_year == 25
    assert header.packet_version == 1
    assert header.packet_id == 0
    assert header.session_uid == 0xDEADBEEFCAFE
    assert header.session_time == pytest.approx(61.25)
    assert header.frame_identifier == 999
    assert header.overall_frame_identifier == 1999
    assert header.player_car_index == 7
    assert header.secondary_player_car_index == 255
    assert packet.recv_monotonic_ns == RECV_MONO
    assert packet.recv_wall_ns == RECV_WALL


def test_motion_round_trip(parser: PacketParser) -> None:
    data = build_motion(
        FMT,
        player_car_index=3,
        cars={
            3: {
                "world_position_x": 101.5,
                "world_position_y": 202.25,
                "world_position_z": 303.75,
                "g_force_lateral": 2.5,
                "g_force_longitudinal": -4.0,
                "yaw": 1.5,
            }
        },
    )
    view = parse(parser, data).view
    assert view.world_x == pytest.approx(101.5)
    assert view.world_y == pytest.approx(202.25)
    assert view.world_z == pytest.approx(303.75)
    # 2025 carries g-forces as float32: no scaling is applied.
    assert view.g_lat == pytest.approx(2.5)
    assert view.g_lon == pytest.approx(-4.0)
    assert view.yaw == pytest.approx(1.5)


def _lap_car_values(seed: int) -> dict[str, object]:
    return {
        "last_lap_time_ms": 90_000 + seed,
        "current_lap_time_ms": 45_000 + seed,
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
        "car_position": seed + 1,
        "current_lap_num": 12,
        "pit_status": 2,
        "sector": 1,
        "current_lap_invalid": 1,
        "penalties": 5,
        "result_status": 3,
    }


def test_lap_data_round_trip_player_and_opponents(parser: PacketParser) -> None:
    cars = {i: _lap_car_values(i) for i in range(SPEC_2025.num_cars)}
    data = build_lap_data(FMT, player_car_index=5, cars=cars)
    view = parse(parser, data).view

    assert len(view.cars) == SPEC_2025.num_cars
    assert view.player is view.cars[5]

    player = view.player
    assert player.car_index == 5
    assert player.position == 6
    assert player.lap_number == 12
    assert player.lap_distance_m == pytest.approx(1234.5)
    assert player.total_distance_m == pytest.approx(6789.25)
    assert player.last_lap_ms == 90_005
    assert player.current_lap_ms == 45_005
    assert player.sector == 1
    assert player.sector1_ms == 60_345  # 1 min + 345 ms
    assert player.sector2_ms == 120_678  # 2 min + 678 ms
    assert player.lap_invalid is True
    assert player.penalties_s == 5
    assert player.pit_status == 2
    assert player.result_status == 3
    assert player.delta_to_car_ahead_ms == 60_250
    assert player.delta_to_leader_ms == 180_500

    # Each car must be read from its own slot, not the player's.
    for i, car in enumerate(view.cars):
        assert car.car_index == i
        assert car.position == i + 1
        assert car.last_lap_ms == 90_000 + i


def test_car_telemetry_round_trip(parser: PacketParser) -> None:
    player = {
        "speed": 287,
        "throttle": 0.75,
        "steer": -0.5,
        "brake": 0.25,
        "gear": 7,
        "engine_rpm": 11500,
        "drs": 1,
        "rev_lights_percent": 88,
        "engine_temperature": 96,
        **wheels("tyres_surface_temperature", 90, 91, 92, 93),
        **wheels("tyres_inner_temperature", 100, 101, 102, 103),
        **wheels("tyres_pressure", 22.5, 22.75, 23.0, 23.25),
        **wheels("brakes_temperature", 400, 401, 402, 403),
    }
    cars = {i: {"speed": 100 + i} for i in range(SPEC_2025.num_cars)}
    cars[4] = player
    data = build_car_telemetry(FMT, player_car_index=4, cars=cars)
    view = parse(parser, data).view

    assert view.speed_kmh == pytest.approx(287.0)
    assert view.throttle == pytest.approx(0.75)
    assert view.steer == pytest.approx(-0.5)
    assert view.brake == pytest.approx(0.25)
    assert view.gear == 7
    assert view.rpm == 11500
    assert view.drs_open is True  # 2025 reports DRS; aero_mode is 2026-only
    assert view.aero_mode is None
    assert view.rev_lights_percent == 88
    assert view.engine_temp == pytest.approx(96.0)
    assert (view.tyre_surface_temp.rl, view.tyre_surface_temp.fr) == (90, 93)
    assert (view.tyre_inner_temp.rr, view.tyre_inner_temp.fl) == (101, 102)
    assert view.tyre_pressure.rl == pytest.approx(22.5)
    assert view.tyre_pressure.fr == pytest.approx(23.25)
    assert (view.brake_temp.rl, view.brake_temp.fr) == (400, 403)
    # 2025 has no energy-system extras.
    assert view.energy_store_j is None
    assert view.energy_deploy_mode is None

    assert len(view.opponent_speeds_kmh) == SPEC_2025.num_cars
    assert view.opponent_speeds_kmh[4] == pytest.approx(287.0)
    for i in (0, 1, 9, 21):
        if i != 4:
            assert view.opponent_speeds_kmh[i] == pytest.approx(100.0 + i)


def test_negative_gear_round_trips(parser: PacketParser) -> None:
    data = build_car_telemetry(FMT, player_car_index=0, cars={0: {"gear": -1}})
    assert parse(parser, data).view.gear == -1


def test_car_status_round_trip(parser: PacketParser) -> None:
    data = build_car_status(
        FMT,
        player_car_index=2,
        cars={
            2: {
                "fuel_in_tank": 55.5,
                "fuel_capacity": 110.0,
                "fuel_remaining_laps": 3.25,
                "actual_tyre_compound": 18,
                "visual_tyre_compound": 17,
                "tyres_age_laps": 9,
                "vehicle_fia_flags": -1,
                "ers_store_energy": 3_500_000.0,
                "ers_deploy_mode": 3,
                "ers_harvested_this_lap_mguk": 1000.0,
                "ers_harvested_this_lap_mguh": 500.0,
                "ers_deployed_this_lap": 250_000.0,
                "drs_allowed": 1,
            }
        },
    )
    view = parse(parser, data).view
    assert view.fuel_in_tank_kg == pytest.approx(55.5)
    assert view.fuel_capacity_kg == pytest.approx(110.0)
    assert view.fuel_remaining_laps == pytest.approx(3.25)
    assert view.tyre_compound_actual == 18
    assert view.tyre_compound_visual == 17
    assert view.tyre_age_laps == 9
    assert view.fia_flags == -1  # int8, sign-extended
    assert view.vehicle_fia_flags == -1
    # 2025 keeps the ERS system on StatusView.
    assert view.ers_store_j == pytest.approx(3_500_000.0)
    assert view.ers_deploy_mode == 3
    assert view.ers_harvested_lap_j == pytest.approx(1500.0)  # MGU-K + MGU-H
    assert view.ers_deployed_lap_j == pytest.approx(250_000.0)
    assert view.drs_allowed is True


def test_car_damage_round_trip(parser: PacketParser) -> None:
    data = build_car_damage(
        FMT,
        player_car_index=1,
        cars={
            1: {
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
    )
    view = parse(parser, data).view
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


def test_session_round_trip(parser: PacketParser) -> None:
    forecast = [
        {
            "time_offset": 5,
            "weather": 3,
            "track_temperature": 40,
            "air_temperature": 25,
            "rain_percentage": 60,
        },
        {
            "time_offset": 10,
            "weather": 4,
            "track_temperature": 38,
            "air_temperature": 24,
            "rain_percentage": 90,
        },
    ]
    data = build_session(
        FMT,
        head={
            "session_type": 10,
            "track_id": 3,
            "track_length": 5412,
            "session_time_left": 1200,
            "session_duration": 1800,
            "total_laps": 57,
            "weather": 2,
            "track_temperature": 42,
            "air_temperature": 28,
            "pit_speed_limit": 80,
        },
        mid={"safety_car_status": 2},
        forecast=forecast,
    )
    view = parse(parser, data).view
    assert view.session_type == 10
    assert view.track_id == 3
    assert view.track_length_m == 5412
    assert view.session_time_left_s == 1200
    assert view.session_duration_s == 1800
    assert view.total_laps == 57
    assert view.weather == 2
    assert view.track_temp_c == 42
    assert view.air_temp_c == 28
    assert view.safety_car_status == 2
    assert view.pit_speed_limit_kmh == 80

    assert len(view.forecast) == 2
    assert view.forecast[0].offset_min == 5
    assert view.forecast[0].weather == 3
    assert view.forecast[0].track_temp_c == 40
    assert view.forecast[0].air_temp_c == 25
    assert view.forecast[0].rain_percentage == 60
    assert view.forecast[1].offset_min == 10
    assert view.forecast[1].rain_percentage == 90


def test_session_negative_temperatures(parser: PacketParser) -> None:
    data = build_session(
        FMT, head={"track_temperature": -5, "air_temperature": -12, "track_id": -1}
    )
    view = parse(parser, data).view
    assert view.track_temp_c == -5
    assert view.air_temp_c == -12
    assert view.track_id == -1


def test_participants_round_trip(parser: PacketParser) -> None:
    data = build_participants(
        FMT,
        num_active=20,
        cars={
            0: {
                "name": b"VERSTAPPEN",
                "team_id": 2,
                "race_number": 1,
                "ai_controlled": 0,
            },
            1: {"name": b"NORRIS", "team_id": 8, "race_number": 4, "ai_controlled": 1},
        },
    )
    view = parse(parser, data).view
    assert view.num_active == 20
    assert len(view.cars) == SPEC_2025.num_cars
    assert view.cars[0].car_index == 0
    assert view.cars[0].name == "VERSTAPPEN"
    assert view.cars[0].team_id == 2
    assert view.cars[0].race_number == 1
    assert view.cars[0].is_ai is False
    assert view.cars[1].name == "NORRIS"
    assert view.cars[1].team_id == 8
    assert view.cars[1].is_ai is True


def test_participant_name_utf8_and_truncation(parser: PacketParser) -> None:
    # 32 bytes exactly, no NUL terminator, plus a multi-byte character.
    name = "Kimi Räikkönen".encode()
    data = build_participants(FMT, cars={0: {"name": name}})
    view = parse(parser, data).view
    assert view.cars[0].name == "Kimi Räikkönen"


def test_session_history_round_trip(parser: PacketParser) -> None:
    laps = [
        {
            "lap_time_ms": 91_000 + i,
            "sector1_time_ms": 30_000 + i,
            "sector1_time_minutes": 0,
            "sector2_time_ms": 500,
            "sector2_time_minutes": 1,
            "sector3_time_ms": 700,
            "sector3_time_minutes": 0,
            "lap_valid_bit_flags": 0x0F if i % 2 == 0 else 0x0E,
        }
        for i in range(4)
    ]
    data = build_session_history(
        FMT, head={"car_idx": 9, "best_lap_time_lap_num": 3}, laps=laps
    )
    view = parse(parser, data).view
    assert view.car_index == 9
    assert view.num_laps == 4
    assert view.best_lap_number == 3
    assert len(view.laps) == 4
    assert view.laps[0].lap_number == 1
    assert view.laps[0].lap_time_ms == 91_000
    assert view.laps[0].sector1_ms == 30_000
    assert view.laps[0].sector2_ms == 60_500  # 1 minute + 500 ms
    assert view.laps[0].sector3_ms == 700
    assert view.laps[0].valid is True
    # bit 0 clear means the lap itself is invalid even if the sectors are set.
    assert view.laps[1].valid is False
    assert view.laps[3].lap_number == 4


def test_final_classification_round_trip(parser: PacketParser) -> None:
    data = build_final_classification(
        FMT,
        num_cars=2,
        rows={
            0: {
                "position": 1,
                "num_laps": 57,
                "grid_position": 3,
                "points": 25,
                "num_pit_stops": 2,
                "result_status": 3,
                "best_lap_time_ms": 88_123,
                "total_race_time": 5432.125,
                "penalties_time": 5,
            },
            1: {"position": 2, "num_laps": 57, "best_lap_time_ms": 88_500},
        },
    )
    view = parse(parser, data).view
    assert len(view.rows) == 2
    row = view.rows[0]
    assert row.car_index == 0
    assert row.position == 1
    assert row.num_laps == 57
    assert row.grid_position == 3
    assert row.points == 25
    assert row.num_pit_stops == 2
    assert row.result_status == 3
    assert row.best_lap_ms == 88_123
    assert row.total_race_time_s == pytest.approx(5432.125)
    assert row.penalties_s == 5
    assert view.rows[1].car_index == 1
    assert view.rows[1].best_lap_ms == 88_500


def test_time_trial_round_trip(parser: PacketParser) -> None:
    data = build_time_trial(
        FMT,
        player_session_best={
            "car_idx": 0,
            "team_id": 5,
            "lap_time_ms": 90_000,
            "sector1_time_ms": 30_000,
            "sector2_time_ms": 31_000,
            "sector3_time_ms": 29_000,
            "valid": 1,
        },
        personal_best={"car_idx": 0, "lap_time_ms": 89_000, "valid": 1},
        rival={"car_idx": 255},  # 255 means "no rival set"
    )
    view = parse(parser, data).view
    best = view.player_session_best
    assert best is not None
    assert best.car_index == 0
    assert best.team_id == 5
    assert best.lap_time_ms == 90_000
    assert best.sector1_ms == 30_000
    assert best.sector2_ms == 31_000
    assert best.sector3_ms == 29_000
    assert best.valid is True
    assert view.personal_best is not None
    assert view.personal_best.lap_time_ms == 89_000
    assert view.rival is None


@pytest.mark.parametrize(
    ("code", "payload", "expected"),
    [
        ("SSTA", b"", {}),
        ("SEND", b"", {}),
        ("CHQF", b"", {}),
        ("DRSE", b"", {}),
        ("LGOT", b"", {}),
        ("RDFL", b"", {}),
        (
            "FTLP",
            struct.pack("<Bf", 7, 88.125),
            {"vehicle_idx": 7, "lap_time_s": 88.125},
        ),
        ("RTMT", bytes([3, 8]), {"vehicle_idx": 3, "reason": 8}),
        ("DRSD", bytes([2]), {"reason": 2}),
        ("TMPT", bytes([11]), {"vehicle_idx": 11}),
        ("RCWN", bytes([1]), {"vehicle_idx": 1}),
        ("DTSV", bytes([4]), {"vehicle_idx": 4}),
        ("STLG", bytes([3]), {"num_lights": 3}),
        (
            "PENA",
            bytes([4, 17, 6, 9, 5, 12, 2]),
            {
                "penalty_type": 4,
                "infringement_type": 17,
                "vehicle_idx": 6,
                "other_vehicle_idx": 9,
                "time_s": 5,
                "lap_num": 12,
                "places_gained": 2,
            },
        ),
        (
            "SGSV",
            struct.pack("<Bf", 8, 10.5),
            {"vehicle_idx": 8, "stop_time_s": 10.5},
        ),
        (
            "FLBK",
            struct.pack("<If", 1234, 56.25),
            {"flashback_frame_identifier": 1234, "flashback_session_time": 56.25},
        ),
        ("BUTN", struct.pack("<I", 0x00000401), {"button_status": 0x401}),
        (
            "OVTK",
            bytes([5, 12]),
            {"overtaking_vehicle_idx": 5, "being_overtaken_vehicle_idx": 12},
        ),
        ("SCAR", bytes([2, 0]), {"safety_car_type": 2, "event_type": 0}),
        ("COLL", bytes([3, 14]), {"vehicle1_idx": 3, "vehicle2_idx": 14}),
    ],
)
def test_event_round_trip(
    parser: PacketParser, code: str, payload: bytes, expected: dict
) -> None:
    view = parse(parser, build_event(FMT, code, payload)).view
    assert view.code == code
    for key, value in expected.items():
        assert view.details[key] == pytest.approx(value)
    assert set(view.details) == set(expected)


def test_event_speed_trap_round_trip(parser: PacketParser) -> None:
    payload = struct.pack("<BfBBBf", 9, 331.5, 1, 1, 9, 340.25)
    view = parse(parser, build_event(FMT, "SPTP", payload)).view
    assert view.code == "SPTP"
    assert view.details["vehicle_idx"] == 9
    assert view.details["speed_kmh"] == pytest.approx(331.5)
    assert view.details["overall_fastest_in_session"] == 1
    assert view.details["driver_fastest_in_session"] == 1
    assert view.details["fastest_vehicle_idx_in_session"] == 9
    assert view.details["fastest_speed_in_session"] == pytest.approx(340.25)


def test_unknown_event_code_is_not_an_error(parser: PacketParser) -> None:
    # A future patch adding an event code must not break capture.
    view = parse(parser, build_event(FMT, "ZZZZ", b"\x01\x02")).view
    assert view.code == "ZZZZ"
    assert view.details == {}
    assert parser.counters["errors_total"] == 0

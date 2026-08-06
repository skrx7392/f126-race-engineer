"""LiveState: delta reference lifecycle, derived metrics, events, payload shapes.

The payload tests validate against a hand-transcribed schema of `docs/ws-protocol.md`
(required keys + types, recursively) rather than eyeballing dicts, so a rename in
`live.py` fails loudly here.
"""

from __future__ import annotations

from typing import Any

import pytest

from f126.config import Config
from f126.state import live as live_module
from f126.state.live import LapTrace, LiveState, resolve_track_name
from f126.state.session import SessionKey
from f126.types import (
    CarLap,
    DamageView,
    EventView,
    HistoryLap,
    HistoryView,
    LapView,
    MotionView,
    PacketHeader,
    PacketId,
    ParsedPacket,
    ParticipantsView,
    ParticipantView,
    SessionView,
    StatusView,
    TelemetryView,
    TimeTrialSet,
    TimeTrialView,
    WheelSet,
)

TRACK_LENGTH_M = 5000

# --------------------------------------------------------------------------
# shape checker
# --------------------------------------------------------------------------

NUM = (int, float)


def opt(spec: Any) -> tuple[Any, ...]:
    return ("opt", spec)


def listof(spec: Any, length: int | None = None) -> tuple[Any, ...]:
    return ("list", spec, length)


def oneof(*values: Any) -> tuple[Any, ...]:
    return ("oneof", frozenset(values))


def check_shape(value: Any, spec: Any, path: str = "$") -> None:
    """Recursively assert `value` matches `spec`. Extra keys are allowed (additive)."""
    if isinstance(spec, tuple) and spec and isinstance(spec[0], str):
        kind = spec[0]
        if kind == "opt":
            if value is not None:
                check_shape(value, spec[1], path)
            return
        if kind == "list":
            assert isinstance(value, list), f"{path}: expected list, got {type(value).__name__}"
            if spec[2] is not None:
                assert len(value) == spec[2], f"{path}: expected {spec[2]} items, got {len(value)}"
            for index, item in enumerate(value):
                check_shape(item, spec[1], f"{path}[{index}]")
            return
        if kind == "oneof":
            assert value in spec[1], f"{path}: {value!r} not one of {sorted(map(str, spec[1]))}"
            return
        raise AssertionError(f"bad spec at {path}: {spec!r}")
    if isinstance(spec, dict):
        assert isinstance(value, dict), f"{path}: expected object, got {type(value).__name__}"
        for key, sub in spec.items():
            assert key in value, f"{path}: missing key {key!r}"
            check_shape(value[key], sub, f"{path}.{key}")
        return
    assert isinstance(value, spec), (
        f"{path}: expected {spec}, got {type(value).__name__} ({value!r})"
    )


WHEELS = opt(listof(NUM, 4))
SECTOR_TRIPLE = listof(opt(int), 3)

FAST_SHAPE = {
    "type": oneof("fast"),
    "t": NUM,
    "speed_kmh": NUM,
    "gear": int,
    "rpm": int,
    "throttle": NUM,
    "brake": NUM,
    "steer": NUM,
    "drs_open": opt(bool),
    "aero_mode": opt(int),
    "ers_deploy_mode": opt(int),
    "rev_lights_percent": int,
    "lap_number": int,
    "lap_distance_m": NUM,
    "current_lap_ms": int,
    "delta_best_ms": opt(int),
    "delta_kind": opt(oneof("session_best", "personal_best", "race_best")),
}

SESSION_SHAPE = {
    "session_uid": str,
    "segment": int,
    "packet_format": int,
    "session_type": int,
    "session_kind": oneof("practice", "quali", "race", "time_trial", "other"),
    "track_id": int,
    "track_name": str,
    "time_left_s": opt(int),
    "duration_s": opt(int),
    "total_laps": opt(int),
    "safety_car": int,
    "fia_flag": int,
    "weather": int,
    "track_temp_c": opt(int),
    "air_temp_c": opt(int),
    "forecast": listof({"offset_min": int, "weather": int, "rain_pct": int}),
    "stalled": bool,
    "joined_in_progress": bool,
}

TOWER_SHAPE = {
    "car_index": int,
    "position": int,
    "name": str,
    "team_id": int,
    "is_player": bool,
    "lap_number": int,
    "last_lap_ms": opt(int),
    "gap_ahead_ms": opt(int),
    "gap_leader_ms": opt(int),
    "compound_visual": opt(int),
    "tyre_age_laps": opt(int),
    "pit_status": int,
    "penalties_s": int,
    "result_status": int,
}

SLOW_SHAPE = {
    "type": oneof("slow"),
    "session": SESSION_SHAPE,
    "tower": listof(TOWER_SHAPE),
    "tyres": {
        "surface_temp_c": WHEELS,
        "inner_temp_c": WHEELS,
        "pressure_psi": WHEELS,
        "wear_pct": WHEELS,
        "wear_rate_pct_per_lap": WHEELS,
        "projected_wear_end_pct": WHEELS,
        "compound_actual": opt(int),
        "compound_visual": opt(int),
        "age_laps": opt(int),
    },
    "fuel": {
        "in_tank_kg": opt(NUM),
        "remaining_laps": opt(NUM),
        "laps_left_in_session": opt(int),
        "delta_laps": opt(NUM),
        "burn_last_lap_kg": opt(NUM),
    },
    "energy": {
        "store_j": opt(NUM),
        "store_pct": opt(NUM),
        "deploy_mode": opt(int),
        "harvested_lap_j": opt(NUM),
        "deployed_lap_j": opt(NUM),
    },
    "damage": {
        "front_left_wing_pct": opt(int),
        "front_right_wing_pct": opt(int),
        "rear_wing_pct": opt(int),
        "floor_pct": opt(int),
        "diffuser_pct": opt(int),
        "sidepod_pct": opt(int),
        "gearbox_pct": opt(int),
        "engine_pct": opt(int),
    },
    "pace": {
        "last_3_avg_ms": opt(int),
        "ahead_last_3_avg_ms": opt(int),
        "behind_last_3_avg_ms": opt(int),
    },
    "sectors": {
        "current_lap": SECTOR_TRIPLE,
        "best_lap": SECTOR_TRIPLE,
        "session_best": SECTOR_TRIPLE,
        "last_lap_valid": opt(bool),
    },
    "timetrial": opt(
        {
            "pb_ms": opt(int),
            "rival_ms": opt(int),
            "pb_sectors": SECTOR_TRIPLE,
            "rival_sectors": SECTOR_TRIPLE,
        }
    ),
    "health": {
        "packets_per_sec": int,
        "parse_errors_total": int,
        "kernel_drops_total": int,
        "last_packet_age_ms": opt(int),
        "ws_clients": int,
    },
}

EVENT_CODES = (
    "SECTOR",
    "LAP",
    "LAP_INVALID",
    "PIT_IN",
    "PIT_OUT",
    "PENALTY",
    "FLAG",
    "SC",
    "FLASHBACK",
    "SESSION_START",
    "SESSION_END",
    "CHEQUERED",
    "FASTEST_LAP",
    "DRS",
    "STALLED",
)

EVENT_SHAPE = {
    "type": oneof("event"),
    "t": NUM,
    "code": oneof(*EVENT_CODES),
    "data": dict,
}

SNAPSHOT_SHAPE = {
    "type": oneof("snapshot"),
    "protocol_version": int,
    "session": SESSION_SHAPE,
    "fast": opt(FAST_SHAPE),
    "slow": opt(SLOW_SHAPE),
    "recent_events": listof(EVENT_SHAPE),
}


# --------------------------------------------------------------------------
# packet fixtures
# --------------------------------------------------------------------------


def cfg() -> Config:
    conf = Config()
    conf.telemetry_hz = 20
    conf.wear_hz = 1
    return conf


def wheelset(a: float = 1.0, b: float = 2.0, c: float = 3.0, d: float = 4.0) -> WheelSet:
    return WheelSet(a, b, c, d)


def car(
    idx: int = 0,
    *,
    position: int = 1,
    lap: int = 1,
    dist: float = 0.0,
    last: int = 0,
    cur: int = 0,
    sector: int = 0,
    s1: int = 0,
    s2: int = 0,
    invalid: bool = False,
    pen: int = 0,
    pit: int = 0,
) -> CarLap:
    return CarLap(
        car_index=idx,
        position=position,
        lap_number=lap,
        lap_distance_m=dist,
        total_distance_m=dist,
        last_lap_ms=last,
        current_lap_ms=cur,
        sector=sector,
        sector1_ms=s1,
        sector2_ms=s2,
        lap_invalid=invalid,
        penalties_s=pen,
        pit_status=pit,
        result_status=2,
        delta_to_car_ahead_ms=300,
        delta_to_leader_ms=1200,
    )


def lapview(player: CarLap, *others: CarLap) -> LapView:
    return LapView(player=player, cars=[player, *others])


def telemetry(speed: float = 250.0) -> TelemetryView:
    return TelemetryView(
        speed_kmh=speed,
        throttle=1.0,
        brake=0.0,
        steer=-0.12,
        gear=7,
        rpm=11450,
        drs_open=None,
        aero_mode=1,
        tyre_surface_temp=wheelset(98, 97, 92, 94),
        tyre_inner_temp=wheelset(102, 101, 96, 97),
        tyre_pressure=wheelset(21.5, 21.6, 23.0, 23.1),
        brake_temp=wheelset(400, 410, 420, 430),
        engine_temp=110.0,
        rev_lights_percent=60,
        energy_store_j=2_800_000.0,
        energy_deploy_mode=2,
    )


def status(fuel: float = 43.2, flags: int = 0, age: int = 8) -> StatusView:
    return StatusView(
        fuel_in_tank_kg=fuel,
        fuel_capacity_kg=110.0,
        fuel_remaining_laps=22.4,
        tyre_compound_actual=18,
        tyre_compound_visual=16,
        tyre_age_laps=age,
        fia_flags=flags,
        ers_store_j=None,
        ers_deploy_mode=None,
        ers_harvested_lap_j=None,
        ers_deployed_lap_j=None,
        drs_allowed=True,
    )


def damage(wear: float = 10.0) -> DamageView:
    return DamageView(
        tyre_wear_pct=wheelset(wear, wear, wear, wear),
        tyre_damage_pct=wheelset(0, 0, 0, 0),
        brake_damage_pct=wheelset(0, 0, 0, 0),
        front_left_wing_pct=0,
        front_right_wing_pct=5,
        rear_wing_pct=0,
        floor_pct=0,
        diffuser_pct=0,
        sidepod_pct=0,
        gearbox_pct=3,
        engine_pct=7,
    )


def sessionview(session_type: int = 5, total_laps: int = 0, safety_car: int = 0) -> SessionView:
    return SessionView(
        session_type=session_type,
        track_id=3,
        track_length_m=TRACK_LENGTH_M,
        session_time_left_s=1740,
        session_duration_s=3600,
        total_laps=total_laps,
        weather=1,
        track_temp_c=31,
        air_temp_c=24,
        safety_car_status=safety_car,
        pit_speed_limit_kmh=80,
    )


class Sim:
    """Drives a LiveState with headers that advance the way the game's do."""

    def __init__(self, session_type: int = 5, *, total_laps: int = 0, uid: int = 1234) -> None:
        self.live = LiveState(cfg())
        self.uid = uid
        self.t = 0.0
        self.frame = 0
        self.overall = 0
        self.mono = 1_000_000_000
        self.live.on_session_open(SessionKey(uid, 0), False)
        self.feed(PacketId.SESSION, sessionview(session_type, total_laps), dt=0.0)

    def packet(self, packet_id: PacketId, view: Any, *, dt: float = 0.05) -> ParsedPacket:
        self.t += dt
        self.frame += 1
        self.overall += 1
        self.mono += int(dt * 1e9)
        header = PacketHeader(
            packet_format=2026,
            game_year=26,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=self.uid,
            session_time=self.t,
            frame_identifier=self.frame,
            overall_frame_identifier=self.overall,
            player_car_index=0,
            secondary_player_car_index=255,
        )
        return ParsedPacket(
            header=header, view=view, recv_monotonic_ns=self.mono, recv_wall_ns=self.mono
        )

    def feed(self, packet_id: PacketId, view: Any, *, dt: float = 0.05) -> None:
        self.live.update(self.packet(packet_id, view, dt=dt))

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.live.events.empty():
            out.append(self.live.events.get_nowait())
        return out

    def codes(self) -> list[str]:
        return [event["code"] for event in self.drain()]

    def drive_lap(self, lap: int, lap_time_ms: int, *, invalid: bool = False) -> None:
        """Feed a full lap of LapData samples at 500 m intervals (linear pace)."""
        for step in range(1, 10):
            dist = float(step * 500)
            self.feed(
                PacketId.LAP_DATA,
                lapview(
                    car(
                        lap=lap,
                        dist=dist,
                        cur=int(dist * lap_time_ms / TRACK_LENGTH_M),
                        invalid=invalid,
                    )
                ),
            )

    def complete_lap(self, next_lap: int, lap_time_ms: int) -> None:
        self.feed(PacketId.LAP_DATA, lapview(car(lap=next_lap, last=lap_time_ms, dist=10.0)))


# --------------------------------------------------------------------------
# LapTrace
# --------------------------------------------------------------------------


def test_lap_trace_interpolates_between_samples() -> None:
    trace = LapTrace()
    trace.append(1000.0, 20000, 1.0)
    trace.append(2000.0, 40000, 2.0)
    assert trace.elapsed_at(1500.0) == pytest.approx(30000.0)
    assert trace.elapsed_at(500.0) == pytest.approx(10000.0)  # from the start line
    assert trace.elapsed_at(9999.0) == pytest.approx(40000.0)  # clamped past the end


def test_lap_trace_rejects_non_monotonic_distance() -> None:
    trace = LapTrace()
    trace.append(1000.0, 20000, 1.0)
    trace.append(900.0, 21000, 1.1)
    assert len(trace) == 1


def test_lap_trace_truncate_after_drops_the_tail() -> None:
    trace = LapTrace()
    for step in range(1, 6):
        trace.append(step * 1000.0, step * 20000, float(step))
    assert trace.truncate_after(2.5) == 3
    assert len(trace) == 2
    trace.append(2100.0, 42000, 2.6)  # the space freed by the rewind is reusable
    assert len(trace) == 3


# --------------------------------------------------------------------------
# delta reference lifecycle
# --------------------------------------------------------------------------


def test_delta_reference_is_set_by_the_first_valid_lap() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    assert sim.live.reference_lap_ms == 90000

    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=2500.0, cur=44000)))
    delta, kind = sim.live.delta()
    assert delta == -1000
    assert kind == "session_best"


def test_faster_valid_lap_replaces_the_reference() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    sim.drive_lap(2, 88000)
    sim.complete_lap(3, 88000)
    assert sim.live.reference_lap_ms == 88000

    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, dist=2500.0, cur=44000)))
    delta, _ = sim.live.delta()
    assert delta == 0  # reference elapsed at half distance is 44000 now


def test_slower_lap_does_not_replace_the_reference() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    sim.drive_lap(2, 95000)
    sim.complete_lap(3, 95000)
    assert sim.live.reference_lap_ms == 90000


def test_invalid_lap_never_becomes_the_reference() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    sim.drive_lap(2, 85000, invalid=True)
    sim.complete_lap(3, 85000)
    assert sim.live.reference_lap_ms == 90000


def test_lap_invalid_is_exposed_and_delta_kind_survives() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    sim.feed(PacketId.CAR_TELEMETRY, telemetry())
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=2500.0, cur=44000, invalid=True)))
    fast = sim.live.get_fast()
    assert fast["lap_invalid"] is True
    assert fast["delta_kind"] == "session_best"
    assert fast["delta_best_ms"] == -1000


def test_delta_kind_follows_session_kind() -> None:
    for session_type, expected in ((5, "session_best"), (15, "race_best"), (18, "personal_best")):
        sim = Sim(session_type=session_type)
        sim.drive_lap(1, 90000)
        sim.complete_lap(2, 90000)
        sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=1000.0, cur=18000)))
        assert sim.live.delta()[1] == expected


def test_flashback_truncates_the_current_lap_trace() -> None:
    sim = Sim(session_type=5)
    sim.drive_lap(1, 90000)
    sim.complete_lap(2, 90000)
    for step in range(1, 7):  # lap 2 up to 3000 m
        dist = float(step * 500)
        sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=dist, cur=int(dist * 18))))
    assert len(sim.live._trace) == 6
    rewind_to = sim.live._trace.stime[3]  # keep samples up to 2000 m

    sim.live.on_generation(1, rewind_to)
    assert len(sim.live._trace) == 4
    assert sim.live.generation == 1
    assert "FLASHBACK" in sim.codes()

    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=2100.0, cur=37800)))
    assert len(sim.live._trace) == 5
    assert sim.live.reference_lap_ms == 90000  # the reference itself is untouched


def test_flashback_rewind_is_not_a_lap_but_the_replay_still_counts() -> None:
    sim = Sim(session_type=5)
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, dist=4000.0, cur=70000)))
    sim.drain()

    # Rewound onto lap 2: the lap number going backwards must not look like a lap.
    sim.live.on_generation(1, sim.t - 10.0)
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=3000.0, cur=54000, last=91000)))
    assert [event["code"] for event in sim.drain() if event["code"] == "LAP"] == []

    # Re-driving to the line does complete a lap, exactly once.
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, dist=4500.0, cur=81000, last=91000)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, dist=100.0, cur=500, last=90500)))
    laps = [event for event in sim.drain() if event["code"] == "LAP"]
    assert len(laps) == 1
    assert laps[0]["data"]["lap_number"] == 2
    assert laps[0]["data"]["time_ms"] == 90500


# --------------------------------------------------------------------------
# sector colours
# --------------------------------------------------------------------------


def test_sector_colors_purple_green_yellow() -> None:
    sim = Sim(session_type=5)
    opponent = car(1, position=2, lap=1)

    # Player sets the first S1 anyone has seen -> purple.
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=0), opponent))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=1, s1=21000), opponent))
    events = [event for event in sim.drain() if event["code"] == "SECTOR"]
    assert events[-1]["data"] == {"sector": 1, "time_ms": 21000, "color": "purple"}

    # Opponent goes quicker: the session best moves, no player event.
    sim.feed(
        PacketId.LAP_DATA,
        lapview(car(lap=1, sector=1, s1=21000), car(1, position=2, lap=1, sector=1, s1=20500)),
    )
    assert [event for event in sim.drain() if event["code"] == "SECTOR"] == []

    # Player improves on their own best but not the session best -> green.
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, sector=0, last=91000)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, sector=1, s1=20800)))
    events = [event for event in sim.drain() if event["code"] == "SECTOR"]
    assert events[-1]["data"]["color"] == "green"

    # Slower than both -> yellow.
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, sector=0, last=91000)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, sector=1, s1=22000)))
    events = [event for event in sim.drain() if event["code"] == "SECTOR"]
    assert events[-1]["data"]["color"] == "yellow"

    # And back to purple when the session best falls.
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=4, sector=0, last=91000)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=4, sector=1, s1=20400)))
    events = [event for event in sim.drain() if event["code"] == "SECTOR"]
    assert events[-1]["data"]["color"] == "purple"


def test_invalid_lap_sectors_do_not_set_bests() -> None:
    sim = Sim(session_type=5)
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=0, invalid=True)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=1, s1=19000, invalid=True)))
    events = [event for event in sim.drain() if event["code"] == "SECTOR"]
    assert events[-1]["data"]["color"] == "yellow"
    assert sim.live.get_slow()["sectors"]["session_best"] == [None, None, None]


def test_lap_event_carries_sectors_and_color() -> None:
    sim = Sim(session_type=5)
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=0)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=1, s1=21000)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1, sector=2, s1=21000, s2=35000)))
    sim.drain()
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, last=91000)))
    events = {event["code"]: event for event in sim.drain()}
    assert events["SECTOR"]["data"] == {"sector": 3, "time_ms": 35000, "color": "purple"}
    assert events["LAP"]["data"] == {
        "lap_number": 1,
        "time_ms": 91000,
        "valid": True,
        "color": "purple",
        "sectors": [21000, 35000, 35000],
    }


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def test_lap_invalidation_and_pit_events() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=5)))
    sim.drain()
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=5, invalid=True)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=5, invalid=True, pit=1)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=5, invalid=True, pit=0)))
    events = sim.drain()
    assert [event["code"] for event in events] == ["LAP_INVALID", "PIT_IN", "PIT_OUT"]
    assert events[0]["data"] == {"lap_number": 5}


def test_flag_and_safety_car_transitions() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.CAR_STATUS, status(flags=0))
    sim.feed(PacketId.CAR_STATUS, status(flags=3))
    sim.feed(PacketId.SESSION, sessionview(15, safety_car=1))
    codes = [(event["code"], event["data"]) for event in sim.drain()]
    assert ("FLAG", {"flag": "clear"}) in codes
    assert ("FLAG", {"flag": "yellow"}) in codes
    assert ("SC", {"status": "deployed"}) in codes


def test_game_event_codes_are_mapped() -> None:
    sim = Sim(session_type=15)
    sim.feed(
        PacketId.PARTICIPANTS,
        ParticipantsView(
            num_active=1,
            cars=[
                ParticipantView(
                    car_index=4, name="VERSTAPPEN", team_id=2, race_number=1, is_ai=True
                )
            ],
        ),
    )
    sim.drain()
    sim.feed(PacketId.EVENT, EventView(code="DRSE"))
    sim.feed(PacketId.EVENT, EventView(code="DRSD"))
    sim.feed(PacketId.EVENT, EventView(code="CHQF"))
    sim.feed(
        PacketId.EVENT,
        EventView(code="FTLP", details={"vehicleIdx": 4, "lapTime": 92.345}),
    )
    sim.feed(
        PacketId.EVENT,
        EventView(
            code="PENA",
            details={
                "penaltyType": 2,
                "infringementType": 7,
                "time": 5,
                "otherVehicleIdx": 9,
            },
        ),
    )
    sim.feed(PacketId.EVENT, EventView(code="SEND"))
    sim.feed(PacketId.EVENT, EventView(code="RTMT"))  # not in the WS code set -> ignored
    events = sim.drain()
    codes = [event["code"] for event in events]
    assert codes == ["DRS", "DRS", "CHEQUERED", "FASTEST_LAP", "PENALTY", "SESSION_END"]
    assert events[0]["data"] == {"enabled": True}
    assert events[1]["data"] == {"enabled": False}
    assert events[3]["data"] == {"car_index": 4, "name": "VERSTAPPEN", "time_ms": 92345}
    assert events[4]["data"] == {
        "penalty_type": 2,
        "infringement_type": 7,
        "time_s": 5,
        "other_car": 9,
    }


def test_session_start_is_emitted_once() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.EVENT, EventView(code="SSTA"))
    assert sim.codes().count("SESSION_START") == 1


def test_flbk_packet_is_deduped_against_the_header_signature() -> None:
    sim = Sim(session_type=15)
    sim.live.on_generation(1, 120.0)
    sim.feed(PacketId.EVENT, EventView(code="FLBK", details={"sessionTime": 120.0}))
    assert sim.codes().count("FLASHBACK") == 1


def test_stall_transition_events() -> None:
    sim = Sim(session_type=15)
    sim.drain()
    sim.live.on_stall(True)
    sim.live.on_stall(True)
    sim.live.on_stall(False)
    events = sim.drain()
    assert [event["data"]["stalled"] for event in events] == [True, False]
    assert sim.live.get_slow()["session"]["stalled"] is False


def test_session_close_emits_session_end_once() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.EVENT, EventView(code="SEND"))
    sim.live.on_session_close(SessionKey(sim.uid, 0), "finished")
    assert sim.codes().count("SESSION_END") == 1


def test_recent_events_are_capped_and_ordered() -> None:
    sim = Sim(session_type=15)
    for _ in range(30):
        sim.feed(PacketId.EVENT, EventView(code="DRSE"))
    assert len(sim.live.recent_events) == 20
    assert sim.live.recent_events[-1]["code"] == "DRS"


# --------------------------------------------------------------------------
# derived metrics
# --------------------------------------------------------------------------


def test_wear_rate_and_projection() -> None:
    sim = Sim(session_type=15, total_laps=10)
    sim.feed(PacketId.CAR_DAMAGE, damage(wear=10.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, last=91000)))
    assert sim.live.wear_rate_pct_per_lap() is None  # one crossing is not a rate

    sim.feed(PacketId.CAR_DAMAGE, damage(wear=12.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, last=91000)))
    assert sim.live.wear_rate_pct_per_lap() == [2.0, 2.0, 2.0, 2.0]

    tyres = sim.live.get_slow()["tyres"]
    assert tyres["wear_pct"] == [12.0, 12.0, 12.0, 12.0]
    # 8 laps left (on lap 3 of 10) -> 12 + 2*8
    assert tyres["projected_wear_end_pct"] == [28.0, 28.0, 28.0, 28.0]


def test_projection_is_race_only() -> None:
    sim = Sim(session_type=5, total_laps=10)
    sim.feed(PacketId.CAR_DAMAGE, damage(wear=10.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, last=91000)))
    sim.feed(PacketId.CAR_DAMAGE, damage(wear=12.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, last=91000)))
    assert sim.live.get_slow()["tyres"]["projected_wear_end_pct"] is None


def test_fuel_burn_and_delta_laps() -> None:
    sim = Sim(session_type=15, total_laps=10)
    sim.feed(PacketId.CAR_STATUS, status(fuel=50.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=1)))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=2, last=91000)))
    sim.feed(PacketId.CAR_STATUS, status(fuel=48.0))
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3, last=91000)))

    fuel = sim.live.get_slow()["fuel"]
    assert fuel["burn_last_lap_kg"] == pytest.approx(2.0)
    assert fuel["in_tank_kg"] == 48.0
    assert fuel["laps_left_in_session"] == 8
    assert fuel["delta_laps"] == pytest.approx(22.4 - 8)


def test_laps_left_is_null_in_timed_sessions() -> None:
    sim = Sim(session_type=5, total_laps=0)
    sim.feed(PacketId.CAR_STATUS, status())
    sim.feed(PacketId.LAP_DATA, lapview(car(lap=3)))
    fuel = sim.live.get_slow()["fuel"]
    assert fuel["laps_left_in_session"] is None
    assert fuel["delta_laps"] is None


def test_rolling_pace_for_player_and_neighbours() -> None:
    sim = Sim(session_type=15)
    ahead, behind = car(1, position=1, lap=1), car(2, position=3, lap=1)
    sim.feed(PacketId.LAP_DATA, lapview(car(position=2, lap=1), ahead, behind))
    sim.feed(
        PacketId.LAP_DATA,
        lapview(
            car(position=2, lap=2, last=91000),
            car(1, position=1, lap=2, last=90000),
            car(2, position=3, lap=2, last=92000),
        ),
    )
    sim.feed(
        PacketId.LAP_DATA,
        lapview(
            car(position=2, lap=3, last=91500),
            car(1, position=1, lap=3, last=90500),
            car(2, position=3, lap=3, last=92500),
        ),
    )
    pace = sim.live.get_slow()["pace"]
    assert pace["last_3_avg_ms"] == 91250
    assert pace["ahead_last_3_avg_ms"] == 90250
    assert pace["behind_last_3_avg_ms"] == 92250


def test_leader_has_no_car_ahead() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.LAP_DATA, lapview(car(position=1, lap=2, last=91000), car(1, position=2)))
    pace = sim.live.get_slow()["pace"]
    assert pace["ahead_last_3_avg_ms"] is None
    assert sim.live.get_slow()["tower"][0]["gap_ahead_ms"] is None


def test_history_seeds_session_bests_for_opponents() -> None:
    sim = Sim(session_type=5)
    sim.feed(
        PacketId.SESSION_HISTORY,
        HistoryView(
            car_index=7,
            num_laps=2,
            best_lap_number=1,
            laps=[HistoryLap(1, 88000, 20000, 34000, 34000, True)],
        ),
    )
    assert sim.live.get_slow()["sectors"]["session_best"] == [20000, 34000, 34000]


def test_energy_percentage_uses_a_floating_capacity() -> None:
    sim = Sim(session_type=15)
    sim.feed(PacketId.CAR_TELEMETRY, telemetry())
    energy = sim.live.get_slow()["energy"]
    assert energy["store_j"] == 2_800_000.0
    assert energy["store_pct"] == pytest.approx(70.0)
    assert energy["deploy_mode"] == 2


# --------------------------------------------------------------------------
# payload shapes
# --------------------------------------------------------------------------


def populated(session_type: int = 15, total_laps: int = 53) -> Sim:
    sim = Sim(session_type=session_type, total_laps=total_laps)
    sim.feed(
        PacketId.PARTICIPANTS,
        ParticipantsView(
            num_active=2,
            cars=[
                ParticipantView(car_index=0, name="PLAYER", team_id=9, race_number=44, is_ai=False),
                ParticipantView(
                    car_index=4, name="VERSTAPPEN", team_id=2, race_number=1, is_ai=True
                ),
            ],
        ),
    )
    sim.feed(PacketId.CAR_TELEMETRY, telemetry())
    sim.feed(PacketId.CAR_STATUS, status())
    sim.feed(PacketId.CAR_DAMAGE, damage())
    sim.feed(PacketId.MOTION, MotionView(10.0, 20.0, 1.0, 0.5, 0.2, 0.1))
    sim.feed(PacketId.LAP_DATA, lapview(car(position=2, lap=1), car(4, position=1, lap=1)))
    sim.feed(
        PacketId.LAP_DATA,
        lapview(
            car(position=2, lap=2, last=92800, s1=21345),
            car(4, position=1, lap=2, last=92345),
        ),
    )
    return sim


def test_fast_payload_matches_the_protocol() -> None:
    sim = populated()
    check_shape(sim.live.get_fast(), FAST_SHAPE, "$.fast")


def test_fast_is_none_without_telemetry() -> None:
    sim = Sim(session_type=15)
    assert sim.live.get_fast() is None


def test_slow_payload_matches_the_protocol() -> None:
    sim = populated()
    sim.live.update_health(packets_per_sec=480, ws_clients=2)
    slow = sim.live.get_slow()
    check_shape(slow, SLOW_SHAPE, "$.slow")
    assert slow["session"]["session_uid"] == "1234"
    assert slow["session"]["session_kind"] == "race"
    assert slow["health"]["packets_per_sec"] == 480
    assert slow["health"]["ws_clients"] == 2
    assert [row["position"] for row in slow["tower"]] == [1, 2]
    assert slow["tower"][0]["name"] == "VERSTAPPEN"
    assert slow["tower"][1]["is_player"] is True
    assert slow["tower"][1]["compound_visual"] == 16
    assert slow["tower"][0]["compound_visual"] is None
    assert slow["timetrial"] is None


def test_slow_is_none_before_a_session_opens() -> None:
    assert LiveState(cfg()).get_slow() is None


def test_snapshot_payload_matches_the_protocol() -> None:
    sim = populated()
    snapshot = sim.live.get_snapshot()
    check_shape(snapshot, SNAPSHOT_SHAPE, "$.snapshot")
    assert snapshot["protocol_version"] == 1
    assert snapshot["session"] == snapshot["slow"]["session"]
    assert snapshot["recent_events"][0]["code"] == "SESSION_START"


def test_every_emitted_event_matches_the_protocol() -> None:
    sim = populated()
    sim.feed(PacketId.EVENT, EventView(code="CHQF"))
    sim.feed(PacketId.CAR_STATUS, status(flags=4))
    sim.live.on_stall(True)
    events = sim.drain()
    assert events
    for event in events:
        check_shape(event, EVENT_SHAPE, f"$.event[{event['code']}]")


def test_timetrial_payload_present_only_for_time_trial() -> None:
    sim = Sim(session_type=18)
    sim.feed(
        PacketId.TIME_TRIAL,
        TimeTrialView(
            player_session_best=TimeTrialSet(0, 9, 91500, 21000, 35300, 35200, True),
            personal_best=TimeTrialSet(0, 9, 91200, 20900, 35200, 35100, True),
            rival=TimeTrialSet(1, 2, 91550, 21000, 35300, 35250, True),
        ),
    )
    slow = sim.live.get_slow()
    check_shape(slow, SLOW_SHAPE, "$.slow")
    assert slow["timetrial"]["pb_ms"] == 91200
    assert slow["timetrial"]["rival_sectors"] == [21000, 35300, 35250]


def test_snapshot_is_shape_valid_when_nothing_has_arrived() -> None:
    live = LiveState(cfg())
    live.on_session_open(SessionKey(5, 0), True)
    snapshot = live.get_snapshot()
    check_shape(snapshot, SNAPSHOT_SHAPE, "$.snapshot")
    assert snapshot["fast"] is None
    assert snapshot["session"]["joined_in_progress"] is True
    assert snapshot["session"]["track_name"] == "unknown"


# --------------------------------------------------------------------------
# track names
# --------------------------------------------------------------------------


@pytest.fixture
def track_name_cache() -> Any:
    saved = (live_module._TRACK_NAME_FN, live_module._TRACK_NAME_LOADED)
    yield
    live_module._TRACK_NAME_FN, live_module._TRACK_NAME_LOADED = saved


def test_track_name_falls_back_without_the_parser(track_name_cache: Any) -> None:
    live_module._TRACK_NAME_LOADED = True
    live_module._TRACK_NAME_FN = None
    assert resolve_track_name(3) == "track_3"
    assert resolve_track_name(-1) == "unknown"


def test_track_name_prefers_the_parser_enum(track_name_cache: Any) -> None:
    live_module._TRACK_NAME_LOADED = True
    live_module._TRACK_NAME_FN = lambda track_id: "Suzuka"
    assert resolve_track_name(3) == "Suzuka"


def test_track_name_survives_a_broken_parser_enum(track_name_cache: Any) -> None:
    def boom(track_id: int) -> str:
        raise KeyError(track_id)

    live_module._TRACK_NAME_LOADED = True
    live_module._TRACK_NAME_FN = boom
    assert resolve_track_name(3) == "track_3"


def test_session_segment_reset_clears_live_state() -> None:
    sim = populated()
    assert sim.live.get_slow()["tower"]
    sim.live.on_segment(SessionKey(sim.uid, 1), 1)
    slow = sim.live.get_slow()
    assert slow["session"]["segment"] == 1
    assert slow["tower"] == []
    assert slow["sectors"]["session_best"] == [None, None, None]
    assert sim.live.reference_lap_ms is None

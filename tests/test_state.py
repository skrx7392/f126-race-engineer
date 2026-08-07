"""SessionTracker lifecycle + LapLedger reconciliation + RowBuilder output."""

from __future__ import annotations

from typing import Any

import pytest

from f126.config import Config
from f126.state import SessionCallbacks, SessionTracker, build_state
from f126.state.rows import NearestDecimator, RowBuilder
from f126.state.session import LapLedger, SessionKey, session_kind, session_type_name
from f126.types import (
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
    PacketId,
    ParsedPacket,
    ParticipantsView,
    ParticipantView,
    SessionView,
    StatusView,
    TelemetryView,
    WheelSet,
)

# --------------------------------------------------------------------------
# hand-built packets (no parser dependency)
# --------------------------------------------------------------------------


def cfg(**overrides: Any) -> Config:
    conf = Config()
    conf.telemetry_hz = 20
    conf.wear_hz = 1
    conf.stall_after_s = 5.0
    conf.session_timeout_min = 30.0
    conf.segment_rewind_s = 30.0
    for name, value in overrides.items():
        setattr(conf, name, value)
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
        delta_to_car_ahead_ms=0,
        delta_to_leader_ms=0,
    )


def lapview(player: CarLap, *others: CarLap) -> LapView:
    return LapView(player=player, cars=[player, *others])


def telemetry(speed: float = 250.0) -> TelemetryView:
    return TelemetryView(
        speed_kmh=speed,
        throttle=1.0,
        brake=0.0,
        steer=0.0,
        gear=7,
        rpm=11000,
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


def status(fuel: float = 40.0, compound: int = 18, age: int = 5, flags: int = 0) -> StatusView:
    return StatusView(
        fuel_in_tank_kg=fuel,
        fuel_capacity_kg=110.0,
        fuel_remaining_laps=22.4,
        tyre_compound_actual=compound,
        tyre_compound_visual=16,
        tyre_age_laps=age,
        fia_flags=flags,
        ers_store_j=None,
        ers_deploy_mode=None,
        ers_harvested_lap_j=120_000.0,
        ers_deployed_lap_j=350_000.0,
        drs_allowed=True,
    )


def damage(wear: float = 10.0) -> DamageView:
    return DamageView(
        tyre_wear_pct=wheelset(wear, wear, wear, wear),
        tyre_damage_pct=wheelset(0, 0, 0, 0),
        brake_damage_pct=wheelset(0, 0, 0, 0),
        front_left_wing_pct=0,
        front_right_wing_pct=0,
        rear_wing_pct=0,
        floor_pct=0,
        diffuser_pct=0,
        sidepod_pct=0,
        gearbox_pct=0,
        engine_pct=0,
    )


def sessionview(session_type: int = 15, total_laps: int = 5) -> SessionView:
    return SessionView(
        session_type=session_type,
        track_id=3,
        track_length_m=5000,
        session_time_left_s=1740,
        session_duration_s=3600,
        total_laps=total_laps,
        weather=1,
        track_temp_c=31,
        air_temp_c=24,
        safety_car_status=0,
        pit_speed_limit_kmh=80,
    )


class Sim:
    """Emits ParsedPackets with sane, monotonically advancing headers."""

    def __init__(self, uid: int = 1001, *, t: float = 0.0, player: int = 0) -> None:
        self.uid = uid
        self.t = t
        self.frame = 0
        self.overall = 0
        self.player = player
        self.mono = 1_000_000_000
        self.wall = 1_700_000_000_000_000_000

    def packet(
        self,
        packet_id: PacketId,
        view: Any,
        *,
        dt: float = 0.05,
        uid: int | None = None,
        frame: int | None = None,
        overall: int | None = None,
        t: float | None = None,
    ) -> ParsedPacket:
        self.t = self.t + dt if t is None else t
        self.frame = self.frame + 1 if frame is None else frame
        self.overall = self.overall + 1 if overall is None else overall
        self.mono += int(dt * 1e9)
        self.wall += int(dt * 1e9)
        header = PacketHeader(
            packet_format=2026,
            game_year=26,
            packet_version=1,
            packet_id=int(packet_id),
            session_uid=self.uid if uid is None else uid,
            session_time=self.t,
            frame_identifier=self.frame,
            overall_frame_identifier=self.overall,
            player_car_index=self.player,
            secondary_player_car_index=255,
        )
        return ParsedPacket(
            header=header, view=view, recv_monotonic_ns=self.mono, recv_wall_ns=self.wall
        )


class Recorder:
    """Collects every tracker callback in order."""

    def __init__(self) -> None:
        self.opens: list[tuple[SessionKey, bool]] = []
        self.closes: list[tuple[SessionKey, str]] = []
        self.segments: list[tuple[SessionKey, int]] = []
        self.generations: list[tuple[int, float]] = []
        self.rotates: list[tuple[int, int]] = []
        self.stalls: list[bool] = []
        self.laps: list[tuple[SessionKey, Any, bool]] = []

    def callbacks(self) -> SessionCallbacks:
        return SessionCallbacks(
            on_session_open=lambda key, jip: self.opens.append((key, jip)),
            on_session_close=lambda key, reason: self.closes.append((key, reason)),
            on_segment=lambda key, segment: self.segments.append((key, segment)),
            on_generation=lambda gen, t: self.generations.append((gen, t)),
            on_rotate=lambda uid, segment: self.rotates.append((uid, segment)),
            on_stall=lambda stalled: self.stalls.append(stalled),
            on_lap=lambda key, record, corrected: self.laps.append((key, record, corrected)),
        )


def tracker_with_recorder(**conf: Any) -> tuple[SessionTracker, Recorder]:
    recorder = Recorder()
    return SessionTracker(cfg(**conf), recorder.callbacks()), recorder


# --------------------------------------------------------------------------
# session identity
# --------------------------------------------------------------------------


def test_first_packet_opens_session_and_rotates() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=42)
    assert tracker.feed(sim.packet(PacketId.SESSION, sessionview())) is True
    assert tracker.key == SessionKey(42, 0)
    assert rec.opens == [(SessionKey(42, 0), False)]
    assert rec.rotates == [(42, 0)]
    assert tracker.generation == 0


def test_mid_session_join_sets_flag() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=7, t=600.0)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    assert rec.opens == [(SessionKey(7, 0), True)]
    assert tracker.joined_in_progress is True


def test_new_uid_without_classification_supersedes() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=1)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview()))
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), uid=2, t=0.0))
    assert rec.closes == [(SessionKey(1, 0), "superseded")]
    assert rec.opens[-1] == (SessionKey(2, 0), False)


def test_new_uid_after_final_classification_finishes() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=1)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview()))
    classification = ClassificationView(
        rows=[
            ClassificationRow(
                car_index=0,
                position=1,
                num_laps=5,
                grid_position=1,
                points=25,
                num_pit_stops=1,
                result_status=3,
                best_lap_ms=90000,
                total_race_time_s=450.0,
                penalties_s=0,
            )
        ]
    )
    tracker.feed(sim.packet(PacketId.FINAL_CLASSIFICATION, classification))
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), uid=2, t=0.0))
    assert rec.closes == [(SessionKey(1, 0), "finished")]


def test_same_uid_time_rewind_starts_new_segment() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=9, t=300.0)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), t=1.0))
    assert tracker.key == SessionKey(9, 1)
    assert rec.segments == [(SessionKey(9, 1), 1)]
    assert rec.rotates == [(9, 0), (9, 1)]
    assert rec.closes == []  # a restart is not a close
    assert tracker.generation == 0


def test_small_rewind_is_not_a_segment() -> None:
    tracker, _ = tracker_with_recorder(segment_rewind_s=30.0)
    sim = Sim(uid=9, t=300.0)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), t=290.0))
    assert tracker.key == SessionKey(9, 0)


# --------------------------------------------------------------------------
# flashback vs reorder
# --------------------------------------------------------------------------


def test_flashback_signature_bumps_generation() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=5, t=120.0)
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), dt=0.0))
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car())))
    # frame_identifier and session_time rewind, overall_frame_identifier climbs
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), t=105.0, frame=sim.frame - 900))
    assert tracker.generation == 1
    assert rec.generations == [(1, 105.0)]
    assert rec.segments == []  # a flashback is never a new segment
    assert tracker.key == SessionKey(5, 0)


def test_flashback_larger_than_segment_threshold_is_still_a_flashback() -> None:
    tracker, rec = tracker_with_recorder(segment_rewind_s=5.0)
    sim = Sim(uid=5, t=200.0)
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), dt=0.0))
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), t=150.0, frame=sim.frame - 3000))
    assert rec.generations == [(1, 150.0)]
    assert rec.segments == []


def test_plain_reorder_is_discarded_and_never_looks_like_a_flashback() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=5, t=120.0)
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), dt=0.0))
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car())))
    fresh_frame, fresh_overall = sim.frame, sim.overall
    accepted = tracker.feed(
        sim.packet(
            PacketId.LAP_DATA,
            lapview(car()),
            t=100.0,
            frame=fresh_frame - 500,
            overall=fresh_overall - 500,
        )
    )
    assert accepted is False
    assert tracker.discarded == 1
    assert tracker.generation == 0
    assert rec.generations == []


def test_reorder_guard_is_per_packet_id() -> None:
    tracker, _ = tracker_with_recorder()
    sim = Sim(uid=5)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), overall=99))
    tracker.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry(), overall=100))
    # A LapData packet from an older frame is fine: different packet_id stream.
    assert tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), overall=90)) is True
    # ...but an older CarTelemetry is stale.
    assert tracker.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry(), overall=95)) is False
    assert tracker.discarded == 1


def test_event_packets_are_never_discarded() -> None:
    tracker, _ = tracker_with_recorder()
    sim = Sim(uid=5)
    tracker.feed(sim.packet(PacketId.EVENT, EventView(code="SSTA"), overall=100))
    accepted = tracker.feed(sim.packet(PacketId.EVENT, EventView(code="DRSE"), overall=10))
    assert accepted is True
    assert tracker.discarded == 0
    assert tracker.generation == 0


def test_opening_packet_seeds_the_reorder_guard() -> None:
    tracker, _ = tracker_with_recorder()
    sim = Sim(uid=5)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), overall=500))
    assert tracker.feed(sim.packet(PacketId.SESSION, sessionview(), overall=400)) is False


def test_uid_change_resets_the_reorder_guard() -> None:
    """A game restart rewinds overall_frame_identifier; do not swallow the session."""
    tracker, _ = tracker_with_recorder()
    sim = Sim(uid=5)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), overall=50_000))
    assert tracker.feed(sim.packet(PacketId.SESSION, sessionview(), uid=6, overall=3)) is True
    assert tracker.key == SessionKey(6, 0)


# --------------------------------------------------------------------------
# silence
# --------------------------------------------------------------------------


def test_stall_then_timeout_then_resume_as_new_segment() -> None:
    tracker, rec = tracker_with_recorder(stall_after_s=5.0, session_timeout_min=0.5)
    sim = Sim(uid=77)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview()))
    base = sim.mono

    tracker.tick(base + 1_000_000_000)
    assert tracker.stalled is False
    tracker.tick(base + 6_000_000_000)
    assert tracker.stalled is True
    assert rec.stalls == [True]

    tracker.tick(base + 31_000_000_000)  # 0.5 min timeout
    assert rec.closes == [(SessionKey(77, 0), "timeout")]
    assert tracker.key is None

    sim.mono = base + 40_000_000_000
    tracker.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    assert tracker.key == SessionKey(77, 1)  # reopened as the next segment
    assert tracker.stalled is False
    assert rec.stalls == [True, False]
    assert rec.opens[-1][0] == SessionKey(77, 1)


def test_shutdown_closes_open_session() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=3)
    tracker.feed(sim.packet(PacketId.SESSION, sessionview()))
    tracker.shutdown()
    assert rec.closes == [(SessionKey(3, 0), "shutdown")]
    tracker.shutdown()
    assert len(rec.closes) == 1


# --------------------------------------------------------------------------
# lap ledger / history reconciliation
# --------------------------------------------------------------------------


def test_lap_ledger_emits_completed_lap_from_lapdata() -> None:
    emitted: list[tuple[Any, bool]] = []
    ledger = LapLedger(on_lap=lambda rec, corrected: emitted.append((rec, corrected)))
    ledger.apply_lap_view(lapview(car(lap=1, s1=21000, s2=35000, sector=2)))
    ledger.apply_lap_view(lapview(car(lap=2, last=91000, sector=0)))
    assert len(emitted) == 1
    record, corrected = emitted[0]
    assert corrected is False
    assert (record.lap_number, record.lap_time_ms) == (1, 91000)
    assert (record.s1_ms, record.s2_ms, record.s3_ms) == (21000, 35000, 35000)
    assert record.valid is True


def test_history_correction_reemits_lap() -> None:
    emitted: list[tuple[Any, bool]] = []
    ledger = LapLedger(on_lap=lambda rec, corrected: emitted.append((rec, corrected)))
    ledger.apply_lap_view(lapview(car(lap=1, s1=21000, s2=35000)))
    ledger.apply_lap_view(lapview(car(lap=2, last=91000)))
    ledger.apply_history(
        HistoryView(
            car_index=0,
            num_laps=2,
            best_lap_number=1,
            laps=[HistoryLap(1, 90950, 20990, 35000, 34960, True)],
        )
    )
    assert len(emitted) == 2
    corrected_record, corrected = emitted[1]
    assert corrected is True
    assert corrected_record.lap_time_ms == 90950
    assert corrected_record.s1_ms == 20990
    assert corrected_record.source == "history"


def test_history_is_authoritative_over_later_lapdata() -> None:
    emitted: list[tuple[Any, bool]] = []
    ledger = LapLedger(on_lap=lambda rec, corrected: emitted.append((rec, corrected)))
    ledger.apply_history(
        HistoryView(
            car_index=0,
            num_laps=2,
            best_lap_number=1,
            laps=[HistoryLap(1, 90950, 20990, 35000, 34960, True)],
        )
    )
    ledger.apply_lap_view(lapview(car(lap=1, s1=21000, s2=35000)))
    ledger.apply_lap_view(lapview(car(lap=2, last=91000)))
    assert len(emitted) == 1  # LapData never downgrades a history-confirmed lap
    assert ledger.record(0, 1).lap_time_ms == 90950


def test_history_agreement_does_not_reemit() -> None:
    emitted: list[tuple[Any, bool]] = []
    ledger = LapLedger(on_lap=lambda rec, corrected: emitted.append((rec, corrected)))
    ledger.apply_lap_view(lapview(car(lap=1, s1=21000, s2=35000)))
    ledger.apply_lap_view(lapview(car(lap=2, last=91000)))
    ledger.apply_history(
        HistoryView(
            car_index=0,
            num_laps=2,
            best_lap_number=1,
            laps=[HistoryLap(1, 91000, 21000, 35000, 35000, True)],
        )
    )
    assert len(emitted) == 1


def test_flashback_resync_does_not_fabricate_a_lap() -> None:
    emitted: list[Any] = []
    ledger = LapLedger(on_lap=lambda rec, corrected: emitted.append(rec))
    ledger.apply_lap_view(lapview(car(lap=3, s1=21000, s2=35000)))
    ledger.resync()
    ledger.apply_lap_view(lapview(car(lap=2, last=91000)))  # rewound onto lap 2
    ledger.apply_lap_view(lapview(car(lap=3, last=90500)))
    assert [record.lap_number for record in emitted] == [2]


def test_tracker_flashback_resyncs_the_ledger() -> None:
    tracker, rec = tracker_with_recorder()
    sim = Sim(uid=5, t=100.0)
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=3, s1=21000, s2=35000)), dt=0.0))
    tracker.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=3)), t=90.0, frame=sim.frame - 600))
    assert rec.generations == [(1, 90.0)]
    assert rec.laps == []


# --------------------------------------------------------------------------
# decimation
# --------------------------------------------------------------------------


def test_nearest_decimator_picks_the_closest_packet() -> None:
    decimator = NearestDecimator(0.05)
    kept = []
    for i in range(61):  # 1 s of 60 Hz
        t = i / 60.0
        chosen = decimator.offer(t, t)
        if chosen is not None:
            kept.append(round(chosen, 4))
    assert len(kept) == 21
    for index, value in enumerate(kept):
        assert abs(value - index * 0.05) <= 1 / 120.0


def test_decimator_regrids_after_a_rewind() -> None:
    decimator = NearestDecimator(0.05)
    assert decimator.offer(10.0, "a") == "a"
    assert decimator.offer(10.05, "b") == "b"
    assert decimator.offer(5.0, "c") == "c"  # flashback: immediate re-grid


def test_telemetry_rows_are_decimated_to_config_hz() -> None:
    sink = Sink()
    bundle = build_state(cfg(telemetry_hz=20), emit_row=sink.emit)
    rows = sink.rows
    sim = Sim(uid=11)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    for _ in range(60):  # 1 s of 60 Hz telemetry
        bundle.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry(), dt=1 / 60.0))
    samples = [row for table, row in rows if table == "telemetry_samples"]
    assert 19 <= len(samples) <= 21
    times = [row["session_time_s"] for row in samples]
    assert times == sorted(times)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(abs(gap - 0.05) < 0.02 for gap in gaps)
    assert samples[0]["tyre_surface_temp"] == [98, 97, 92, 94]
    assert samples[0]["drs_or_aero"] == 1  # aero_mode on the 2026 format


def test_wear_samples_are_decimated_to_1hz() -> None:
    rows: list[tuple[str, dict[str, Any]]] = []
    bundle = build_state(cfg(wear_hz=1), emit_row=lambda table, row: rows.append((table, row)))
    sim = Sim(uid=11)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    for i in range(50):  # 5 s of 10 Hz damage packets
        bundle.feed(sim.packet(PacketId.CAR_DAMAGE, damage(wear=i * 0.1), dt=0.1))
    samples = [row for table, row in rows if table == "wear_samples"]
    assert 5 <= len(samples) <= 6
    assert samples[0]["tyre_wear_pct"] == [0.0, 0.0, 0.0, 0.0]
    assert "engine_pct" in samples[0]["damage_json"]


# --------------------------------------------------------------------------
# row builder
# --------------------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def emit(self, table: str, row: dict[str, Any]) -> None:
        self.rows.append((table, row))

    def table(self, name: str) -> list[dict[str, Any]]:
        return [row for table, row in self.rows if table == name]


def test_session_row_fills_in_and_closes() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=1234567890123)
    bundle.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry(), dt=0.0))
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(session_type=15)))
    bundle.shutdown()

    sessions = sink.table("sessions")
    assert sessions[0]["session_uid"] == "1234567890123"
    assert sessions[0]["segment"] == 0
    assert sessions[0]["started_at_wall"] == pytest.approx(sim.wall / 1e9, abs=1.0)
    filled = sessions[-1]
    assert filled["session_type"] == 15
    assert filled["session_type_name"] == "Race"
    assert filled["track_id"] == 3
    assert filled["ended_reason"] == "shutdown"
    assert filled["ended_at_wall"] is not None
    assert filled["weather_json"]["track_temp_c"] == 31
    assert filled["joined_in_progress"] is False


def test_participants_rows_emitted_once_per_change() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=1)
    participants = ParticipantsView(
        num_active=2,
        cars=[
            ParticipantView(car_index=0, name="PLAYER", team_id=9, race_number=44, is_ai=False),
            ParticipantView(car_index=1, name="VERSTAPPEN", team_id=2, race_number=1, is_ai=True),
        ],
    )
    bundle.feed(sim.packet(PacketId.PARTICIPANTS, participants, dt=0.0))
    bundle.feed(sim.packet(PacketId.PARTICIPANTS, participants))
    rows = sink.table("participants")
    assert len(rows) == 2
    assert rows[0]["is_player"] is True
    assert rows[1]["is_ai"] is True
    assert rows[1]["name"] == "VERSTAPPEN"


def test_lap_row_carries_player_context_and_is_corrected_by_history() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=1)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.CAR_STATUS, status(fuel=50.0)))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=1, s1=21000, s2=35000))))
    bundle.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry(speed=331.0)))
    bundle.feed(sim.packet(PacketId.CAR_STATUS, status(fuel=48.0)))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=2, last=91000, pen=5))))

    laps = sink.table("laps")
    assert len(laps) == 1
    assert laps[0]["lap_number"] == 1
    assert laps[0]["lap_time_ms"] == 91000
    assert laps[0]["s3_ms"] == 35000
    assert laps[0]["generation"] == 0
    assert laps[0]["fuel_start_kg"] == 50.0
    assert laps[0]["fuel_end_kg"] == 48.0
    assert laps[0]["top_speed_kmh"] == 331.0
    assert laps[0]["ers_deployed_j"] == 350_000.0
    assert laps[0]["compound_actual"] == 18
    assert laps[0]["valid"] is True

    bundle.feed(
        sim.packet(
            PacketId.SESSION_HISTORY,
            HistoryView(
                car_index=0,
                num_laps=2,
                best_lap_number=1,
                laps=[HistoryLap(1, 90950, 20990, 35000, 34960, False)],
            ),
        )
    )
    laps = sink.table("laps")
    assert len(laps) == 2
    assert laps[1]["lap_number"] == 1
    assert laps[1]["lap_time_ms"] == 90950
    assert laps[1]["valid"] is False
    assert laps[1]["fuel_start_kg"] == 50.0  # context preserved across the correction


def test_tyre_stints_open_and_close_on_compound_change() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=1)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=1))))
    bundle.feed(sim.packet(PacketId.CAR_STATUS, status(compound=18, age=3)))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=8, pit=1, last=91000))))
    bundle.feed(sim.packet(PacketId.CAR_DAMAGE, damage(wear=22.0)))
    bundle.feed(sim.packet(PacketId.CAR_STATUS, status(compound=17, age=0)))
    bundle.shutdown()

    stints = sink.table("tyre_stints")
    assert [row["stint_no"] for row in stints] == [1, 1, 2, 2]
    closed = stints[1]
    assert closed["compound_actual"] == 18
    assert closed["lap_end"] == 8
    assert closed["end_reason"] == "pit_stop"
    assert closed["wear_at_end_json"]["tyre_wear_pct"] == [22.0, 22.0, 22.0, 22.0]
    assert stints[2]["compound_actual"] == 17
    assert stints[-1]["end_reason"] == "session_end"


def test_event_rows_include_synthetic_flashback() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=1, t=100.0)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.EVENT, EventView(code="SSTA")))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car()), t=80.0, frame=sim.frame - 1200))
    events = sink.table("events")
    codes = [row["code"] for row in events]
    assert codes == ["SSTA", "FLBK"]
    assert events[1]["details_json"]["synthetic"] is True
    assert events[1]["details_json"]["generation"] == 1
    assert events[0]["session_key_uid"] == "1"


def test_segment_change_closes_previous_session_row() -> None:
    sink = Sink()
    bundle = build_state(cfg(segment_rewind_s=10.0), emit_row=sink.emit)
    sim = Sim(uid=1, t=300.0)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), t=1.0))
    sessions = sink.table("sessions")
    closed = [row for row in sessions if row["ended_reason"] == "superseded"]
    assert closed and closed[0]["segment"] == 0
    assert sessions[-1]["segment"] == 1
    assert sessions[-1]["ended_reason"] is None


def test_raw_file_threads_into_session_row() -> None:
    sink = Sink()
    paths: list[str] = []

    def rotate(uid: int, segment: int) -> str:
        path = f"/data/raw/{uid}_{segment}.f1raw"
        paths.append(path)
        return path

    bundle = build_state(cfg(), emit_row=sink.emit, on_rotate=rotate)
    sim = Sim(uid=99)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    assert paths == ["/data/raw/99_0.f1raw"]
    assert sink.table("sessions")[-1]["raw_file"] == "/data/raw/99_0.f1raw"


def test_row_builder_ignores_packets_before_a_session_opens() -> None:
    sink = Sink()
    builder = RowBuilder(cfg(), sink.emit)
    sim = Sim(uid=1)
    builder.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry()))
    assert sink.rows == []


def test_motion_and_status_land_in_telemetry_rows() -> None:
    sink = Sink()
    bundle = build_state(cfg(telemetry_hz=1), emit_row=sink.emit)
    sim = Sim(uid=1)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.MOTION, MotionView(10.0, 20.0, 1.0, 0.5, 0.2, 0.1)))
    bundle.feed(sim.packet(PacketId.CAR_STATUS, status(fuel=33.0)))
    bundle.feed(sim.packet(PacketId.LAP_DATA, lapview(car(lap=4, dist=1234.5))))
    bundle.feed(sim.packet(PacketId.CAR_TELEMETRY, telemetry()))
    sample = sink.table("telemetry_samples")[0]
    assert sample["world_x"] == 10.0
    assert sample["world_z"] == 20.0
    assert sample["fuel_kg"] == 33.0
    assert sample["lap_number"] == 4
    assert sample["lap_distance_m"] == 1234.5
    assert sample["energy_store_j"] == 2_800_000.0


# --------------------------------------------------------------------------
# enum helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_type", "kind"),
    [
        (1, "practice"),
        (5, "quali"),
        (10, "quali"),
        (15, "race"),
        (18, "time_trial"),
        (99, "other"),
    ],
)
def test_session_kind_mapping(session_type: int, kind: str) -> None:
    assert session_kind(session_type) == kind


def test_session_type_name_falls_back() -> None:
    assert session_type_name(15) == "Race"
    assert session_type_name(99) == "session_99"


def test_stats_are_flat_and_metrics_ready() -> None:
    sink = Sink()
    bundle = build_state(cfg(), emit_row=sink.emit)
    sim = Sim(uid=42)
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), dt=0.0))
    bundle.feed(sim.packet(PacketId.SESSION, sessionview(), overall=0))  # stale, discarded

    stats = bundle.stats()
    assert stats["packets_total"] == 1
    assert stats["discarded_total"] == 1
    assert stats["sessions_opened_total"] == 1
    assert stats["session_uid"] == "42"
    assert stats["segment"] == 0
    assert stats["stalled"] == 0
    assert stats["rows_emitted_total"] >= 1
    assert stats["events_dropped_total"] == 0
    # /metrics only renders flat scalars.
    assert all(isinstance(value, int | float | str) for value in stats.values())


def test_uid_zero_menu_packets_are_ignored() -> None:
    """Menu/loading packets (sessionUID 0) must not open, close, or fragment
    sessions — the exact failure that produced phantom session rows in prod."""
    tracker, rec = tracker_with_recorder()
    menu = Sim(uid=0)
    assert tracker.feed(menu.packet(PacketId.SESSION, sessionview())) is False
    assert tracker.key is None
    assert rec.opens == []
    assert tracker.stats()["menu_ignored_total"] == 1

    # Real session opens normally after menu noise…
    real = Sim(uid=42)
    tracker.feed(real.packet(PacketId.SESSION, sessionview()))
    assert tracker.key == SessionKey(42, 0)

    # …menu noise mid-session neither closes nor segments it…
    tracker.feed(menu.packet(PacketId.SESSION, sessionview()))
    assert tracker.key == SessionKey(42, 0)
    assert rec.closes == []
    assert rec.segments == []

    # …and the same real session continuing afterwards is still segment 0.
    tracker.feed(real.packet(PacketId.LAP_DATA, None))
    assert tracker.key == SessionKey(42, 0)
    assert tracker.stats()["menu_ignored_total"] == 2

"""Tests for the post-session debrief: fact sheet, LLM client, endpoint, serve-path trigger.

The thing this file exists to protect is the *grounding contract*. A debrief is only worth
having if every number in it was computed in Python and handed to the model — so the tests
that matter most here are the ones asserting that the fact sheet is exact and deterministic,
and that the prompt tells the model in so many words that it may not do arithmetic.

Three tiers:

* **Tier 1** (always runs) drives `build_fact_sheet` against synthetic store rows through a
  monkeypatched `store.queries`, so the arithmetic is checked with no database anywhere. The
  LLM client is driven through `httpx.MockTransport`: the full request-building and
  response-parsing path, no socket.
* **Tier 2** (needs `F126_TEST_DATABASE_URL`) puts the schema, the JSONB round-trip and the
  newest-row-wins read against a real Postgres::

      F126_TEST_DATABASE_URL=postgresql://postgres:t@localhost:5555/postgres uv run pytest \\
          tests/test_debrief.py

* **No tier ever reaches the network.** There is no test here that would talk to a real
  model, by construction.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_analysis import THREE_CORNERS, TRACK_M, synthetic_lap, three_corner_speed

from f126.analysis.factsheet import FACT_SHEET_VERSION, build_fact_sheet
from f126.analysis.resample import AnalysisError
from f126.config import Config
from f126.llm import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Debrief,
    LlmClient,
    LlmError,
    build_messages,
    write_debrief,
)
from f126.store import queries as store_queries
from f126.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

# --------------------------------------------------------------------------------------------
# A synthetic session, shaped like the real Jeddah race that motivated this feature:
# 13 laps, softs 1-6, mediums 7-13, one flashback, one VSC, finished P3 from P5.
# --------------------------------------------------------------------------------------------

SESSION_ID = 102
PLAYER = 21
TRACK_ID = 25
SOFT_VISUAL = 16
MEDIUM_VISUAL = 17

#: Lap times in ms. Laps 1 (standing start), 6 (in-lap) and 7 (out-lap) are deliberately slow
#: so the consistency statistics have something to exclude, and lap 9 is invalidated.
LAP_TIMES = {
    1: 95_400,
    2: 91_800,
    3: 91_500,
    4: 91_900,
    5: 91_700,
    6: 104_200,
    7: 99_800,
    8: 92_400,
    9: 92_100,
    10: 92_300,
    11: 92_600,
    12: 92_900,
    13: 93_400,
}
INVALID_LAPS = frozenset({9})
BEST_LAP = 3


def _lap_rows() -> list[dict[str, Any]]:
    """13 `laps` rows for the player, plus one other car's lap to prove it is filtered out."""
    rows: list[dict[str, Any]] = []
    for lap, time_ms in LAP_TIMES.items():
        soft = lap <= 6
        # Fuel: 1.85 kg a lap off a 40 kg start, monotonically down. No refuels here; the
        # practice-programme case gets its own test.
        start = 40.0 - 1.85 * (lap - 1)
        rows.append(
            {
                "session_id": SESSION_ID,
                "car_index": PLAYER,
                "lap_number": lap,
                "generation": 0,
                "lap_time_ms": time_ms,
                "s1_ms": time_ms // 3,
                "s2_ms": time_ms // 3,
                "s3_ms": time_ms - 2 * (time_ms // 3),
                "valid": lap not in INVALID_LAPS,
                "compound_actual": 16 if soft else 18,
                "compound_visual": SOFT_VISUAL if soft else MEDIUM_VISUAL,
                "tyre_age_laps": (lap - 1) if soft else (lap - 7),
                "fuel_start_kg": round(start, 3),
                "fuel_end_kg": round(start - 1.85, 3),
                "top_speed_kmh": 320.0,
                "penalties_s": 0,
                "wall_ts": 1_770_000_000.0 + lap * 95.0,
            }
        )
    rows.append(
        {
            "session_id": SESSION_ID,
            "car_index": 3,
            "lap_number": 1,
            "generation": 0,
            "lap_time_ms": 90_100,
            "valid": True,
            "compound_visual": SOFT_VISUAL,
        }
    )
    return rows


def _stint_rows() -> list[dict[str, Any]]:
    return [
        {
            "car_index": PLAYER,
            "stint_no": 1,
            "compound_actual": 16,
            "compound_visual": SOFT_VISUAL,
            "lap_start": 1,
            "lap_end": 6,
            "wear_at_end_json": {"tyre_wear_pct": [31.0, 33.5, 20.1, 22.0]},
            "end_reason": "pit",
        },
        {
            "car_index": PLAYER,
            "stint_no": 2,
            "compound_actual": 18,
            "compound_visual": MEDIUM_VISUAL,
            "lap_start": 6,
            "lap_end": 13,
            "wear_at_end_json": {"tyre_wear_pct": [24.0, 25.5, 15.0, 16.5]},
            "end_reason": "session_end",
        },
    ]


def _event_rows() -> list[dict[str, Any]]:
    return [
        {"code": "SSTA", "session_time_s": 0.0, "details_json": {}},
        # A VSC: one deployment, three follow-up transitions that must NOT be counted.
        {
            "code": "SCAR",
            "session_time_s": 410.5,
            "details_json": {"safety_car_type": 2, "event_type": 0},
        },
        {
            "code": "SCAR",
            "session_time_s": 470.0,
            "details_json": {"safety_car_type": 2, "event_type": 1},
        },
        {
            "code": "SCAR",
            "session_time_s": 480.0,
            "details_json": {"safety_car_type": 2, "event_type": 2},
        },
        {"code": "FLBK", "session_time_s": 512.0, "details_json": {"flashback_frame_id": 9}},
        {
            "code": "PENA",
            "session_time_s": 620.0,
            "details_json": {
                "penalty_type": 4,
                "infringement_type": 12,
                "vehicle_idx": PLAYER,
                "time_s": 5,
                "lap_num": 8,
            },
        },
        # Another car's penalty: must not appear in the player's digest.
        {
            "code": "PENA",
            "session_time_s": 700.0,
            "details_json": {"penalty_type": 4, "vehicle_idx": 3, "time_s": 5, "lap_num": 9},
        },
    ]


def _session_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": SESSION_ID,
        "session_uid": "884422",
        "segment": 0,
        "packet_format": 2026,
        "session_type": 15,
        "session_type_name": "Race",
        "track_id": TRACK_ID,
        "track_name": "Jeddah",
        "started_at_wall": 1_770_000_000.0,
        "ended_at_wall": 1_770_001_260.0,
        "ended_reason": "finished",
        "player_car_index": PLAYER,
        "total_laps": 13,
        "weather_json": {
            "weather": 0,
            "track_temp_c": 33,
            "air_temp_c": 28,
            "forecast": [
                {"offset_min": 0, "weather": 0, "rain_percentage": 0},
                {"offset_min": 30, "weather": 1, "rain_percentage": 10},
            ],
        },
        "final_classification_json": {
            "rows": [
                {
                    "car_index": PLAYER,
                    "position": 3,
                    "num_laps": 13,
                    "grid_position": 5,
                    "points": 15,
                    "num_pit_stops": 1,
                    "result_status": 3,
                    "best_lap_ms": LAP_TIMES[BEST_LAP],
                    "total_race_time_s": 1245.678,
                    "penalties_s": 0,
                },
                {"car_index": 3, "position": 1, "points": 25, "result_status": 3},
            ]
        },
        "participants": [
            {"car_index": PLAYER, "name": "PLAYER", "is_player": True, "is_ai": False},
            {"car_index": 3, "name": "VERSTAPPEN", "is_player": False, "is_ai": True},
        ],
        "tyre_stints": _stint_rows(),
    }
    row.update(overrides)
    return row


def _trace(scale: float = 1.0, start_time: float = 0.0) -> list[dict[str, Any]]:
    """A three-corner lap; `scale` stretches the dips so a reference lap is genuinely faster."""
    dips = tuple((centre, depth * scale, width) for centre, depth, width in THREE_CORNERS)
    return synthetic_lap(
        lambda d: three_corner_speed(d, dips), TRACK_M, start_time=start_time
    )


class FakeStore:
    """Just enough of `store.queries` to build a fact sheet, with no database behind it.

    Installed over the real module with monkeypatch. Every method takes the `conn` the real
    one does and ignores it, which is what keeps the fact sheet's call shape honest: if
    `build_fact_sheet` starts calling a helper with different arguments, this breaks.
    """

    def __init__(self, session: dict[str, Any], **overrides: Any) -> None:
        self.session = session
        self.laps: list[dict[str, Any]] = overrides.get("laps", _lap_rows())
        self.events: list[dict[str, Any]] = overrides.get("events", _event_rows())
        self.stints: list[dict[str, Any]] = overrides.get("stints", _stint_rows())
        self.traces: dict[int, list[dict[str, Any]]] = overrides.get("traces", {})
        self.track_best: dict[str, Any] | None = overrides.get("track_best")
        self.calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "session_detail",
            "session_summary",
            "laps_for_session",
            "events_for_session",
            "tyre_stints_for_session",
            "telemetry_trace",
            "lap_row",
            "best_lap_with_telemetry",
            "track_best_lap_with_telemetry",
        ):
            monkeypatch.setattr(store_queries, name, getattr(self, name))

    # -- the helpers `build_fact_sheet` actually calls ---------------------------------

    def session_detail(self, conn: Any, session_id: int) -> dict[str, Any] | None:
        self.calls.append("session_detail")
        return self.session if session_id == self.session["id"] else None

    def session_summary(self, conn: Any, session_id: int) -> dict[str, Any] | None:
        if session_id == self.session["id"]:
            return self.session
        if self.track_best and session_id == self.track_best["session_id"]:
            return {
                "id": self.track_best["session_id"],
                "track_id": TRACK_ID,
                "track_name": "Jeddah",
                "session_type_name": "One-Shot Qualifying",
                "started_at_wall": 1_769_990_000.0,
                "player_car_index": PLAYER,
            }
        return None

    def laps_for_session(self, conn: Any, session_id: int, car_index: int | None = None) -> Any:
        rows = self.laps if car_index is None else [
            r for r in self.laps if r.get("car_index") == car_index
        ]
        return list(rows)

    def events_for_session(self, conn: Any, session_id: int, limit: int = 1000) -> Any:
        return list(self.events)

    def tyre_stints_for_session(
        self, conn: Any, session_id: int, car_index: int | None = None
    ) -> Any:
        return list(self.stints)

    def telemetry_trace(self, conn: Any, session_id: int, lap_number: int, **_: Any) -> Any:
        return list(self.traces.get(lap_number, []))

    def lap_row(self, conn: Any, session_id: int, car_index: int, lap_number: int) -> Any:
        for row in self.laps:
            if row.get("car_index") == car_index and row.get("lap_number") == lap_number:
                return row
        return None

    def best_lap_with_telemetry(
        self, conn: Any, session_id: int, car_index: int, *, exclude_lap: int | None = None
    ) -> Any:
        drawable = [
            row
            for row in self.laps
            if row.get("car_index") == car_index
            and row.get("valid")
            and row.get("lap_number") in self.traces
            and row.get("lap_number") != exclude_lap
        ]
        return min(drawable, key=lambda r: r["lap_time_ms"], default=None)

    def track_best_lap_with_telemetry(self, conn: Any, track_id: int, **_: Any) -> Any:
        return self.track_best


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore(_session_row())
    fake.install(monkeypatch)
    return fake


def sheet_of(store: FakeStore) -> dict[str, Any]:
    return build_fact_sheet(object(), SESSION_ID)


# --------------------------------------------------------------------------------------------
# Tier 1: the fact sheet
# --------------------------------------------------------------------------------------------


def test_the_fact_sheet_is_exact(store: FakeStore) -> None:
    """The golden dict. Every number here was computed by hand from the rows above.

    Asserted whole rather than field by field: the point of this sheet is that it is the
    *complete* set of facts a debrief may state, so a silently added or dropped key is
    exactly the regression worth catching.
    """
    sheet = sheet_of(store)

    assert sheet["fact_sheet_version"] == FACT_SHEET_VERSION
    assert sheet["session"] == {
        "session_id": 102,
        "type": "Race",
        "track": "Jeddah",
        "started_at_utc": "2026-02-02T02:40:00+00:00",
        "driver": "PLAYER",
        "is_race": True,
        "laps_recorded_all_cars": 14,
        "scheduled_laps": 13,
        "wall_duration_min": 21.0,
        "ended_reason": "finished",
    }
    assert sheet["result"] == {
        "finish_position": 3,
        "grid_position": 5,
        "points_scored": 15,
        "pit_stops": 1,
        "penalties_s": 0,
        "classified_cars": 2,
        "status": "Finished",
        "finished": True,
        "places_gained": 2,
        "race_time_s": 1245.678,
        "classified_best_lap": "1:31.500",
    }
    assert sheet["pace"] == {
        "laps_completed": 13,
        "laps_timed": 13,
        "laps_valid": 12,
        "laps_invalidated": 1,
        "best_lap_ms": 91_500,
        "best_lap": "1:31.500",
        "best_lap_number": 3,
        "best_lap_sectors_ms": [30_500, 30_500, 30_500],
        # Lap 6 is the pit lap (the stint engine attributes it to both stints, and stint 2's
        # out-lap label is the one that survives); lap 7 is 8.0% over the stint median, so
        # the engine's 107% rule drops it. Lap 9 was already gone for being invalidated.
        # Ten laps remain: 1,2,3,4,5,8,10,11,12,13.
        "consistency_laps_used": 10,
        "laps_excluded_from_consistency": [
            {"lap": 6, "reason": "out_lap"},
            {"lap": 7, "reason": "outlier"},
        ],
        # Sorted: 91500 91700 91800 91900 92300 | 92400 92600 92900 93400 95400.
        # median = (92300+92400)/2; q1 interpolates 2.25 -> 91800+0.25*100; q3 at 6.75.
        "median_lap_ms": 92_350.0,
        "median_lap": "1:32.350",
        "iqr_ms": 1000.0,
        "iqr_s": 1.0,
        "q1_lap_ms": 91_825.0,
        "q3_lap_ms": 92_825.0,
        "median_minus_best_ms": 850.0,
    }
    assert sheet["stints"] == [
        {
            "stint": 1,
            "lap_start": 1,
            "lap_end": 6,
            "compound": "Soft",
            "compound_actual": "C5",
            "laps_on_set": 6,
            "laps_used_for_fit": 5,
            "best_lap_on_set": "1:31.500",
            # Lap 1 is the standing start (95.4 s) and the stint engine keeps it, so the
            # least-squares line through laps 1-5 slopes *down*. That is not the tyres
            # gaining grip, and r2 = 0.489 is why `degradation_confidence` says so — the
            # one field standing between this number and a debrief stating it as a fact.
            "degradation_ms_per_lap": -730.0,
            "degradation_fit_r2": 0.489,
            "degradation_laps_fitted": 5,
            "degradation_confidence": "weak",
            "tyre_wear_at_end_pct": [31.0, 33.5, 20.1, 22.0],
        },
        {
            "stint": 2,
            "lap_start": 6,
            "lap_end": 13,
            "compound": "Medium",
            "compound_actual": "C3",
            "laps_on_set": 8,
            "laps_used_for_fit": 5,
            "best_lap_on_set": "1:32.300",
            "degradation_ms_per_lap": 197.3,
            "degradation_fit_r2": 0.731,
            "degradation_laps_fitted": 5,
            "degradation_confidence": "fair",
            "tyre_wear_at_end_pct": [24.0, 25.5, 15.0, 16.5],
        },
    ]
    # 13 laps at a flat 1.85 kg: 40.0 at the start of lap 1, 15.95 at the end of lap 13.
    assert sheet["fuel"] == {
        "burn_per_lap_kg": 1.85,
        "laps_measured": 13,
        "total_burn_kg": 24.05,
        "fuel_at_first_lap_kg": 40.0,
        "fuel_at_last_lap_kg": 15.95,
    }
    assert sheet["events"] == {
        "flashbacks": 1,
        "collisions_involving_player": 0,
        "safety_car_deployments": 1,
        "safety_cars": [{"type": "Virtual", "session_time_s": 410.5}],
        "virtual_safety_car_deployments": 1,
        "penalties": [
            {"type": "Time Penalty", "lap": 8, "seconds": 5, "infringement_code": 12},
        ],
        "pit_stops": 1,
    }
    assert sheet["weather"] == {
        "conditions": "Clear",
        "track_temp_c": 33.0,
        "air_temp_c": 28.0,
        "max_rain_probability_pct": 10,
        "forecast_conditions": ["Clear", "Light Cloud"],
    }
    # No telemetry was installed, so both corner comparisons are absent — named, not faked.
    assert sheet["omitted"] == {"corners": "lap 3 has no telemetry trace"}
    assert "corners" not in sheet


def test_the_sheet_is_deterministic(store: FakeStore) -> None:
    """Byte-identical across runs, including the corner sections.

    The whole design assumes the sheet is a pure function of the rows: a debrief that says
    something different on a second run of the same session would make every stored
    fact_sheet unfalsifiable.
    """
    store.traces = {BEST_LAP: _trace(), 5: _trace(scale=1.06, start_time=200.0)}
    first = json.dumps(build_fact_sheet(object(), SESSION_ID), sort_keys=True)
    second = json.dumps(build_fact_sheet(object(), SESSION_ID), sort_keys=True)
    assert first == second


def test_corners_name_the_worst_three_against_both_references(store: FakeStore) -> None:
    """Structure and sign conventions, not the segmentation itself.

    Which corners the algorithm finds is `tests/test_analysis.py`'s business. What this
    module owns is the reduction: at most three, worst first, only genuine losses, and every
    delta oriented the way the `units` block says it is.
    """
    store.traces = {
        BEST_LAP: _trace(scale=1.10),          # the subject: slowest through the corners
        5: _trace(scale=1.04, start_time=200.0),  # the session reference
        1: _trace(scale=1.00, start_time=400.0),  # the circuit benchmark, in another session
    }
    store.track_best = {"session_id": 91, "lap_number": 1, "lap_time_ms": 88_000}
    # The benchmark lap is read out of session 91, so its trace has to be reachable there.
    corners = build_fact_sheet(object(), SESSION_ID)["corners"]

    assert corners["subject_lap"] == BEST_LAP
    assert corners["subject_is_session_best"] is True
    for key in ("vs_session_best", "vs_track_best"):
        block = corners[key]
        assert 0 < len(block["worst_corners"]) <= 3
        losses = [corner["time_loss_ms"] for corner in block["worst_corners"]]
        assert losses == sorted(losses, reverse=True), "worst corner must come first"
        assert all(loss > 0 for loss in losses), "a corner that gained time is not a loss"
        for corner in block["worst_corners"]:
            assert corner["kind"] in {"slow", "medium", "fast"}
            if "min_speed_delta_kmh" in corner:
                expected = round(corner["min_speed_kmh"] - corner["ref_min_speed_kmh"], 1)
                assert corner["min_speed_delta_kmh"] == pytest.approx(expected, abs=0.05)
        # The subject is slower than both references, so the totals agree with the losses.
        assert block["total_delta_ms"] > 0
        assert block["total_delta_s"].startswith("+")

    assert corners["vs_track_best"]["reference_session_id"] == 91
    assert corners["vs_track_best"]["reference_label"] == "One-Shot Qualifying · Feb 01"
    # Segmentation finds braking zones, not the circuit's numbered turns — measured on the
    # real Jeddah race it found 8 across a 27-turn lap. Every comparison carries the warning
    # so the prose cannot quietly promote "corner 8" into "Turn 8".
    for key in ("vs_session_best", "vs_track_best"):
        note = corners[key]["corner_numbering"]
        assert "NOT the circuit's official" in note
        assert "apex_m" in note


def test_a_session_with_nothing_in_it_still_produces_a_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abandoned-session case: no laps, no classification, no weather, no crash.

    This is the shape of a session the driver quit out of after one installation lap, and it
    has to degrade into named gaps rather than into zeros a debrief would state as facts.
    """
    bare = _session_row(
        session_type=0,
        session_type_name=None,
        track_id=-1,
        track_name="track_-1",
        total_laps=None,
        ended_at_wall=None,
        weather_json=None,
        final_classification_json=None,
        participants=[],
        tyre_stints=[],
    )
    FakeStore(bare, laps=[], events=[], stints=[]).install(monkeypatch)

    sheet = build_fact_sheet(object(), SESSION_ID)
    assert sheet["session"] == {
        "session_id": 102,
        "type": "Unknown session",
        "started_at_utc": "2026-02-02T02:40:00+00:00",
        "is_race": False,
        "laps_recorded_all_cars": 0,
        "ended_reason": "finished",
    }
    assert "track" not in sheet["session"], "a -1 track id is not a circuit"
    assert sheet["pace"] == {
        "laps_completed": 0,
        "laps_timed": 0,
        "laps_valid": 0,
        "laps_invalidated": 0,
        "consistency_laps_used": 0,
    }
    assert "median_lap_ms" not in sheet["pace"], "no median from no laps"
    assert "result" not in sheet
    assert "weather" not in sheet
    assert "fuel" not in sheet
    assert sheet["omitted"] == {
        "corners": "no valid timed lap to analyse",
        "fuel": "no lap recorded a usable fuel start/end pair",
        "stints": "no tyre stints recorded or derivable",
        "weather": "no weather was recorded for this session",
    }


def test_a_race_that_never_finished_says_so_rather_than_inventing_a_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeStore(_session_row(final_classification_json=None)).install(monkeypatch)
    sheet = build_fact_sheet(object(), SESSION_ID)
    assert "result" not in sheet
    assert sheet["omitted"]["result"] == "the race produced no final classification packet"
    # ...and the pit-stop count falls back to the stint boundaries rather than vanishing.
    assert sheet["events"]["pit_stops"] == 1


def test_fuel_ignores_a_practice_programme_refuel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tank that refills mid-session is a garage event, not a lap of negative burn.

    Without this the median is computed across a step change and comes out plausible and
    wrong — the exact failure mode the fact sheet exists to prevent.
    """
    laps = []
    for lap, start in enumerate([40.0, 38.0, 36.0, 60.0, 58.0, 56.0, 54.0], start=1):
        laps.append(
            {
                "car_index": PLAYER,
                "lap_number": lap,
                "lap_time_ms": 92_000,
                "valid": True,
                "compound_visual": SOFT_VISUAL,
                "tyre_age_laps": lap - 1,
                "fuel_start_kg": start,
                "fuel_end_kg": start - 2.0,
            }
        )
    FakeStore(_session_row(), laps=laps, stints=[]).install(monkeypatch)

    fuel = build_fact_sheet(object(), SESSION_ID)["fuel"]
    assert fuel["burn_per_lap_kg"] == 2.0
    # Lap 4 is the top-up: dropped, named, and not folded into the six that were measured.
    assert fuel["laps_ignored_refuelled"] == [4]
    assert fuel["laps_measured"] == 6


def test_a_qualifying_session_never_reports_points_or_a_race_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found on the real one-shot quali at Jeddah: the sheet claimed 15 championship points.

    The game writes a final-classification packet for qualifying too, and it carries a stale
    `points` value, a `grid_position` of 0 and a `total_race_time_s` that is really just the
    lap time. Only the position on the timing sheet means anything outside a race.
    """
    quali = _session_row(
        session_type=9,
        session_type_name="One-Shot Qualifying",
        total_laps=1,
        final_classification_json={
            "rows": [
                {
                    "car_index": PLAYER,
                    "position": 8,
                    "grid_position": 0,
                    "points": 15,
                    "num_pit_stops": 0,
                    "result_status": 3,
                    "total_race_time_s": 90.649,
                    "best_lap_ms": 90_649,
                },
                {"car_index": 3, "position": 1},
            ]
        },
    )
    FakeStore(quali).install(monkeypatch)
    sheet = build_fact_sheet(object(), SESSION_ID)

    assert "result" not in sheet
    assert sheet["standing"] == {"position_in_session": 8, "classified_cars": 2}
    encoded = json.dumps(sheet)
    assert "points_scored" not in encoded
    assert "race_time_s" not in encoded
    assert "grid_position" not in encoded, "0 is 'no grid slot', not P0"
    assert sheet["session"]["is_race"] is False
    # Five stints in a practice programme are compound changes, not pit stops.
    assert "pit_stops" not in sheet["events"]


def test_a_warning_is_not_a_255_second_penalty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found on the real Jeddah race: `"seconds": 255` for a lap-1 warning.

    255 is the game's 0xFF "not applicable" byte, not a duration. A debrief told the driver
    took a 255-second penalty is not a small error — it is the largest number on the sheet.
    """
    events = [
        {
            "code": "PENA",
            "session_time_s": 30.0,
            "details_json": {
                "penalty_type": 5,  # Warning: carries no time and no lap
                "infringement_type": 4,
                "vehicle_idx": PLAYER,
                "time_s": 255,
                "lap_num": 1,
            },
        }
    ]
    FakeStore(_session_row(), events=events).install(monkeypatch)
    penalties = build_fact_sheet(object(), SESSION_ID)["events"]["penalties"]
    assert penalties == [{"type": "Warning", "lap": 1, "infringement_code": 4}]


def test_an_unknown_session_is_a_404_not_an_empty_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStore(_session_row()).install(monkeypatch)
    with pytest.raises(AnalysisError) as excinfo:
        build_fact_sheet(object(), 999_999)
    assert excinfo.value.status_code == 404


def test_every_number_in_the_sheet_is_json_safe(store: FakeStore) -> None:
    """No numpy scalars, no NaN, no infinities — the sheet goes into JSONB and into a prompt.

    `json.dumps` with `allow_nan=False` is the whole test: a NaN would serialise to the
    literal `NaN`, which is not JSON, and psycopg would store it or the model would read it.
    """
    store.traces = {BEST_LAP: _trace(), 5: _trace(scale=1.06, start_time=200.0)}
    encoded = json.dumps(build_fact_sheet(object(), SESSION_ID), allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded


# --------------------------------------------------------------------------------------------
# Tier 1: the LLM client
# --------------------------------------------------------------------------------------------


def llm_cfg(**overrides: Any) -> Config:
    cfg = Config()
    cfg.llm_base_url = "http://llm.test/api/v1"
    cfg.llm_model = "test-model"
    cfg.llm_api_key = "secret-key"
    cfg.llm_timeout_s = 5.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def ok_response(text: str = "You finished third.") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_the_prompt_forbids_the_model_from_doing_arithmetic() -> None:
    """The grounding contract, asserted on the actual system prompt.

    If these clauses are ever softened the model becomes free to compute, and a debrief that
    computes is a debrief that can be wrong in a way nothing downstream can detect.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "only numbers that appear in the fact sheet" in lowered
    assert "do not calculate" in lowered
    assert "200-300 words" in lowered
    assert "no flattery" in lowered
    assert "metric units" in lowered
    assert "one concrete, actionable focus item" in lowered


def test_the_request_carries_the_fact_sheet_and_nothing_else() -> None:
    """The whole request, inspected: URL, auth, model, and both turns."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_response())

    fact_sheet = {"session": {"track": "Jeddah"}, "pace": {"best_lap": "1:31.500"}}
    client = LlmClient("http://llm.test/api/v1/", "test-model", api_key="secret-key")
    text = asyncio.run(
        client.chat(build_messages(fact_sheet), transport=httpx.MockTransport(handler))
    )

    assert text == "You finished third."
    # The trailing slash on the base URL must not produce a double slash.
    assert seen["url"] == "http://llm.test/api/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is False

    system, user = seen["body"]["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    # The user turn is the fact sheet, verbatim and complete: the model is given the numbers
    # and no prose framing that could smuggle in a claim the sheet does not support.
    assert json.loads(user["content"]) == fact_sheet


def test_reasoning_effort_is_sent_only_when_configured() -> None:
    """A reasoning model burns max_tokens thinking and returns an empty message; the
    configured effort must reach the wire — and an unconfigured client must not send
    the key at all, because not every backend accepts it."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_response())

    client = LlmClient("http://llm.test/v1", "m", reasoning_effort="none")
    asyncio.run(client.chat(build_messages({}), transport=httpx.MockTransport(handler)))
    assert seen["body"]["reasoning_effort"] == "none"

    client = LlmClient("http://llm.test/v1", "m")
    asyncio.run(client.chat(build_messages({}), transport=httpx.MockTransport(handler)))
    assert "reasoning_effort" not in seen["body"]


def test_no_authorization_header_when_no_key_is_configured() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=ok_response())

    client = LlmClient("http://llm.test/api/v1", "test-model")
    asyncio.run(client.chat(build_messages({}), transport=httpx.MockTransport(handler)))
    assert seen["auth"] is None


def test_a_transient_failure_is_retried_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("f126.llm.RETRY_BACKOFF_S", 0.0)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=ok_response("Second time lucky."))

    client = LlmClient("http://x/v1", "m")
    text = asyncio.run(client.chat(build_messages({}), transport=httpx.MockTransport(handler)))
    assert text == "Second time lucky."
    assert attempts == 2


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 will fail identically the second time; retrying only buries the real cause."""
    monkeypatch.setattr("f126.llm.RETRY_BACKOFF_S", 0.0)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": {"message": "Missing API key"}})

    client = LlmClient("http://x/v1", "m")
    with pytest.raises(LlmError) as excinfo:
        asyncio.run(client.chat(build_messages({}), transport=httpx.MockTransport(handler)))
    assert attempts == 1
    assert "401" in str(excinfo.value)


def test_an_unusable_response_is_an_error_not_an_empty_debrief() -> None:
    for payload in ({}, {"choices": []}, {"choices": [{"message": {"content": "   "}}]}):
        client = LlmClient("http://x/v1", "m")
        transport = httpx.MockTransport(lambda _r, p=payload: httpx.Response(200, json=p))
        with pytest.raises(LlmError):
            asyncio.run(client.chat(build_messages({}), transport=transport))


def test_write_debrief_refuses_when_the_feature_is_not_configured() -> None:
    cfg = Config()
    assert cfg.llm_enabled is False
    with pytest.raises(LlmError):
        asyncio.run(write_debrief(cfg, {}))
    # Half-configured is still disabled: a base URL with no model would fail one request at
    # a time inside a fire-and-forget task, which is the worst place to discover it.
    assert llm_cfg(llm_model="").llm_enabled is False
    assert llm_cfg(llm_base_url="  ").llm_enabled is False
    assert llm_cfg().llm_enabled is True


def test_write_debrief_returns_the_provenance_to_store() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=ok_response("Prose.")))
    result = asyncio.run(write_debrief(llm_cfg(), {"a": 1}, transport=transport))
    assert result == Debrief(text="Prose.", model="test-model", prompt_version=PROMPT_VERSION)


# --------------------------------------------------------------------------------------------
# Tier 1: the HTTP endpoint
# --------------------------------------------------------------------------------------------


def web_cfg() -> Config:
    cfg = Config()
    cfg.static_dir = "/nonexistent/no-such-frontend"
    cfg.ws_fast_hz = 50.0
    cfg.ws_slow_hz = 0.0
    return cfg


class FakeLive:
    def get_snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot"}

    def get_fast(self) -> None:
        return None

    def get_slow(self) -> None:
        return None

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        return asyncio.Queue()


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


DEBRIEF_ROW = {
    "id": 7,
    "session_id": SESSION_ID,
    "created_at": 1_770_002_000.0,
    "model": "test-model",
    "prompt_version": PROMPT_VERSION,
    "fact_sheet": {"session": {"track": "Jeddah"}},
    "text": "You finished third from fifth.",
}


async def test_the_debrief_endpoint_serves_the_stored_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_queries, "latest_debrief", lambda conn, sid: DEBRIEF_ROW)
    app = create_app(web_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        response = await client.get(f"/api/sessions/{SESSION_ID}/debrief")
    assert response.status_code == 200
    assert response.json() == DEBRIEF_ROW


async def test_a_session_without_a_debrief_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence is the normal state of a freshly recorded session, and says so plainly."""
    monkeypatch.setattr(store_queries, "latest_debrief", lambda conn, sid: None)
    app = create_app(web_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        response = await client.get(f"/api/sessions/{SESSION_ID}/debrief")
    assert response.status_code == 404
    assert response.json() == {"detail": "no debrief for this session"}


async def test_the_debrief_endpoint_is_503_without_a_database() -> None:
    app = create_app(web_cfg(), FakeLive(), {}, None)
    async with client_for(app) as client:
        response = await client.get(f"/api/sessions/{SESSION_ID}/debrief")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


async def test_the_debrief_route_is_read_only() -> None:
    """The project invariant, restated for the route this feature added.

    Generation is deliberately CLI- and serve-path-only. A POST here would be convenient and
    would cost the one property that lets this dashboard sit on the open internet.
    """
    app = create_app(web_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await client.request(method, f"/api/sessions/{SESSION_ID}/debrief")
            assert response.status_code in (404, 405), method


# --------------------------------------------------------------------------------------------
# Tier 1: the serve-path trigger
# --------------------------------------------------------------------------------------------


class FakeKey:
    def __init__(self, uid: str = "884422") -> None:
        self.uid_str = uid


async def test_the_trigger_fires_only_for_a_session_that_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from f126 import app as app_module

    started: list[str] = []

    async def fake_task(cfg: Config, writer: Any, uid: str) -> None:
        started.append(uid)

    monkeypatch.setattr(app_module, "_debrief_task", fake_task)
    cfg = llm_cfg(database_url="postgresql://x/y")
    tasks: set[asyncio.Task[Any]] = set()
    on_close = app_module._debrief_on_close(cfg, None, tasks)

    for reason in ("timeout", "superseded", "shutdown"):
        on_close(FakeKey(), reason)
    await asyncio.sleep(0)
    assert started == [], "an unfinished session is a fragment, not a session to debrief"

    on_close(FakeKey(), "finished")
    await asyncio.gather(*tasks)
    assert started == ["884422"]


async def test_the_trigger_is_inert_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from f126 import app as app_module

    started: list[str] = []
    monkeypatch.setattr(
        app_module, "_debrief_task", lambda *a: started.append("x")  # noqa: ARG005
    )
    tasks: set[asyncio.Task[Any]] = set()

    # No LLM configured, and separately no database: either one is enough to skip.
    app_module._debrief_on_close(Config(), None, tasks)(FakeKey(), "finished")
    app_module._debrief_on_close(llm_cfg(database_url=""), None, tasks)(FakeKey(), "finished")
    await asyncio.sleep(0)
    assert started == [] and tasks == set()


async def test_a_failing_debrief_never_escapes_into_the_close_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure-tolerance requirement, exercised end to end through the real task.

    The close callback runs inside the packet pump. If an unreachable LLM could raise
    through it, an optional feature would be able to break session lifecycle handling — and
    it would do so at exactly the moment a race ends.
    """
    from f126 import app as app_module

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("postgres is on fire")

    monkeypatch.setattr(app_module.store_db, "connect", explode)
    cfg = llm_cfg(database_url="postgresql://x/y", debrief_delay_s=0.0)
    tasks: set[asyncio.Task[Any]] = set()

    app_module._debrief_on_close(cfg, None, tasks)(FakeKey(), "finished")  # must not raise
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(not isinstance(r, BaseException) for r in results), "the task swallows failures"
    assert "post-session debrief failed" in caplog.text
    assert tasks == set(), "finished tasks release their strong reference"


def test_a_close_observer_that_raises_does_not_break_the_lifecycle() -> None:
    """`build_state`'s new hook is an observer, and an observer may not veto the close."""
    from f126.state import build_state
    from f126.state.session import SessionKey

    cfg = Config()
    cfg.data_dir = "/nonexistent"
    seen: list[tuple[str, str]] = []

    def observer(key: SessionKey, reason: str) -> None:
        seen.append((key.uid_str, reason))
        raise RuntimeError("observer is broken")

    state = build_state(cfg, on_session_close=observer)
    # Reaching into the callbacks is the point: this asserts the fan-out wiring, not the
    # tracker's own logic for deciding when a session ends.
    state.tracker._callbacks.on_session_close(SessionKey(884422, 0), "finished")
    assert seen == [("884422", "finished")]


# --------------------------------------------------------------------------------------------
# Tier 2: a real Postgres (needs F126_TEST_DATABASE_URL)
# --------------------------------------------------------------------------------------------

TEST_DB_URL = os.environ.get("F126_TEST_DATABASE_URL", "")
integration = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="set F126_TEST_DATABASE_URL to run the debrief integration tier",
)


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A throwaway schema with the real DDL applied, dropped afterwards."""
    from f126.store import db

    schema = f"debrief_{uuid.uuid4().hex[:12]}"
    with db.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    url = f"{TEST_DB_URL}?options=-csearch_path%3D{schema}"
    try:
        with db.connect(url, autocommit=True) as conn:
            db.apply_schema(conn)
        yield url
    finally:
        with db.connect(TEST_DB_URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@integration
def test_the_schema_upgrade_creates_the_debriefs_table(scratch_db: str) -> None:
    from f126.store import db

    with db.connect(scratch_db, autocommit=True) as conn:
        assert db.schema_version(conn) == db.SCHEMA_VERSION >= 2
        # Idempotent: applying twice is what every boot does.
        db.apply_schema(conn)
        row = conn.execute(
            "SELECT count(*) AS n FROM information_schema.columns"
            " WHERE table_name = 'debriefs'"
        ).fetchone()
        assert row is not None and row["n"] == 7


@integration
def test_a_regenerated_debrief_supersedes_without_destroying(scratch_db: str) -> None:
    """Append-only, newest wins — and the fact sheet survives the JSONB round trip."""
    from f126.store import db
    from f126.store.writer import insert_debrief

    with db.connect(scratch_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sessions (session_uid, segment, started_at_wall, player_car_index)"
            " VALUES ('884422', 0, 1770000000.0, 21)"
        )
        session_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]  # type: ignore[index]

        sheet = {"pace": {"best_lap_ms": 91_500}, "units": {"_ms": "milliseconds"}}
        first = insert_debrief(
            conn,
            session_id,
            created_at=1_770_002_000.0,
            model="m1",
            prompt_version=1,
            fact_sheet=sheet,
            text="first take",
        )
        second = insert_debrief(
            conn,
            session_id,
            created_at=1_770_003_000.0,
            model="m2",
            prompt_version=2,
            fact_sheet=sheet,
            text="second take",
        )
        assert first is not None and second is not None and second != first

        latest = store_queries.latest_debrief(conn, session_id)
        assert latest is not None
        assert latest["text"] == "second take"
        assert latest["model"] == "m2"
        assert latest["prompt_version"] == 2
        # psycopg hands JSONB back as a dict, so this is the sheet the endpoint will serve.
        assert latest["fact_sheet"] == sheet

        kept = conn.execute("SELECT count(*) AS n FROM debriefs").fetchone()
        assert kept is not None and kept["n"] == 2, "regeneration appends, never overwrites"

        assert store_queries.latest_debrief(conn, session_id + 999) is None
        assert store_queries.player_lap_count(conn, session_id) == 0
        assert store_queries.session_id_for_key(conn, "884422") == session_id
        assert store_queries.session_id_for_key(conn, "nope") is None


@integration
def test_a_debrief_is_deleted_with_its_session(scratch_db: str) -> None:
    """`ON DELETE CASCADE`: Postgres is a derived index and must stay re-derivable."""
    from f126.store import db
    from f126.store.writer import insert_debrief

    with db.connect(scratch_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sessions (session_uid, segment, started_at_wall) VALUES ('9', 0, 1.0)"
        )
        session_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]  # type: ignore[index]
        insert_debrief(
            conn,
            session_id,
            created_at=1.0,
            model="m",
            prompt_version=1,
            fact_sheet={},
            text="t",
        )
        conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        left = conn.execute("SELECT count(*) AS n FROM debriefs").fetchone()
        assert left is not None and left["n"] == 0

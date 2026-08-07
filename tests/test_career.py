"""Tests for the Phase 3 career surface: derivation, routes, and the tag CLI.

Three tiers, none needing a database:

* the pure derivation (`f126.analysis.career`) against synthetic sessions whose seasons,
  rounds, totals and consistency numbers are computed by hand;
* the two career routes over `httpx.ASGITransport`, with `store.queries` monkeypatched the
  way `tests/test_debrief.py` fakes its store — the guards (503/422/404) and the read-only
  invariant are checked exactly as the ASGI server will exercise them;
* the `f126 tag` CLI against a fake connection, the way `tests/test_store.py` fakes the
  writer's — the SQL it emits and the return codes it answers with, no Postgres required.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from f126 import app as app_module
from f126.analysis.career import build_overview, build_track
from f126.config import Config
from f126.main import main as cli_main
from f126.parser.enums import session_type_name, track_name
from f126.store import queries as store_queries
from f126.store.writer import delete_career_tag, upsert_career_tag
from f126.web.app import create_app

PLAYER = 21
RIVAL = 4
T0 = 1_786_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR

CAREER_PATHS = ("/api/career/overview", "/api/career/tracks/3")


# --------------------------------------------------------------------------------------------
# Row builders
# --------------------------------------------------------------------------------------------


def session(
    session_id: int,
    *,
    session_type: int,
    track_id: int | None,
    started: float | None,
    classification: dict[str, Any] | None = None,
    player: int | None = PLAYER,
) -> dict[str, Any]:
    """One `queries.career_sessions` row."""
    return {
        "id": session_id,
        "session_uid": str(9000 + session_id),
        "session_type": session_type,
        "session_type_name": session_type_name(session_type),
        "track_id": track_id,
        "track_name": None if track_id is None or track_id < 0 else track_name(track_id),
        "total_laps": None,
        "player_car_index": player,
        "started_at_wall": started,
        "ended_at_wall": None if started is None else started + HOUR,
        "final_classification_json": classification,
    }


def result_row(
    car: int,
    position: int,
    *,
    points: int = 0,
    grid: int | None = None,
    best_lap_ms: int = 0,
    status: int = 3,
    pit_stops: int = 0,
) -> dict[str, Any]:
    return {
        "car_index": car,
        "position": position,
        "grid_position": grid if grid is not None else position,
        "points": points,
        "num_pit_stops": pit_stops,
        "best_lap_ms": best_lap_ms,
        "result_status": status,
    }


def classification(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"rows": list(rows)}


def lap(
    lap_number: int,
    time_ms: int,
    *,
    valid: bool = True,
    s1: int | None = None,
    s2: int | None = None,
    s3: int | None = None,
    compound: int = 16,
    top_speed: float | None = None,
    wall_ts: float | None = None,
) -> dict[str, Any]:
    return {
        "car_index": PLAYER,
        "lap_number": lap_number,
        "lap_time_ms": time_ms,
        "valid": valid,
        "s1_ms": s1,
        "s2_ms": s2,
        "s3_ms": s3,
        "compound_actual": 18,
        "compound_visual": compound,
        "tyre_age_laps": lap_number - 1,
        "top_speed_kmh": top_speed,
        "wall_ts": wall_ts,
    }


def career_fixture() -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Two seasons, three weekends, one time trial, one untracked fragment.

    Every derived number asserted below was computed by hand from these rows:

    * weekend A (Bahrain, standard): quali P2, race P3 from grid 5 for 15 points, no
      fastest lap (a rival's classified best is quicker);
    * weekend B (Miami, sprint): shootout P1 (NOT a pole), sprint win (+8), pole, GP win
      (+25) with the fastest lap held strictly;
    * weekend C (Bahrain again, > 48 h later -> season 2): pole and a win, fastest-lap
      *tie* -> not a fastest lap.
    """
    sessions = [
        # -- weekend A: Bahrain, standard ------------------------------------------------
        session(101, session_type=1, track_id=3, started=T0),
        session(
            102,
            session_type=9,
            track_id=3,
            started=T0 + 2 * HOUR,
            classification=classification(
                result_row(RIVAL, 1, best_lap_ms=87_500),
                result_row(PLAYER, 2, best_lap_ms=88_000),
            ),
        ),
        session(
            103,
            session_type=15,
            track_id=3,
            started=T0 + 4 * HOUR,
            classification=classification(
                result_row(RIVAL, 1, points=25, best_lap_ms=89_500),
                result_row(PLAYER, 3, grid=5, points=15, best_lap_ms=90_000),
            ),
        ),
        # -- a time trial at the same circuit: never a career round, always a PB source --
        session(104, session_type=18, track_id=3, started=T0 + 1 * DAY),
        # -- weekend B: Miami, sprint ----------------------------------------------------
        session(105, session_type=1, track_id=30, started=T0 + 7 * DAY),
        session(
            106,
            session_type=14,
            track_id=30,
            started=T0 + 7 * DAY + 1 * HOUR,
            classification=classification(
                result_row(PLAYER, 1, best_lap_ms=89_500),
                result_row(RIVAL, 2, best_lap_ms=89_700),
            ),
        ),
        session(
            107,
            session_type=16,
            track_id=30,
            started=T0 + 7 * DAY + 2 * HOUR,
            classification=classification(
                result_row(PLAYER, 1, grid=12, points=8, best_lap_ms=89_000),
                result_row(RIVAL, 2, points=7, best_lap_ms=89_200),
            ),
        ),
        session(
            108,
            session_type=9,
            track_id=30,
            started=T0 + 7 * DAY + 3 * HOUR,
            classification=classification(
                result_row(PLAYER, 1, best_lap_ms=87_274),
                result_row(RIVAL, 2, best_lap_ms=87_400),
            ),
        ),
        session(
            109,
            session_type=16,
            track_id=30,
            started=T0 + 7 * DAY + 4 * HOUR,
            classification=classification(
                result_row(PLAYER, 1, grid=1, points=25, best_lap_ms=88_643, pit_stops=1),
                result_row(RIVAL, 2, points=18, best_lap_ms=88_700),
            ),
        ),
        # -- weekend C: Bahrain again, 14 days on -> season 2 ----------------------------
        session(
            110,
            session_type=9,
            track_id=3,
            started=T0 + 14 * DAY,
            classification=classification(
                result_row(PLAYER, 1, best_lap_ms=87_800),
                result_row(RIVAL, 2, best_lap_ms=88_100),
            ),
        ),
        session(
            111,
            session_type=15,
            track_id=3,
            started=T0 + 14 * DAY + 2 * HOUR,
            classification=classification(
                result_row(PLAYER, 1, grid=1, points=25, best_lap_ms=90_200),
                # The exact same classified best: a tie is NOT the fastest lap.
                result_row(RIVAL, 2, points=18, best_lap_ms=90_200),
            ),
        ),
        # -- a fragment that never learned its circuit -----------------------------------
        session(112, session_type=15, track_id=-1, started=T0 + 20 * DAY),
    ]
    laps = {
        101: [lap(1, 92_000), lap(2, 91_500)],
        102: [lap(1, 88_000, s1=28_500, s2=33_500, s3=26_000)],
        # Lap 5 is 99 s against a 91 s median: past 107 %, excluded from consistency.
        103: [
            lap(1, 90_000, s1=29_500, s2=33_000, s3=27_500, top_speed=328.0),
            lap(2, 90_500, s1=29_000, s2=32_000, s3=29_500),
            lap(3, 91_000),
            lap(4, 91_500),
            lap(5, 99_000),
        ],
        104: [lap(1, 85_000, s1=28_000, s2=33_000, s3=24_000, top_speed=340.5)],
        105: [lap(1, 92_000)],
        106: [lap(1, 89_500)],
        107: [lap(1, 89_000), lap(2, 89_300), lap(3, 89_600)],
        # No lap at Miami ever recorded an s3, so its theoretical best must be null.
        108: [lap(1, 87_274, s1=27_800, s2=31_900, wall_ts=T0 + 7 * DAY + 3.2 * HOUR)],
        109: [
            lap(1, 88_643, s1=28_100, s2=32_000, top_speed=342.0),
            lap(2, 89_000),
            lap(3, 89_200),
            lap(4, 89_400),
            lap(5, 89_600),
        ],
        110: [lap(1, 87_800)],
        111: [lap(1, 90_200), lap(2, 90_400), lap(3, 90_600), lap(4, 90_800)],
    }
    return sessions, laps


def overview_of(
    sessions: list[dict[str, Any]],
    laps: dict[int, list[dict[str, Any]]],
    tags: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_overview(sessions, laps, {}, tags or [])


def weekends_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [weekend for season in payload["seasons"] for weekend in season["weekends"]]


# --------------------------------------------------------------------------------------------
# Derivation: weekends and seasons
# --------------------------------------------------------------------------------------------


def test_weekends_cluster_by_circuit_and_split_on_the_48h_gap() -> None:
    sessions, laps = career_fixture()
    payload = overview_of(sessions, laps)

    weekends = weekends_of(payload)
    assert [weekend["session_ids"] for weekend in weekends] == [
        [101, 102, 103],
        [105, 106, 107, 108, 109],
        [110, 111],
    ]
    # The time trial sits one day after weekend A at the same circuit — inside the gap,
    # excluded by type, and it must neither join the weekend nor split it.
    assert all(104 not in weekend["session_ids"] for weekend in weekends)
    assert weekends[0]["track_id"] == 3
    assert weekends[0]["started_at_wall"] == T0
    assert weekends[0]["ended_at_wall"] == T0 + 4 * HOUR + HOUR


def test_a_gap_under_48h_is_one_weekend_and_over_it_is_two() -> None:
    close = [
        session(1, session_type=1, track_id=3, started=T0),
        session(2, session_type=15, track_id=3, started=T0 + 47 * HOUR),
    ]
    apart = [
        session(1, session_type=1, track_id=3, started=T0),
        session(2, session_type=15, track_id=3, started=T0 + 3 * DAY),
    ]
    assert len(weekends_of(overview_of(close, {}))) == 1
    assert len(weekends_of(overview_of(apart, {}))) == 2


def test_seasons_roll_over_when_a_circuit_repeats() -> None:
    sessions, laps = career_fixture()
    payload = overview_of(sessions, laps)

    assert [season["season"] for season in payload["seasons"]] == [1, 2]
    assert [season["rounds"] for season in payload["seasons"]] == [2, 1]
    weekends = weekends_of(payload)
    assert [(w["season"], w["round"]) for w in weekends] == [(1, 1), (1, 2), (2, 1)]


def test_time_trials_and_trackless_fragments_never_make_weekends() -> None:
    sessions, laps = career_fixture()
    payload = overview_of(sessions, laps)

    assert payload["untracked_sessions"] == 1  # session 112, track_id -1
    assert all(
        session_id not in weekend["session_ids"]
        for weekend in weekends_of(payload)
        for session_id in (104, 112)
    )
    assert payload["notes"]["weekend_rule"]
    assert payload["notes"]["season_rule"]


def test_an_empty_database_is_empty_arrays_not_an_error() -> None:
    payload = build_overview([], {}, {}, [])
    assert payload["seasons"] == []
    assert payload["pbs"] == []
    assert payload["untracked_sessions"] == 0
    assert payload["career_totals"]["points"] == 0
    assert payload["career_totals"]["races"] == 0


# --------------------------------------------------------------------------------------------
# Derivation: tags
# --------------------------------------------------------------------------------------------


def _three_single_race_weekends() -> list[dict[str, Any]]:
    return [
        session(1, session_type=15, track_id=3, started=T0),
        session(2, session_type=15, track_id=30, started=T0 + 7 * DAY),
        session(3, session_type=15, track_id=7, started=T0 + 14 * DAY),
    ]


def test_a_tag_pins_its_weekend_and_later_weekends_derive_forward() -> None:
    tags = [{"session_uid": "9002", "season": 2, "round": 5, "note": "actual", "updated_at": 100.0}]
    weekends = weekends_of(overview_of(_three_single_race_weekends(), {}, tags))

    assert [(w["season"], w["round"]) for w in weekends] == [(1, 1), (2, 5), (2, 6)]
    assert weekends[0]["tags"] is None
    assert weekends[1]["tags"] == {"season": 2, "round": 5, "note": "actual"}
    assert weekends[1]["tag_conflict"] is False


def test_a_tag_with_no_round_pins_the_season_and_leaves_the_round_derived() -> None:
    tags = [{"session_uid": "9002", "season": 2, "round": None, "note": None, "updated_at": 1.0}]
    weekends = weekends_of(overview_of(_three_single_race_weekends(), {}, tags))
    assert [(w["season"], w["round"]) for w in weekends] == [(1, 1), (2, 1), (2, 2)]


def test_disagreeing_tags_newest_wins_and_the_weekend_is_flagged() -> None:
    sessions = [
        session(1, session_type=9, track_id=3, started=T0),
        session(2, session_type=15, track_id=3, started=T0 + 2 * HOUR),
    ]
    tags = [
        {"session_uid": "9001", "season": 2, "round": 5, "note": "old", "updated_at": 100.0},
        {"session_uid": "9002", "season": 3, "round": 1, "note": "new", "updated_at": 200.0},
    ]
    (weekend,) = weekends_of(overview_of(sessions, {}, tags))
    assert (weekend["season"], weekend["round"]) == (3, 1)
    assert weekend["tags"] == {"season": 3, "round": 1, "note": "new"}
    assert weekend["tag_conflict"] is True


def test_agreeing_tags_are_not_a_conflict() -> None:
    sessions = [
        session(1, session_type=9, track_id=3, started=T0),
        session(2, session_type=15, track_id=3, started=T0 + 2 * HOUR),
    ]
    tags = [
        {"session_uid": "9001", "season": 2, "round": 5, "note": "a", "updated_at": 100.0},
        {"session_uid": "9002", "season": 2, "round": 5, "note": "b", "updated_at": 200.0},
    ]
    (weekend,) = weekends_of(overview_of(sessions, {}, tags))
    assert weekend["tag_conflict"] is False
    assert weekend["tags"]["note"] == "b"  # newest updated_at still picks the reported tag


# --------------------------------------------------------------------------------------------
# Derivation: results, totals, consistency
# --------------------------------------------------------------------------------------------


def test_the_last_race_type_session_is_the_grand_prix_and_the_sprint_is_reported() -> None:
    sessions, laps = career_fixture()
    weekend = weekends_of(overview_of(sessions, laps))[1]  # Miami

    assert weekend["format"] == "sprint"
    assert weekend["race"]["session_id"] == 109
    assert weekend["sprint"]["session_id"] == 107
    assert weekend["sprint"] == {
        "session_id": 107,
        "position": 1,
        "grid_position": 12,
        "points": 8,
        "status": "finished",
    }
    assert weekend["race"] == {
        "session_id": 109,
        "position": 1,
        "grid_position": 1,
        "points": 25,
        "pit_stops": 1,
        "best_lap_ms": 88_643,
        "fastest_lap": True,
        "status": "finished",
    }
    assert weekend["points"] == 33
    # Quali is the last type-5..9 session — the one-shot at 108, never the shootout at 106.
    assert weekend["quali"] == {"session_id": 108, "position": 1, "best_lap_ms": 87_274}


def test_a_lone_race_type_session_is_the_race_not_a_sprint() -> None:
    sessions, laps = career_fixture()
    weekend = weekends_of(overview_of(sessions, laps))[0]  # Bahrain, standard
    assert weekend["format"] == "standard"
    assert weekend["sprint"] is None
    assert weekend["race"]["session_id"] == 103
    assert weekend["race"]["position"] == 3
    assert weekend["race"]["grid_position"] == 5
    assert weekend["race"]["fastest_lap"] is False  # the rival's 89.5 beats the player's 90.0
    assert weekend["points"] == 15


def test_totals_count_the_contract_rules_exactly() -> None:
    sessions, laps = career_fixture()
    payload = overview_of(sessions, laps)

    season_1, season_2 = payload["seasons"]
    assert season_1["totals"] == {
        "points": 48,  # 15 + 8 + 25: sprint points count, shootout points do not exist
        "wins": 1,  # the Miami GP; the sprint win is not a win
        "podiums": 2,  # Bahrain P3 + Miami P1
        "poles": 1,  # the one-shot quali at 108; the shootout P1 at 106 is NOT a pole
        "fastest_laps": 1,  # Miami GP, strict minimum
        "sprint_wins": 1,
        "races": 2,
    }
    assert season_2["totals"] == {
        "points": 25,
        "wins": 1,
        "podiums": 1,
        "poles": 1,
        "fastest_laps": 0,  # the classified bests tie, and a tie is not the fastest lap
        "sprint_wins": 0,
        "races": 1,
    }
    assert payload["career_totals"] == {
        "points": 73,
        "wins": 2,
        "podiums": 3,
        "poles": 2,
        "fastest_laps": 1,
        "sprint_wins": 1,
        "races": 3,
    }


def test_a_missing_classification_is_nulls_never_invented_values() -> None:
    sessions = [
        session(1, session_type=9, track_id=3, started=T0),
        session(2, session_type=15, track_id=3, started=T0 + 2 * HOUR),
    ]
    laps = {1: [lap(1, 88_000)], 2: [lap(1, 90_000), lap(2, 90_400)]}
    (weekend,) = weekends_of(overview_of(sessions, laps))

    assert weekend["quali"] == {"session_id": 1, "position": None, "best_lap_ms": 88_000}
    assert weekend["race"] == {
        "session_id": 2,
        "position": None,
        "grid_position": None,
        "points": None,
        "pit_stops": None,
        "best_lap_ms": 90_000,  # measured in the lap table, not inferred
        "fastest_lap": None,
        "status": None,
    }
    assert weekend["points"] is None

    payload = overview_of(sessions, laps)
    assert payload["career_totals"]["races"] == 1  # the race happened...
    assert payload["career_totals"]["wins"] == 0  # ...but nothing was invented about it
    assert payload["career_totals"]["points"] == 0


def test_consistency_matches_the_hand_computed_representative_laps() -> None:
    """Weekend A's race: laps 90.0/90.5/91.0/91.5/99.0 s — the last is past 107 % of the
    91 s median and drops out. Over the four survivors: median 90750, IQR 750, and a
    sample stdev of 645.497 on a 90750 mean = cv 0.71 %."""
    sessions, laps = career_fixture()
    weekend = weekends_of(overview_of(sessions, laps))[0]

    assert weekend["consistency"] == {
        "session_id": 103,
        "laps_used": 4,
        "median_ms": 90_750,
        "iqr_ms": 750,
        "cv_pct": 0.71,
    }


def test_consistency_below_four_laps_is_null_spread_not_a_zero() -> None:
    sessions = [session(1, session_type=15, track_id=3, started=T0)]
    laps = {1: [lap(1, 90_000), lap(2, 90_400)]}
    (weekend,) = weekends_of(overview_of(sessions, laps))
    assert weekend["consistency"] == {
        "session_id": 1,
        "laps_used": 2,
        "median_ms": None,
        "iqr_ms": None,
        "cv_pct": None,
    }


# --------------------------------------------------------------------------------------------
# Derivation: personal bests
# --------------------------------------------------------------------------------------------


def test_pbs_cover_every_circuit_in_first_visit_order_time_trial_included() -> None:
    sessions, laps = career_fixture()
    pbs = overview_of(sessions, laps)["pbs"]

    assert [entry["track_id"] for entry in pbs] == [3, 30]
    bahrain, miami = pbs

    # The circuit best is the time trial's 85.0 s — a TT is not a career round but its
    # laps are personal bests like any other.
    assert bahrain["best_lap_ms"] == 85_000
    assert bahrain["session_id"] == 104
    assert bahrain["lap_number"] == 1
    assert "Time Trial" in bahrain["session_label"]
    assert bahrain["compound_visual"] == 16
    assert bahrain["compound_name"] == "Soft"
    # Best valid sectors across the circuit: TT s1 28.0 + race-lap-2 s2 32.0 + TT s3 24.0.
    assert bahrain["theoretical_ms"] == 84_000
    assert bahrain["top_speed_kmh"] == 340.5

    assert miami["best_lap_ms"] == 87_274
    assert miami["session_id"] == 108
    assert miami["set_at_wall"] == T0 + 7 * DAY + 3.2 * HOUR
    assert miami["theoretical_ms"] is None  # no lap at Miami ever recorded an s3
    assert miami["top_speed_kmh"] == 342.0


# --------------------------------------------------------------------------------------------
# Derivation: the track page
# --------------------------------------------------------------------------------------------


def test_track_page_visits_carry_the_career_numbers_and_the_pb_names_its_sectors() -> None:
    sessions, laps = career_fixture()
    payload = build_track(3, sessions, laps, {}, [])

    assert payload["track_id"] == 3
    assert payload["track_name"] == "Bahrain"

    assert [(visit["season"], visit["round"]) for visit in payload["visits"]] == [(1, 1), (2, 1)]
    first, second = payload["visits"]
    assert first["session_ids"] == [101, 102, 103]
    assert first["best_lap_ms"] == 88_000
    assert first["best_lap_session_id"] == 102
    assert first["race"]["position"] == 3
    assert first["consistency"]["session_id"] == 103
    assert second["race"]["fastest_lap"] is False  # the tie again, through the track page

    sectors = payload["pb"]["sectors"]
    assert sectors == {
        "s1_ms": 28_000,
        "s1_session_id": 104,
        "s1_lap_number": 1,
        "s2_ms": 32_000,
        "s2_session_id": 103,
        "s2_lap_number": 2,
        "s3_ms": 24_000,
        "s3_session_id": 104,
        "s3_lap_number": 1,
    }

    # Every session at the circuit, time trial included, chronological, with its own stats.
    assert [row["id"] for row in payload["sessions"]] == [101, 102, 103, 104, 110, 111]
    time_trial = payload["sessions"][3]
    assert time_trial["session_type"] == 18
    assert time_trial["laps_total"] == 1
    assert time_trial["laps_used"] == 1
    assert time_trial["median_ms"] is None  # one lap has no spread
    assert time_trial["top_speed_kmh"] == 340.5
    race = payload["sessions"][2]
    assert (race["laps_used"], race["median_ms"], race["iqr_ms"], race["cv_pct"]) == (
        4,
        90_750,
        750,
        0.71,
    )


# --------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------


class FakeLive:
    """The narrowest `LiveSource` that lets an app be built without a game attached."""

    def get_snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot"}

    def get_fast(self) -> dict[str, Any] | None:
        return None

    def get_slow(self) -> dict[str, Any] | None:
        return None


def make_cfg(**overrides: Any) -> Config:
    fields: dict[str, Any] = {"static_dir": "/nonexistent/f126-static", "database_url": ""}
    fields.update(overrides)
    return Config(**fields)


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class FakeCareerStore:
    """Just enough of `store.queries` for the career routes, with no database behind it.

    Every method takes the `conn` the real one does and ignores it, which keeps the
    routes' call shapes honest — a changed helper signature breaks here first.
    """

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        laps: dict[int, list[dict[str, Any]]],
        tags: list[dict[str, Any]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.laps = laps
        self.tags = tags or []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "career_sessions",
            "career_tags",
            "player_laps_for_sessions",
            "player_stints_for_sessions",
        ):
            monkeypatch.setattr(store_queries, name, getattr(self, name))

    def career_sessions(self, conn: Any, *, limit: int = 2_000) -> list[dict[str, Any]]:
        return list(self.sessions)

    def career_tags(self, conn: Any) -> list[dict[str, Any]]:
        return list(self.tags)

    def player_laps_for_sessions(self, conn: Any, session_ids: Any) -> list[dict[str, Any]]:
        return [
            {**row, "session_id": session_id}
            for session_id in session_ids
            for row in self.laps.get(session_id, [])
        ]

    def player_stints_for_sessions(self, conn: Any, session_ids: Any) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def career_api(monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    sessions, laps = career_fixture()
    FakeCareerStore(sessions, laps).install(monkeypatch)
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    return client_for(app)


async def test_career_endpoints_degrade_to_503_without_a_database() -> None:
    app = create_app(make_cfg(), FakeLive())
    async with client_for(app) as client:
        for path in CAREER_PATHS:
            response = await client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "database unavailable"}


async def test_career_track_id_is_validated_before_a_query_can_run() -> None:
    app = create_app(make_cfg(), FakeLive(), {}, lambda: object())
    async with client_for(app) as client:
        # -1 is the recorder's "the Session packet never landed" sentinel, not a circuit.
        for bad in ("-1", "abc", "1001"):
            response = await client.get(f"/api/career/tracks/{bad}")
            assert response.status_code == 422, bad


async def test_career_track_404s_when_the_circuit_has_no_sessions(career_api: Any) -> None:
    async with career_api as client:
        response = await client.get("/api/career/tracks/7")
    assert response.status_code == 404
    assert "track" in response.json()["detail"]


async def test_career_overview_serves_the_derived_payload(career_api: Any) -> None:
    async with career_api as client:
        response = await client.get("/api/career/overview")
    assert response.status_code == 200
    assert "NaN" not in response.text

    payload = response.json()
    assert [season["season"] for season in payload["seasons"]] == [1, 2]
    assert payload["career_totals"]["points"] == 73
    assert payload["untracked_sessions"] == 1
    assert [entry["track_id"] for entry in payload["pbs"]] == [3, 30]


async def test_career_track_serves_the_derived_payload(career_api: Any) -> None:
    async with career_api as client:
        response = await client.get("/api/career/tracks/3")
    assert response.status_code == 200
    payload = response.json()
    assert payload["track_name"] == "Bahrain"
    assert len(payload["visits"]) == 2
    assert payload["pb"]["sectors"]["s2_session_id"] == 103


def _all_routes(app: Any) -> list[Any]:
    """Every leaf route, following the wrappers `include_router` leaves in `app.routes`."""
    found: list[Any] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
            continue
        found.append(route)
        mounted = getattr(route, "routes", None)
        if isinstance(mounted, list):
            stack.extend(mounted)
    return found


def test_the_career_routes_are_read_only() -> None:
    """Same invariant `tests/test_web.py` enforces, restated for the routes added here."""
    app = create_app(make_cfg(), FakeLive())
    career = [
        route for route in _all_routes(app) if getattr(route, "path", "").startswith("/api/career")
    ]
    assert career, "the career routes were never registered"
    for route in career:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}, route.path


def test_the_career_router_is_wired_into_the_app() -> None:
    app = create_app(make_cfg(), FakeLive())
    paths = {getattr(route, "path", "") for route in _all_routes(app)}
    assert "/api/career/overview" in paths
    assert "/api/career/tracks/{track_id}" in paths
    assert "/api/sessions" in paths, "the earlier routes must survive the Phase 3 include"


# --------------------------------------------------------------------------------------------
# The tag CLI
# --------------------------------------------------------------------------------------------


class FakeTagCursor:
    def __init__(self, conn: FakeTagConn) -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> FakeTagCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))
        self.rowcount = self.conn.delete_rowcount if sql.lstrip().startswith("DELETE") else 1


class FakeTagConn:
    """Just enough psycopg surface for the tag writers: cursor/commit/context manager."""

    autocommit = False

    def __init__(self, delete_rowcount: int = 1) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.delete_rowcount = delete_rowcount

    def cursor(self, **_kwargs: Any) -> FakeTagCursor:
        return FakeTagCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> FakeTagConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_upsert_career_tag_is_a_whole_row_replacement() -> None:
    conn = FakeTagConn()
    upsert_career_tag(conn, "42", season=2, round_no=5, note="sprint fix", updated_at=123.0)

    sql, params = conn.executed[0]
    assert "ON CONFLICT (session_uid) DO UPDATE" in sql
    assert "season = EXCLUDED.season" in sql
    assert "round = EXCLUDED.round" in sql
    # Replacement, not fill-the-blanks: re-tagging with no --round must null the old pin.
    assert "COALESCE" not in sql
    assert params == ("42", 2, 5, "sprint fix", 123.0)
    assert conn.commits == 1


def test_delete_career_tag_reports_whether_a_row_existed() -> None:
    conn = FakeTagConn(delete_rowcount=1)
    assert delete_career_tag(conn, "42") is True
    assert conn.commits == 1

    conn = FakeTagConn(delete_rowcount=0)
    assert delete_career_tag(conn, "42") is False


def tag_cfg(**overrides: Any) -> Config:
    fields: dict[str, Any] = {
        "database_url": "postgresql://fake/f126",
        "static_dir": "/nonexistent/f126-static",
    }
    fields.update(overrides)
    return Config(**fields)


def test_run_tag_resolves_the_uid_and_upserts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = FakeTagConn()
    monkeypatch.setattr(app_module.store_db, "connect", lambda url, **_: conn)
    monkeypatch.setattr(
        store_queries,
        "session_summary",
        lambda _conn, session_id: (
            {"id": session_id, "session_uid": "777"} if session_id == 7 else None
        ),
    )

    assert app_module.run_tag(tag_cfg(), 7, season=2, round_no=3, note="sprint fix") == 0
    sql, params = conn.executed[0]
    assert "career_tags" in sql
    assert params[:4] == ("777", 2, 3, "sprint fix")
    assert isinstance(params[4], float)  # updated_at is a wall clock, not a placeholder
    assert conn.commits == 1
    assert "777" in capsys.readouterr().out


def test_run_tag_clear_deletes_and_says_what_happened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        store_queries,
        "session_summary",
        lambda _conn, session_id: {"id": session_id, "session_uid": "777"},
    )

    conn = FakeTagConn(delete_rowcount=1)
    monkeypatch.setattr(app_module.store_db, "connect", lambda url, **_: conn)
    assert app_module.run_tag(tag_cfg(), 7, clear=True) == 0
    assert conn.executed[0][0].lstrip().startswith("DELETE FROM career_tags")
    assert "cleared" in capsys.readouterr().out

    conn = FakeTagConn(delete_rowcount=0)
    monkeypatch.setattr(app_module.store_db, "connect", lambda url, **_: conn)
    assert app_module.run_tag(tag_cfg(), 7, clear=True) == 0
    assert "no tag" in capsys.readouterr().out


def test_run_tag_list_prints_every_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app_module.store_db, "connect", lambda url, **_: FakeTagConn())
    monkeypatch.setattr(
        store_queries,
        "career_tags",
        lambda _conn: [
            {"session_uid": "9", "season": 2, "round": None, "note": "moved", "updated_at": 1.0}
        ],
    )
    monkeypatch.setattr(store_queries, "session_id_for_key", lambda _conn, _uid: 12)

    assert app_module.run_tag(tag_cfg(), None, list_tags=True) == 0
    out = capsys.readouterr().out
    assert "session 12" in out
    assert "season 2" in out
    assert "round -" in out
    assert "moved" in out


def test_run_tag_refuses_bad_invocations_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(url: str, **_: Any) -> Any:
        raise AssertionError("a usage error must never open a connection")

    monkeypatch.setattr(app_module.store_db, "connect", explode)

    assert app_module.run_tag(tag_cfg(database_url=""), 7, season=1) == 1
    assert app_module.run_tag(tag_cfg(), None, season=1) == 2  # no session id
    assert app_module.run_tag(tag_cfg(), 7) == 2  # no season and no --clear
    assert app_module.run_tag(tag_cfg(), 7, clear=True, note="x") == 2  # clear takes nothing
    assert app_module.run_tag(tag_cfg(), 7, season=0) == 2  # seasons are 1-based
    assert app_module.run_tag(tag_cfg(), 7, season=1, round_no=0) == 2
    assert app_module.run_tag(tag_cfg(), 7, season=1, list_tags=True) == 2


def test_run_tag_errors_on_an_unknown_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.store_db, "connect", lambda url, **_: FakeTagConn())
    monkeypatch.setattr(store_queries, "session_summary", lambda _conn, _sid: None)
    assert app_module.run_tag(tag_cfg(), 4242, season=1) == 1


def test_main_wires_the_tag_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_run_tag(cfg: Any, session_id: Any, **kwargs: Any) -> int:
        calls["session_id"] = session_id
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(app_module, "run_tag", fake_run_tag)
    assert cli_main(["tag", "5", "--season", "2", "--round", "3", "--note", "x"]) == 0
    assert calls == {
        "session_id": 5,
        "season": 2,
        "round_no": 3,
        "note": "x",
        "clear": False,
        "list_tags": False,
    }

    calls.clear()
    assert cli_main(["tag", "--list"]) == 0
    assert calls["session_id"] is None
    assert calls["list_tags"] is True

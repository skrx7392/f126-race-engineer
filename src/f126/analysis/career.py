"""Career derivation: seasons, weekends, per-track evolution, personal bests.

`docs/analysis-api.md` Phase 3 is the frozen contract. Everything here is derived from the
recorded sessions with no manual input — the game never says "round 3 of season 2", so
season and round are computed from the timeline by rules the payload itself states, and a
`career_tags` pin (written only by `f126 tag`) overrides the derivation where it is wrong.

Like the other engines in this package, this module is a function from rows to payloads:
no database, no HTTP, no clock. The routes fetch and hand in rows, so every rule below is
testable against synthetic sessions. Deterministic for a given input — every ordering is
total.

Two doctrines carry over from the fact sheet, deliberately by import rather than by copy:

* the **representative-lap rule** (`representative_lap_times` + the stint engine's
  exclusions) is what "consistency" means, here and in the debrief, one implementation;
* the **classification is the only source of results**. A weekend whose race lost its
  FinalClassification packet reports null positions and null points — a missing input
  never becomes a zero, and nothing is inferred from the last position update.

The private-name imports from `factsheet` are the smallest honest way to share those
implementations inside this package; duplicating them here is how the two pages would
drift into meaning different things by the same word.
"""

from __future__ import annotations

# Mapping is imported at runtime, not under TYPE_CHECKING: classification rows come back
# as whatever psycopg made of the JSONB, so they are isinstance-checked before use.
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from f126.analysis.factsheet import (
    _classification_row,
    _classification_rows,
    _excluded_laps,
    _positive,
    _quantiles,
    _session_label,
    representative_lap_times,
)
from f126.analysis.stints import build_stints
from f126.analysis.strategy import compound_name
from f126.parser.enums import SessionType, result_status_name

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---- derivation constants ---------------------------------------------------------------

WEEKEND_GAP_S = 48.0 * 3600.0
#: Largest gap between consecutive sessions at one circuit that still belongs to the same
#: career weekend. A weekend is driven across one or a few sittings — practice tonight,
#: quali and race tomorrow — so 48 h comfortably spans the realistic pattern while sitting
#: far below the weeks that separate two *visits* to the same circuit in consecutive
#: seasons. The gap is measured start-to-start because `ended_at_wall` is missing on
#: crashed segments, and no session runs long enough for the difference to flip a split.

RACE_SESSION_TYPES: frozenset[int] = frozenset(
    {int(SessionType.RACE), int(SessionType.RACE_2), int(SessionType.RACE_3)}
)
#: Race-family types (15-17). The game numbers a sprint among these — a real Miami sprint
#: arrived as 16 "Race 2" — so within a weekend the *last* race-type session is the grand
#: prix and the earlier ones are sprints. Type alone cannot tell them apart.

QUALI_SESSION_TYPES: frozenset[int] = frozenset(
    {
        int(SessionType.QUALIFYING_1),
        int(SessionType.QUALIFYING_2),
        int(SessionType.QUALIFYING_3),
        int(SessionType.SHORT_QUALIFYING),
        int(SessionType.ONE_SHOT_QUALIFYING),
    }
)
#: GP qualifying (5-9) only. The sprint shootouts (10-14) are deliberately absent: they set
#: the sprint grid, and a shootout P1 is not a pole.

TIME_TRIAL_SESSION_TYPE = int(SessionType.TIME_TRIAL)
#: Excluded from weekends — a time trial is not a career round — but its laps still count
#: for the personal bests and the track page's session list.

#: The derivation rules, restated in the payload so the season/round numbers on the career
#: page are visibly computed facts rather than something the game said.
NOTES: dict[str, str] = {
    "weekend_rule": (
        "a weekend is the maximal chronological run of sessions at one circuit, split when "
        "consecutive sessions there are more than 48 h apart; time trials are not career "
        "rounds and are excluded (their laps still count for personal bests)"
    ),
    "season_rule": (
        "seasons start at 1 and increment when a weekend starts at a circuit already "
        "visited that season; round is the weekend's 1-based index within its season. A "
        "`f126 tag` pin overrides the derivation for its weekend and later weekends derive "
        "forward from the pinned value"
    ),
}


# ---- small, total helpers ---------------------------------------------------------------


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except TypeError, ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except TypeError, ValueError:
        return None
    return out if np.isfinite(out) else None


def _chrono_key(row: Mapping[str, Any]) -> tuple[bool, float, int]:
    """Total chronological order: wall start, then id — unknown starts sort last."""
    start = _float_or_none(row.get("started_at_wall"))
    return (start is None, start if start is not None else 0.0, _int_or_none(row.get("id")) or 0)


def _session_type(row: Mapping[str, Any]) -> int | None:
    return _int_or_none(row.get("session_type"))


def _player_car(session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> int | None:
    """Whose classification row to read. Never assumed to be car 0.

    The session row carries the resolved player index (`participants.is_player`, falling
    back to `sessions.player_car_index`); the lap rows are the player's by query
    construction, so their `car_index` is the second witness. None means the session never
    learned who the player was, and no classification claim is made for it.
    """
    car = _int_or_none(session.get("player_car_index"))
    if car is not None:
        return car
    for row in laps:
        car = _int_or_none(row.get("car_index"))
        if car is not None:
            return car
    return None


def _sorted_laps(laps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        (row for row in laps if _int_or_none(row.get("lap_number")) is not None),
        key=lambda row: int(row["lap_number"]),
    )


def _valid_timed(laps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row for row in laps if row.get("valid") and (_int_or_none(row.get("lap_time_ms")) or 0) > 0
    ]


def _best_valid_lap_ms(laps: Sequence[Mapping[str, Any]]) -> int | None:
    times = [int(row["lap_time_ms"]) for row in _valid_timed(laps)]
    return min(times) if times else None


def _top_speed(laps: Sequence[Mapping[str, Any]]) -> float | None:
    """Fastest recorded speed over *all* laps — validity does not un-reach a speed."""
    speeds = [
        speed
        for row in laps
        if (speed := _float_or_none(row.get("top_speed_kmh"))) is not None and speed > 0
    ]
    return round(max(speeds), 1) if speeds else None


def _track_name(rows: Sequence[Mapping[str, Any]]) -> str | None:
    for row in rows:
        name = row.get("track_name")
        if name:
            return str(name)
    return None


# ---- weekends ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Weekend:
    """One career weekend: a cluster of same-circuit sessions, chronological."""

    track_id: int
    track_name: str | None
    rows: list[Mapping[str, Any]]
    season: int = 0
    round_no: int = 0
    tag: Mapping[str, Any] | None = None
    tag_conflict: bool = False


def _partition(
    sessions: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int]:
    """`(weekend-eligible sessions in chronological order, untracked count)`.

    Untracked = a session the career timeline cannot place: no circuit (`track_id` is the
    -1 "Session packet never landed" sentinel) or no wall-clock start to order it by. Time
    trials are *not* untracked — they are excluded from weekends by design and still feed
    the personal bests.
    """
    considered: list[Mapping[str, Any]] = []
    untracked = 0
    for row in sorted(sessions, key=_chrono_key):
        track = _int_or_none(row.get("track_id"))
        if track is None or track < 0:
            untracked += 1
            continue
        if _session_type(row) == TIME_TRIAL_SESSION_TYPE:
            continue
        if _float_or_none(row.get("started_at_wall")) is None:
            untracked += 1
            continue
        considered.append(row)
    return considered, untracked


def _cluster(considered: Sequence[Mapping[str, Any]]) -> list[_Weekend]:
    """Weekends: per-circuit chronological runs, split on a gap over `WEEKEND_GAP_S`.

    Grouped per circuit rather than on the interleaved global order, so a stray session at
    another track between two sittings of the same weekend cannot split it — the 48 h gap
    is the one splitting rule.
    """
    by_track: dict[int, list[Mapping[str, Any]]] = {}
    for row in considered:
        by_track.setdefault(int(row["track_id"]), []).append(row)

    weekends: list[_Weekend] = []
    for track, rows in by_track.items():
        group: list[Mapping[str, Any]] = []
        for row in rows:
            if group:
                gap = float(row["started_at_wall"]) - float(group[-1]["started_at_wall"])
                if gap > WEEKEND_GAP_S:
                    weekends.append(_Weekend(track, _track_name(group), group))
                    group = []
            group.append(row)
        weekends.append(_Weekend(track, _track_name(group), group))
    weekends.sort(key=lambda weekend: _chrono_key(weekend.rows[0]))
    return weekends


def _apply_tags(weekends: Sequence[_Weekend], tag_rows: Sequence[Mapping[str, Any]]) -> None:
    """Attach each weekend's winning tag and flag disagreements.

    A tag row belongs to one session (`session_uid`); it pins the whole weekend that
    session fell into. Several sessions of one weekend may carry tags: the newest
    `updated_at` wins, and the weekend is flagged when the pins disagree on
    `(season, round)` — differing notes are annotations, not a conflict.
    """
    by_uid = {
        str(tag["session_uid"]): tag for tag in tag_rows if tag.get("session_uid") is not None
    }
    for weekend in weekends:
        found = [
            by_uid[str(row["session_uid"])]
            for row in weekend.rows
            if row.get("session_uid") is not None and str(row["session_uid"]) in by_uid
        ]
        if not found:
            continue
        weekend.tag = max(
            found,
            key=lambda tag: (
                _float_or_none(tag.get("updated_at")) or 0.0,
                str(tag.get("session_uid")),
            ),
        )
        pins = {(_int_or_none(tag.get("season")), _int_or_none(tag.get("round"))) for tag in found}
        weekend.tag_conflict = len(pins) > 1


def _assign(weekends: Sequence[_Weekend]) -> None:
    """Number the weekends: season and 1-based round, tags pinning, forward derivation.

    A season rolls over when a weekend's circuit repeats within the running season — a
    24-round career never visits a circuit twice in one season. A tag pins its weekend's
    `(season, round)` and *suppresses* the repeat rule there (the pin exists precisely
    because the derivation was wrong); every later weekend derives forward from the pinned
    state. A tag with no round pins only the season and lets the round stay derived.
    """
    season = 1
    round_no = 0
    seen: set[int] = set()
    for weekend in weekends:
        pin_season = None if weekend.tag is None else _int_or_none(weekend.tag.get("season"))
        pin_round = None if weekend.tag is None else _int_or_none(weekend.tag.get("round"))
        if pin_season is not None:
            if pin_season != season:
                season = pin_season
                round_no = 0
                seen = set()
            round_no = pin_round if pin_round is not None else round_no + 1
        else:
            if weekend.track_id in seen:
                season += 1
                round_no = 0
                seen = set()
            round_no += 1
        seen.add(weekend.track_id)
        weekend.season = season
        weekend.round_no = round_no


def _headline_sessions(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """`(grand prix, sprints, quali)` for one weekend's chronological sessions.

    The last race-type session is the grand prix and the earlier ones are sprints — the
    wire type cannot tell them apart (a real sprint arrived as "Race 2"), but a sprint is
    always run before its GP. Quali is the last GP-qualifying session; shootouts are not
    consulted here at all.
    """
    race_rows = [row for row in rows if _session_type(row) in RACE_SESSION_TYPES]
    quali_rows = [row for row in rows if _session_type(row) in QUALI_SESSION_TYPES]
    grand_prix = race_rows[-1] if race_rows else None
    quali = quali_rows[-1] if quali_rows else None
    return grand_prix, race_rows[:-1], quali


# ---- classification readers -------------------------------------------------------------


def _player_classification(
    session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The player's classification row, or None when the packet (or the player) is unknown."""
    player = _player_car(session, laps)
    if player is None:
        return None
    found = _classification_row(session, player)
    return None if found is None else found[0]


def _classified_position(
    session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]
) -> int | None:
    mine = _player_classification(session, laps)
    return None if mine is None else _positive(mine.get("position"))


def _fastest_lap_flag(session: Mapping[str, Any], player: int | None) -> bool | None:
    """Whether the player's classified best lap is the strict minimum across the field.

    Null when there is no classification to read. A tie is not the fastest lap (strict),
    and a car whose classified best is 0 set no lap and cannot beat anyone. False, not
    null, when the player's own best is missing: the game recorded that they set no lap.
    """
    rows = _classification_rows(session)
    if rows is None or player is None:
        return None
    mine = next(
        (
            row
            for row in rows
            if isinstance(row, Mapping) and _int_or_none(row.get("car_index")) == player
        ),
        None,
    )
    if mine is None:
        return None
    my_best = _int_or_none(mine.get("best_lap_ms"))
    if my_best is None or my_best <= 0:
        return False
    for row in rows:
        if not isinstance(row, Mapping) or _int_or_none(row.get("car_index")) == player:
            continue
        other = _int_or_none(row.get("best_lap_ms"))
        if other is not None and 0 < other <= my_best:
            return False
    return True


def _status_word(mine: Mapping[str, Any]) -> str | None:
    status = _int_or_none(mine.get("result_status"))
    return None if status is None else result_status_name(status).lower()


def _quali_block(session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mine = _player_classification(session, laps)
    best = _best_valid_lap_ms(laps)
    if best is None and mine is not None:
        # The lap table lost the flyer but the game classified it — still a measurement.
        classified = _int_or_none(mine.get("best_lap_ms"))
        best = classified if classified and classified > 0 else None
    return {
        "session_id": int(session["id"]),
        "position": None if mine is None else _positive(mine.get("position")),
        "best_lap_ms": best,
    }


def _sprint_block(session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mine = _player_classification(session, laps)
    return {
        "session_id": int(session["id"]),
        "position": None if mine is None else _positive(mine.get("position")),
        "grid_position": None if mine is None else _positive(mine.get("grid_position")),
        "points": None if mine is None else _int_or_none(mine.get("points")),
        "status": None if mine is None else _status_word(mine),
    }


def _race_block(session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mine = _player_classification(session, laps)
    classified_best = None if mine is None else _int_or_none(mine.get("best_lap_ms"))
    if not classified_best or classified_best <= 0:
        classified_best = None
    return {
        "session_id": int(session["id"]),
        "position": None if mine is None else _positive(mine.get("position")),
        "grid_position": None if mine is None else _positive(mine.get("grid_position")),
        "points": None if mine is None else _int_or_none(mine.get("points")),
        "pit_stops": None if mine is None else _int_or_none(mine.get("num_pit_stops")),
        # The classified best is what the fastest-lap flag compares, so it leads; a race
        # with no classification still reports the measured best from the lap table.
        "best_lap_ms": classified_best if classified_best else _best_valid_lap_ms(laps),
        "fastest_lap": None
        if mine is None
        else _fastest_lap_flag(session, _player_car(session, laps)),
        "status": None if mine is None else _status_word(mine),
    }


def _classified_points(session: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> int | None:
    mine = _player_classification(session, laps)
    return None if mine is None else _int_or_none(mine.get("points"))


# ---- consistency ------------------------------------------------------------------------


def _consistency_stats(
    session: Mapping[str, Any],
    laps: Sequence[Mapping[str, Any]],
    stint_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """`laps_used` / `median_ms` / `iqr_ms` / `cv_pct` over the representative laps.

    The population is the fact sheet's rule, imported rather than restated: valid, timed,
    and not excluded by the stint engine (in/out laps, pit laps, 107 % outliers). The
    spread statistics stay null below the four-lap floor `_quantiles` already enforces —
    the "consistency" of two laps is the spread of two numbers. `cv_pct` is the sample
    standard deviation (ddof=1) over the mean, per the contract.
    """
    session_id = int(session["id"])
    car = _player_car(session, laps)
    ordered = _sorted_laps(laps)
    stints_payload = build_stints(session_id, stint_rows, ordered, car_index=car)
    excluded = _excluded_laps(stints_payload, car if car is not None else 0)
    times = representative_lap_times(ordered, excluded)

    out: dict[str, Any] = {
        "laps_used": len(times),
        "median_ms": None,
        "iqr_ms": None,
        "cv_pct": None,
    }
    quartiles = _quantiles(times)
    if quartiles is not None:
        q1, median, q3 = quartiles
        out["median_ms"] = int(round(median))
        out["iqr_ms"] = int(round(q3 - q1))
        mean = float(np.mean(times))
        if mean > 0:
            out["cv_pct"] = round(float(np.std(times, ddof=1)) / mean * 100.0, 2)
    return out


def _weekend_consistency(
    grand_prix: Mapping[str, Any] | None,
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    stints_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """The weekend's headline consistency: the grand prix's racing laps, or null."""
    if grand_prix is None:
        return None
    session_id = int(grand_prix["id"])
    stats = _consistency_stats(
        grand_prix,
        list(laps_by_session.get(session_id, ())),
        list(stints_by_session.get(session_id, ())),
    )
    return {"session_id": session_id, **stats}


# ---- weekend payloads -------------------------------------------------------------------


def _session_entry(row: Mapping[str, Any], laps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_type": _session_type(row),
        "session_type_name": row.get("session_type_name"),
        "started_at_wall": _float_or_none(row.get("started_at_wall")),
        "best_lap_ms": _best_valid_lap_ms(laps),
    }


def _weekend_payload(
    weekend: _Weekend,
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    stints_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    rows = weekend.rows
    grand_prix, sprints, quali = _headline_sessions(rows)
    laps = {int(row["id"]): list(laps_by_session.get(int(row["id"]), ())) for row in rows}

    # Points sum every race-type session the game classified — sprint and GP alike. A
    # session with no classification contributes nothing rather than a zero; a weekend
    # where none was classified has no points figure at all.
    race_rows = [*sprints, *([grand_prix] if grand_prix is not None else [])]
    known_points = [
        points
        for row in race_rows
        if (points := _classified_points(row, laps[int(row["id"])])) is not None
    ]

    ends = [
        end
        for row in rows
        if (
            end := _float_or_none(row.get("ended_at_wall"))
            or _float_or_none(row.get("started_at_wall"))
        )
        is not None
    ]

    return {
        "season": weekend.season,
        "round": weekend.round_no,
        "track_id": weekend.track_id,
        "track_name": weekend.track_name,
        "format": "sprint" if sprints else "standard",
        "started_at_wall": _float_or_none(rows[0].get("started_at_wall")),
        "ended_at_wall": max(ends) if ends else None,
        "session_ids": [int(row["id"]) for row in rows],
        "sessions": [_session_entry(row, laps[int(row["id"])]) for row in rows],
        "quali": None if quali is None else _quali_block(quali, laps[int(quali["id"])]),
        "sprint": None if not sprints else _sprint_block(sprints[-1], laps[int(sprints[-1]["id"])]),
        "race": None
        if grand_prix is None
        else _race_block(grand_prix, laps[int(grand_prix["id"])]),
        "points": sum(known_points) if known_points else None,
        "consistency": _weekend_consistency(grand_prix, laps_by_session, stints_by_session),
        "tags": None
        if weekend.tag is None
        else {
            "season": _int_or_none(weekend.tag.get("season")),
            "round": _int_or_none(weekend.tag.get("round")),
            "note": weekend.tag.get("note"),
        },
        "tag_conflict": weekend.tag_conflict,
    }


# ---- totals -----------------------------------------------------------------------------


def _fresh_totals() -> dict[str, int]:
    return {
        "points": 0,
        "wins": 0,
        "podiums": 0,
        "poles": 0,
        "fastest_laps": 0,
        "sprint_wins": 0,
        "races": 0,
    }


def _accumulate(
    totals: dict[str, int],
    weekend: _Weekend,
    payload: Mapping[str, Any],
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
) -> None:
    """Fold one weekend into a totals block. Unknowns contribute nothing, never a zero."""
    if payload["points"] is not None:
        totals["points"] += int(payload["points"])

    race = payload["race"]
    if race is not None:
        totals["races"] += 1
        if race["position"] == 1:
            totals["wins"] += 1
        if race["position"] is not None and race["position"] <= 3:
            totals["podiums"] += 1
        if race["fastest_lap"] is True:
            totals["fastest_laps"] += 1

    quali = payload["quali"]
    if quali is not None and quali["position"] == 1:
        totals["poles"] += 1

    # Counted over every sprint in the weekend, not only the reported last one — the
    # points already sum them all, and a weekend with two sprints must not lose a win.
    _, sprints, _ = _headline_sessions(weekend.rows)
    for row in sprints:
        position = _classified_position(row, list(laps_by_session.get(int(row["id"]), ())))
        if position == 1:
            totals["sprint_wins"] += 1


# ---- personal bests ---------------------------------------------------------------------


def _pb_tracks(
    sessions: Sequence[Mapping[str, Any]],
) -> list[tuple[int, list[Mapping[str, Any]]]]:
    """Circuits with sessions, in chronological first-visit order, time trials included."""
    groups: dict[int, list[Mapping[str, Any]]] = {}
    for row in sorted(sessions, key=_chrono_key):
        track = _int_or_none(row.get("track_id"))
        if track is None or track < 0:
            continue
        groups.setdefault(track, []).append(row)
    return list(groups.items())


def _track_pairs(
    track_rows: Sequence[Mapping[str, Any]],
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Every `(session row, player lap row)` pair at one circuit, chronological."""
    return [(row, lap) for row in track_rows for lap in laps_by_session.get(int(row["id"]), ())]


def _sector_best(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], key: str
) -> tuple[int, int, int] | None:
    """`(sector_ms, session_id, lap_number)` of the circuit's best valid sector, or None."""
    best: tuple[int, tuple[Any, ...], int, int] | None = None
    for session, lap in pairs:
        if not lap.get("valid"):
            continue
        value = _int_or_none(lap.get(key))
        lap_number = _int_or_none(lap.get("lap_number"))
        if value is None or value <= 0 or lap_number is None:
            continue
        candidate = (value, _chrono_key(session), int(session["id"]), lap_number)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else (best[0], best[2], best[3])


def _pb_entry(
    track_id: int,
    track_rows: Sequence[Mapping[str, Any]],
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """The overview `pbs` entry for one circuit. None when no lap was ever recorded there."""
    pairs = _track_pairs(track_rows, laps_by_session)
    if not pairs:
        return None

    best_pair: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    best_key: tuple[Any, ...] | None = None
    for session, lap in pairs:
        if not lap.get("valid"):
            continue
        time_ms = _int_or_none(lap.get("lap_time_ms"))
        if time_ms is None or time_ms <= 0:
            continue
        key = (time_ms, _chrono_key(session), _int_or_none(lap.get("lap_number")) or 0)
        if best_key is None or key < best_key:
            best_key, best_pair = key, (session, lap)

    sectors = {index: _sector_best(pairs, f"s{index}_ms") for index in (1, 2, 3)}
    theoretical = (
        sum(found[0] for found in sectors.values())
        if all(found is not None for found in sectors.values())
        else None
    )

    entry: dict[str, Any] = {
        "track_id": int(track_id),
        "track_name": _track_name(track_rows),
        "best_lap_ms": None,
        "session_id": None,
        "session_label": None,
        "lap_number": None,
        "compound_visual": None,
        "compound_name": None,
        "set_at_wall": None,
        "theoretical_ms": theoretical,
        "top_speed_kmh": _top_speed([lap for _, lap in pairs]),
    }
    if best_pair is not None:
        session, lap = best_pair
        visual = _int_or_none(lap.get("compound_visual"))
        entry.update(
            best_lap_ms=int(lap["lap_time_ms"]),
            session_id=int(session["id"]),
            session_label=_session_label(session),
            lap_number=_int_or_none(lap.get("lap_number")),
            compound_visual=visual,
            compound_name=None if visual is None else compound_name(visual),
            # The lap's own wall stamp — never approximated from the session start.
            set_at_wall=_float_or_none(lap.get("wall_ts")),
        )
    return entry


def _pb_sectors(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    """Sector provenance for the track page: where each best sector was set."""
    out: dict[str, Any] = {}
    for index in (1, 2, 3):
        found = _sector_best(pairs, f"s{index}_ms")
        out[f"s{index}_ms"] = None if found is None else found[0]
        out[f"s{index}_session_id"] = None if found is None else found[1]
        out[f"s{index}_lap_number"] = None if found is None else found[2]
    return out


# ---- the payloads -----------------------------------------------------------------------


def _derived_weekends(
    sessions: Sequence[Mapping[str, Any]], tag_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[_Weekend], int]:
    considered, untracked = _partition(sessions)
    weekends = _cluster(considered)
    _apply_tags(weekends, tag_rows)
    _assign(weekends)
    return weekends, untracked


def build_overview(
    sessions: Sequence[Mapping[str, Any]],
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    stints_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    tag_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the `GET /api/career/overview` payload.

    Args:
        sessions: one row per game session (`queries.career_sessions`), any order.
        laps_by_session: session id -> the player's laps there, newest generation each.
        stints_by_session: session id -> the player's recorded tyre stints there.
        tag_rows: every `career_tags` row.

    Returns a JSON-serialisable dict. An empty database yields empty arrays, not an error.
    """
    weekends, untracked = _derived_weekends(sessions, tag_rows)

    seasons: dict[int, dict[str, Any]] = {}
    for weekend in weekends:
        payload = _weekend_payload(weekend, laps_by_session, stints_by_session)
        entry = seasons.setdefault(
            weekend.season,
            {"season": weekend.season, "rounds": 0, "totals": _fresh_totals(), "weekends": []},
        )
        entry["weekends"].append(payload)
        # Weekends recorded in the season, not the highest round number — a tag can pin
        # round 7 of a season this database only holds two weekends of.
        entry["rounds"] = len(entry["weekends"])
        _accumulate(entry["totals"], weekend, payload, laps_by_session)

    ordered = [seasons[season] for season in sorted(seasons)]
    career_totals = _fresh_totals()
    for season in ordered:
        for key in career_totals:
            career_totals[key] += season["totals"][key]

    pbs = [
        entry
        for track_id, track_rows in _pb_tracks(sessions)
        if (entry := _pb_entry(track_id, track_rows, laps_by_session)) is not None
    ]

    return {
        "seasons": ordered,
        "career_totals": career_totals,
        "pbs": pbs,
        "untracked_sessions": untracked,
        "notes": dict(NOTES),
    }


def build_track(
    track_id: int,
    sessions: Sequence[Mapping[str, Any]],
    laps_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    stints_by_session: Mapping[int, Sequence[Mapping[str, Any]]],
    tag_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the `GET /api/career/tracks/{track_id}` payload.

    `sessions` is the whole career, not just this circuit: season and round numbers only
    exist relative to the full timeline, so the visits here carry the same numbers the
    overview shows. `laps_by_session` / `stints_by_session` need only cover this circuit.
    """
    track_id = int(track_id)
    track_rows = [
        row
        for row in sorted(sessions, key=_chrono_key)
        if _int_or_none(row.get("track_id")) == track_id
    ]
    weekends, _untracked = _derived_weekends(sessions, tag_rows)

    visits: list[dict[str, Any]] = []
    for weekend in weekends:
        if weekend.track_id != track_id:
            continue
        rows = weekend.rows
        grand_prix, sprints, quali = _headline_sessions(rows)
        laps = {int(row["id"]): list(laps_by_session.get(int(row["id"]), ())) for row in rows}

        best_ms: int | None = None
        best_session: int | None = None
        for row in rows:
            candidate = _best_valid_lap_ms(laps[int(row["id"])])
            if candidate is not None and (best_ms is None or candidate < best_ms):
                best_ms, best_session = candidate, int(row["id"])

        visits.append(
            {
                "season": weekend.season,
                "round": weekend.round_no,
                "started_at_wall": _float_or_none(rows[0].get("started_at_wall")),
                "session_ids": [int(row["id"]) for row in rows],
                "best_lap_ms": best_ms,
                "best_lap_session_id": best_session,
                "quali": None if quali is None else _quali_block(quali, laps[int(quali["id"])]),
                "sprint": None
                if not sprints
                else _sprint_block(sprints[-1], laps[int(sprints[-1]["id"])]),
                "race": None
                if grand_prix is None
                else _race_block(grand_prix, laps[int(grand_prix["id"])]),
                "consistency": _weekend_consistency(grand_prix, laps_by_session, stints_by_session),
            }
        )

    pb = _pb_entry(track_id, track_rows, laps_by_session)
    if pb is not None:
        pb["sectors"] = _pb_sectors(_track_pairs(track_rows, laps_by_session))

    sessions_out: list[dict[str, Any]] = []
    for row in track_rows:
        session_id = int(row["id"])
        laps_here = list(laps_by_session.get(session_id, ()))
        stats = _consistency_stats(row, laps_here, list(stints_by_session.get(session_id, ())))
        sessions_out.append(
            {
                "id": session_id,
                "session_type": _session_type(row),
                "session_type_name": row.get("session_type_name"),
                "started_at_wall": _float_or_none(row.get("started_at_wall")),
                "best_lap_ms": _best_valid_lap_ms(laps_here),
                "laps_total": len(
                    {
                        lap_number
                        for lap in laps_here
                        if (lap_number := _int_or_none(lap.get("lap_number"))) is not None
                    }
                ),
                "laps_used": stats["laps_used"],
                "median_ms": stats["median_ms"],
                "iqr_ms": stats["iqr_ms"],
                "cv_pct": stats["cv_pct"],
                "top_speed_kmh": _top_speed(laps_here),
            }
        )

    return {
        "track_id": track_id,
        "track_name": _track_name(track_rows),
        "pb": pb,
        "visits": visits,
        "sessions": sessions_out,
    }

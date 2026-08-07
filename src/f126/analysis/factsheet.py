"""The deterministic fact sheet a debrief is written from.

This module computes **every number** that ends up in a post-session debrief. The LLM that
turns it into prose is given this dict and nothing else, and is told in the system prompt
that it may not compute or infer a figure that is not in it (`f126.llm`). That division is
the whole design: arithmetic here, where it is testable and reproducible, and only sentence
construction there.

Two properties follow, and both are load-bearing:

* **Deterministic.** The same session yields a byte-identical sheet on every run. No clocks,
  no iteration over unordered containers, every float rounded on the way out.
* **None-tolerant.** A session that lost its Session packet, never wrote telemetry, or was
  abandoned after two laps still produces a valid sheet — the pieces that cannot be computed
  are *omitted* and the reason is recorded under `"omitted"`. Nothing is ever guessed, and a
  missing input never becomes a zero. A zero in this sheet means the measurement was made
  and came out zero.

Unlike the four analysis engines next to it, this one takes a `conn` — it is the aggregator
that fetches and assembles, so the engines can stay pure and testable against synthetic laps.

Units are in the field names (`_ms`, `_s`, `_kmh`, `_kg`, `_m`, `_c`, `_pct`) and repeated in
the `"units"` block, because the reader is a language model and an unlabelled number is an
invitation to guess.
"""

from __future__ import annotations

import math

# Mapping is imported at runtime, not under TYPE_CHECKING: the JSONB columns come back as
# whatever psycopg made of them, so several sections `isinstance`-check before trusting one.
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from f126.analysis.compare import LapRef
from f126.analysis.corners import analyse_corners
from f126.analysis.resample import AnalysisError, trace_from_rows
from f126.analysis.stints import build_stints
from f126.parser.enums import (
    PenaltyType,
    ResultStatus,
    SafetyCarEventType,
    SafetyCarType,
    SessionType,
    compound_name,
    result_status_name,
    safety_car_status_name,
    visual_compound_name,
    weather_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from f126.store.queries import Conn, JsonRow

#: Bumped when the *shape* of the sheet changes in a way a prompt would notice. Stored on
#: every debrief row next to the prompt version, so an old debrief stays interpretable.
FACT_SHEET_VERSION = 1

#: Race-family session types, from the parser's authoritative table. Only these carry a
#: finishing position and points; a quali "position" would be the grid slot, which is a
#: different claim and is reported under its own name.
_RACE_SESSION_TYPES: frozenset[int] = frozenset(
    {int(SessionType.RACE), int(SessionType.RACE_2), int(SessionType.RACE_3)}
)

#: How many corners the sheet names. The debrief is 200-300 words; a table of 16 corners
#: would either be ignored or padded out into filler. Three is what fits in a paragraph.
TOP_CORNERS = 3

#: Carried inside every corner comparison, because this is the one place the sheet can be
#: read as claiming something it does not. `analyse_corners` segments braking zones by speed
#: prominence and numbers them in track order — at Jeddah that is 8 zones across a 27-turn
#: circuit, so "corner 8" is the eighth *detected* zone, not Turn 8. A debrief that says
#: "turn 8" sends the driver to the wrong corner, which is worse than saying nothing.
CORNER_NUMBERING_NOTE = (
    "Corner numbers are detected braking zones in track order, NOT the circuit's official "
    "turn numbers. Identify a corner by its apex_m distance, never as 'Turn N'."
)

#: Per-lap fuel burn outside this band is not a racing lap: a negative or zero burn means the
#: lap was not driven under power (or the fuel columns were never populated), and no F1 car
#: burns 5 kg on a single lap. Both bounds exist to keep practice-program noise out of the
#: median rather than to model the car.
MIN_BURN_KG = 0.05
MAX_BURN_KG = 5.0

#: How much a degradation slope is worth believing, by the same r2 bands the stint page
#: renders (`analysis-format.ts::fitConfidence`). It is in the sheet as a *word* because the
#: model is forbidden from interpreting a raw 0.49 for itself, and a slope fitted through
#: five scattered laps stated as fact is exactly the kind of confident wrongness a grounded
#: debrief is supposed to rule out.
FIT_STRONG_R2 = 0.8
FIT_FAIR_R2 = 0.5

#: A lap that starts with more fuel than the previous one ended with was refuelled between
#: them — the practice-program reset the F1 games do when you pick a new programme. The lap
#: is dropped and the chain restarts, because its "burn" is a garage event, not a lap.
#: The tolerance absorbs float noise in the game's own telemetry, which reports fuel to
#: about 10 g; 0.2 kg is two orders of magnitude above that and far below any real top-up.
REFUEL_TOLERANCE_KG = 0.2


# ---- small, total helpers ------------------------------------------------------------
#
# Everything below returns None rather than raising or substituting a default. That is what
# lets the builders stay linear: a missing input propagates to an omitted field instead of
# to a try/except around every access.


def _num(value: Any) -> float | None:
    """A finite float, or None. NaN and inf are missing values, not numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except TypeError, ValueError:
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _round(value: Any, digits: int = 1) -> float | None:
    number = _num(value)
    return None if number is None else round(number, digits)


def _u8(value: Any) -> int | None:
    """A u8 field, with the game's 0xFF "not applicable" sentinel read as missing.

    The event payloads pack every optional byte as 255 rather than omitting it, so a Warning
    — which carries no time — arrives as `time_s: 255`. Passed through, the sheet states a
    255-second penalty and the debrief repeats it. This is the difference between a missing
    value and a number, and the whole module depends on that distinction holding.
    """
    number = _int(value)
    return None if number is None or number == 255 else number


def _positive(value: Any) -> int | None:
    """A 1-based position, with 0 read as "no position". Quali rows carry `grid_position: 0`."""
    number = _int(value)
    return None if number is None or number <= 0 else number


def _lap_time_str(ms: Any) -> str | None:
    """`92345` -> `"1:32.345"`. The debrief quotes lap times the way a driver reads them."""
    total = _int(ms)
    if total is None or total <= 0:
        return None
    minutes, remainder = divmod(total, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def _delta_str(ms: Any) -> str | None:
    """A signed gap in seconds, e.g. `"+0.412"`. Positive always means slower/behind."""
    total = _num(ms)
    if total is None:
        return None
    return f"{total / 1000.0:+.3f}"


def _quantiles(values: Sequence[float]) -> tuple[float, float, float] | None:
    """(q1, median, q3) by linear interpolation, or None below four samples.

    Four is the floor because a quartile of three points is just the points again, and an
    IQR that is really "the spread of two numbers" would be reported as a consistency
    measurement. numpy's default `linear` method is used so the result is the same one
    every other tool computing quartiles on this data would get.
    """
    if len(values) < 4:
        return None
    array = np.asarray(values, dtype=np.float64)
    q1, median, q3 = (float(v) for v in np.percentile(array, [25.0, 50.0, 75.0]))
    return q1, median, q3


def _epoch_to_iso(epoch: Any) -> str | None:
    seconds = _num(epoch)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _player_index(session: Mapping[str, Any]) -> int:
    """The player's car index, defaulting to 0 when the Session packet never landed."""
    index = _int(session.get("player_car_index"))
    return 0 if index is None else index


def _sorted_laps(rows: Sequence[JsonRow], car_index: int) -> list[JsonRow]:
    """One car's laps, ordered, newest generation already resolved by the query."""
    return sorted(
        (row for row in rows if _int(row.get("car_index")) == car_index
         and _int(row.get("lap_number")) is not None),
        key=lambda row: int(row["lap_number"]),
    )


# ---- section builders ----------------------------------------------------------------


def _session_section(session: Mapping[str, Any], lap_rows: Sequence[JsonRow]) -> dict[str, Any]:
    session_type = _int(session.get("session_type"))
    track_id = _int(session.get("track_id"))
    started = _num(session.get("started_at_wall"))
    ended = _num(session.get("ended_at_wall"))
    player_name = None
    for row in session.get("participants") or ():
        if row.get("is_player"):
            player_name = row.get("name")
            break

    out: dict[str, Any] = {
        "session_id": _int(session.get("id")),
        "type": session.get("session_type_name") or "Unknown session",
        # A track_id of -1 is the sentinel for "the Session packet never arrived", not a
        # circuit; reporting `track_-1` as a track name would be a fabricated fact.
        "track": session.get("track_name") if track_id is not None and track_id >= 0 else None,
        "started_at_utc": _epoch_to_iso(started),
        "driver": player_name,
        "is_race": session_type in _RACE_SESSION_TYPES if session_type is not None else False,
        "laps_recorded_all_cars": len({
            (_int(r.get("car_index")), _int(r.get("lap_number"))) for r in lap_rows
        }),
    }
    scheduled = _int(session.get("total_laps"))
    if scheduled:
        out["scheduled_laps"] = scheduled
    if started is not None and ended is not None and ended > started:
        out["wall_duration_min"] = _round((ended - started) / 60.0, 1)
    if session.get("ended_reason"):
        out["ended_reason"] = session["ended_reason"]
    return {key: value for key, value in out.items() if value is not None}


def _classification_row(
    session: Mapping[str, Any], player: int
) -> tuple[Mapping[str, Any], int] | None:
    """The player's row in the game's final classification, plus the field size."""
    classification = session.get("final_classification_json")
    if not isinstance(classification, Mapping):
        return None
    rows = classification.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    mine = next(
        (row for row in rows if isinstance(row, Mapping) and _int(row.get("car_index")) == player),
        None,
    )
    return None if mine is None else (mine, len(rows))


def _result_section(session: Mapping[str, Any], player: int) -> dict[str, Any] | None:
    """Finishing position and points for a RACE, out of the game's final classification.

    Race-only, and gated hard. The game writes a classification packet for qualifying and
    practice too, and it carries a stale `points` value and a `total_race_time_s` that is
    really just the lap time — measured on a real one-shot quali, this section claimed the
    driver had scored 15 championship points for a single flying lap. Non-race sessions get
    `_standing_section` instead, which reports only the fields that mean something there.

    Returns None when the session was quit before the flag: inferring a finishing position
    from the last position update would be inventing the headline fact of the debrief.
    """
    session_type = _int(session.get("session_type"))
    if session_type not in _RACE_SESSION_TYPES:
        return None
    found = _classification_row(session, player)
    if found is None:
        return None
    mine, field_size = found

    position = _positive(mine.get("position"))
    grid = _positive(mine.get("grid_position"))
    status = _int(mine.get("result_status"))
    out: dict[str, Any] = {
        "finish_position": position,
        "grid_position": grid,
        "points_scored": _int(mine.get("points")),
        "pit_stops": _int(mine.get("num_pit_stops")),
        "penalties_s": _int(mine.get("penalties_s")),
        "classified_cars": field_size,
        "status": result_status_name(status) if status is not None else None,
        "finished": status == int(ResultStatus.FINISHED) if status is not None else None,
    }
    if position is not None and grid is not None:
        # Positive = places gained. The game numbers positions with 1 at the front, so the
        # sign has to be flipped to read the way a driver would say it.
        out["places_gained"] = grid - position
    race_time = _num(mine.get("total_race_time_s"))
    if race_time is not None and race_time > 0:
        out["race_time_s"] = _round(race_time, 3)
    best = _int(mine.get("best_lap_ms"))
    if best:
        out["classified_best_lap"] = _lap_time_str(best)
    return {key: value for key, value in out.items() if value is not None}


def _standing_section(session: Mapping[str, Any], player: int) -> dict[str, Any] | None:
    """Where the driver ended up on the timing sheet, for a non-race session.

    Deliberately narrow. A qualifying classification row also carries points, a grid slot and
    a "race time", and every one of those is meaningless — or actively wrong — outside a
    race. Only the position and the field size survive.
    """
    session_type = _int(session.get("session_type"))
    if session_type in _RACE_SESSION_TYPES:
        return None
    found = _classification_row(session, player)
    if found is None:
        return None
    mine, field_size = found
    position = _positive(mine.get("position"))
    if position is None:
        return None
    return {"position_in_session": position, "classified_cars": field_size}


def _excluded_laps(stints_payload: Mapping[str, Any], car_index: int) -> dict[int, str]:
    """Lap number -> why the stint engine excluded it from a degradation fit.

    Reused verbatim for the consistency statistics: an out-lap, an in-lap or a lap under a
    safety car is a real lap and belongs in the lap count, but folding it into a median and
    an IQR would report the pit stop as inconsistent driving.
    """
    excluded: dict[int, str] = {}
    for stint in stints_payload.get("stints") or ():
        if _int(stint.get("car_index")) not in (car_index, None):
            continue
        for entry in stint.get("laps") or ():
            lap = _int(entry.get("lap_number"))
            if lap is not None and entry.get("excluded"):
                excluded[lap] = str(entry.get("exclude_reason") or "excluded")
    return excluded


def _pace_section(
    laps: Sequence[JsonRow], excluded: Mapping[int, str]
) -> tuple[dict[str, Any], JsonRow | None]:
    """Lap count, personal best, and the median/IQR consistency pair.

    Also returns the best lap row, because the corner analysis is anchored on it and
    re-deriving it somewhere else would risk the two sections disagreeing.
    """
    timed = [row for row in laps if (_int(row.get("lap_time_ms")) or 0) > 0]
    valid = [row for row in timed if row.get("valid")]
    best = min(valid, key=lambda row: int(row["lap_time_ms"]), default=None)

    out: dict[str, Any] = {
        "laps_completed": len(laps),
        "laps_timed": len(timed),
        "laps_valid": len(valid),
        "laps_invalidated": len(timed) - len(valid),
    }
    if best is not None:
        out["best_lap_ms"] = _int(best["lap_time_ms"])
        out["best_lap"] = _lap_time_str(best["lap_time_ms"])
        out["best_lap_number"] = _int(best.get("lap_number"))
        sectors = [_int(best.get(key)) for key in ("s1_ms", "s2_ms", "s3_ms")]
        if all(sector for sector in sectors):
            out["best_lap_sectors_ms"] = sectors

    # Representative laps: valid, timed, and not one the stint engine already ruled out as
    # an in/out lap or an outlier. This is the set the consistency numbers describe, and it
    # is named in the sheet so the debrief can say what it measured.
    representative = [
        row for row in valid if _int(row.get("lap_number")) not in excluded
    ]
    times = [float(row["lap_time_ms"]) for row in representative]
    out["consistency_laps_used"] = len(times)
    dropped = sorted(
        {_int(row.get("lap_number")) for row in valid} & set(excluded),
        key=lambda lap: (lap is None, lap),
    )
    if dropped:
        out["laps_excluded_from_consistency"] = [
            {"lap": lap, "reason": excluded[lap]} for lap in dropped if lap is not None
        ]

    quartiles = _quantiles(times)
    if quartiles is not None:
        q1, median, q3 = quartiles
        out["median_lap_ms"] = _round(median, 0)
        out["median_lap"] = _lap_time_str(round(median))
        out["iqr_ms"] = _round(q3 - q1, 0)
        out["iqr_s"] = _round((q3 - q1) / 1000.0, 3)
        out["q1_lap_ms"] = _round(q1, 0)
        out["q3_lap_ms"] = _round(q3, 0)
        if out.get("best_lap_ms") is not None:
            out["median_minus_best_ms"] = _round(median - float(out["best_lap_ms"]), 0)
    return {key: value for key, value in out.items() if value is not None}, best


def _fit_confidence(r2: float | None) -> str:
    """`"strong" | "fair" | "weak"` — how much of the lap-time spread the line explains."""
    if r2 is None:
        return "weak"
    if r2 >= FIT_STRONG_R2:
        return "strong"
    return "fair" if r2 >= FIT_FAIR_R2 else "weak"


def _compound_label(name: str) -> str:
    """`"F1_SOFT"` -> `"Soft"`, `"F2_SUPER_SOFT"` -> `"F2 Super Soft"`.

    The enum names are wire identifiers; a race engineer says "the soft". The series prefix
    is only kept when it is not the F1 tyre the rest of the sheet is about.
    """
    if name.startswith("F1_"):
        name = name[3:]
    return name.replace("_", " ").title()


def _stints_section(stints_payload: Mapping[str, Any], car_index: int) -> list[dict[str, Any]]:
    """One compact entry per stint: compound, lap range, tyre life, degradation slope."""
    out: list[dict[str, Any]] = []
    for stint in stints_payload.get("stints") or ():
        if _int(stint.get("car_index")) not in (car_index, None):
            continue
        entry: dict[str, Any] = {
            "stint": _int(stint.get("stint_no")),
            "lap_start": _int(stint.get("lap_start")),
            "lap_end": _int(stint.get("lap_end")),
        }
        visual = _int(stint.get("compound_visual"))
        if visual is not None and visual >= 0:
            entry["compound"] = _compound_label(visual_compound_name(visual))
        actual = _int(stint.get("compound_actual"))
        if actual is not None and actual >= 0:
            entry["compound_actual"] = _compound_label(compound_name(actual))
        laps = [e for e in (stint.get("laps") or ()) if (_int(e.get("lap_time_ms")) or 0) > 0]
        used = [e for e in laps if not e.get("excluded")]
        entry["laps_on_set"] = len(laps)
        entry["laps_used_for_fit"] = len(used)
        if used:
            entry["best_lap_on_set"] = _lap_time_str(
                min(int(e["lap_time_ms"]) for e in used)
            )
        fit = stint.get("fit")
        if isinstance(fit, Mapping):
            # r2 is carried through so the prose can hedge a weak fit instead of quoting a
            # slope from four scattered laps as if it were a measurement.
            entry["degradation_ms_per_lap"] = _round(fit.get("deg_ms_per_lap"), 1)
            # Three decimals, not two: measured on a real race, both stints rounded to 0.00
            # at two, which makes "no correlation at all" indistinguishable from "weak".
            entry["degradation_fit_r2"] = _round(fit.get("r2"), 3)
            entry["degradation_laps_fitted"] = _int(fit.get("n_used"))
            entry["degradation_confidence"] = _fit_confidence(_num(fit.get("r2")))
        wear = stint.get("wear_end_pct")
        if isinstance(wear, list) and any(value is not None for value in wear):
            entry["tyre_wear_at_end_pct"] = [_round(value, 1) for value in wear]
        out.append({key: value for key, value in entry.items() if value is not None})
    return out


def _fuel_section(laps: Sequence[JsonRow]) -> dict[str, Any] | None:
    """Median fuel burn per lap, with garage refuels excluded rather than averaged in.

    The F1 games reset the tank when a practice programme is selected, so `fuel_start_kg`
    jumps *up* mid-session. Averaging across that boundary produces a negative burn on one
    lap and a plausible-looking but wrong mean overall, which is exactly the kind of number
    a debrief would state with confidence.
    """
    burns: list[float] = []
    used_laps: list[int] = []
    refuelled: list[int] = []
    previous_end: float | None = None

    for row in laps:
        lap = _int(row.get("lap_number"))
        start, end = _num(row.get("fuel_start_kg")), _num(row.get("fuel_end_kg"))
        if start is None or end is None:
            previous_end = end
            continue
        if previous_end is not None and start > previous_end + REFUEL_TOLERANCE_KG:
            if lap is not None:
                refuelled.append(lap)
            previous_end = end
            continue
        burn = start - end
        if MIN_BURN_KG <= burn <= MAX_BURN_KG:
            burns.append(burn)
            if lap is not None:
                used_laps.append(lap)
        previous_end = end

    if not burns:
        return None
    out: dict[str, Any] = {
        "burn_per_lap_kg": _round(float(np.median(np.asarray(burns, dtype=np.float64))), 3),
        "laps_measured": len(burns),
        "total_burn_kg": _round(sum(burns), 2),
    }
    if refuelled:
        out["laps_ignored_refuelled"] = refuelled
    first_start = _num(laps[0].get("fuel_start_kg")) if laps else None
    last_end = _num(laps[-1].get("fuel_end_kg")) if laps else None
    if first_start is not None:
        out["fuel_at_first_lap_kg"] = _round(first_start, 2)
    if last_end is not None:
        out["fuel_at_last_lap_kg"] = _round(last_end, 2)
    return out


def _events_section(
    events: Sequence[JsonRow],
    player: int,
    stint_count: int,
    result: Mapping[str, Any] | None,
    *,
    is_race: bool,
) -> dict[str, Any]:
    """Safety cars, penalties, flashbacks and pit stops, counted rather than narrated."""
    safety_cars: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []
    flashbacks = 0
    collisions = 0
    retired = False

    for event in events:
        code = event.get("code")
        details = event.get("details_json")
        details = details if isinstance(details, Mapping) else {}
        if code == "FLBK":
            flashbacks += 1
        elif code == "SCAR":
            # Only deployments are counted. The game emits the returning/returned/resume
            # transitions of the same intervention as separate events, and counting those
            # would report one safety car as four.
            if _int(details.get("event_type")) == int(SafetyCarEventType.DEPLOYED):
                kind = _int(details.get("safety_car_type"))
                safety_cars.append(
                    {
                        "type": safety_car_status_name(kind) if kind is not None else None,
                        "session_time_s": _round(event.get("session_time_s"), 1),
                    }
                )
        elif code == "PENA" and _int(details.get("vehicle_idx")) == player:
            kind = _int(details.get("penalty_type"))
            # Every optional byte here is 255 when it does not apply — a Warning carries no
            # duration, an unserved penalty no lap. `_u8` turns those back into absence.
            penalties.append(
                {
                    "type": _penalty_name(kind),
                    "lap": _u8(details.get("lap_num")),
                    "seconds": _u8(details.get("time_s")),
                    "infringement_code": _u8(details.get("infringement_type")),
                }
            )
        elif code == "COLL" and player in {
            _int(details.get("vehicle1_idx")),
            _int(details.get("vehicle2_idx")),
        }:
            collisions += 1
        elif code == "RTMT" and _int(details.get("vehicle_idx")) == player:
            retired = True

    out: dict[str, Any] = {
        "flashbacks": flashbacks,
        "collisions_involving_player": collisions,
        "safety_car_deployments": len(safety_cars),
    }
    if safety_cars:
        out["safety_cars"] = [
            {key: value for key, value in car.items() if value is not None}
            for car in safety_cars
        ]
        out["virtual_safety_car_deployments"] = sum(
            1 for car in safety_cars if car.get("type") == safety_car_status_name(
                int(SafetyCarType.VIRTUAL)
            )
        )
    out["penalties"] = [
        {key: value for key, value in penalty.items() if value is not None}
        for penalty in penalties
    ]
    if retired:
        out["player_retired"] = True

    # Pit stops: the classification is the game's own count and wins outright. The stint
    # fallback is race-only — measured on a real practice session, five compound changes
    # across a programme became "4 pit stops", which is a garage count wearing the name of a
    # racing decision. Outside a race the field is omitted rather than guessed at.
    stops = None if result is None else _int(result.get("pit_stops"))
    if stops is None and is_race and stint_count > 0:
        stops = stint_count - 1
    if stops is not None:
        out["pit_stops"] = stops
    return out


def _penalty_name(kind: int | None) -> str | None:
    if kind is None:
        return None
    try:
        return PenaltyType(kind).name.replace("_", " ").title()
    except ValueError:
        return f"penalty_{kind}"


def _weather_section(session: Mapping[str, Any]) -> dict[str, Any] | None:
    weather = session.get("weather_json")
    if not isinstance(weather, Mapping):
        return None
    kind = _int(weather.get("weather"))
    out: dict[str, Any] = {
        "conditions": weather_name(kind) if kind is not None else None,
        "track_temp_c": _round(weather.get("track_temp_c"), 1),
        "air_temp_c": _round(weather.get("air_temp_c"), 1),
    }
    forecast = weather.get("forecast")
    if isinstance(forecast, list) and forecast:
        rain = [
            _int(sample.get("rain_percentage"))
            for sample in forecast
            if isinstance(sample, Mapping)
        ]
        rain = [value for value in rain if value is not None]
        if rain:
            out["max_rain_probability_pct"] = max(rain)
        upcoming = []
        for sample in forecast:
            if not isinstance(sample, Mapping):
                continue
            code = _int(sample.get("weather"))
            if code is None:
                continue
            name = weather_name(code)
            if name not in upcoming:
                upcoming.append(name)
        if upcoming:
            out["forecast_conditions"] = upcoming
    cleaned = {key: value for key, value in out.items() if value is not None}
    return cleaned or None


# ---- corner analysis -----------------------------------------------------------------


def _lap_ref(conn: Conn, session: Mapping[str, Any], lap_number: int, car: int) -> LapRef | None:
    """Assemble the identity + trace bundle `analyse_corners` works on. None if undrawable.

    Deliberately a local copy of the web layer's helper rather than an import: the analysis
    package must not depend on the web package, and fifteen lines of assembly is a cheaper
    price than that edge in the dependency graph.
    """
    from f126.store import queries  # noqa: PLC0415 — keeps `analysis` importable without psycopg

    rows = queries.telemetry_trace(conn, int(session["id"]), lap_number)
    if not rows:
        return None
    try:
        trace = trace_from_rows(rows)
    except AnalysisError:
        return None
    lap = queries.lap_row(conn, int(session["id"]), car, lap_number) or {}
    return LapRef(
        session_id=int(session["id"]),
        lap_number=int(lap_number),
        trace=trace,
        car_index=car,
        track_id=session.get("track_id"),
        track_name=session.get("track_name"),
        lap_time_ms=lap.get("lap_time_ms"),
        sectors_ms=(lap.get("s1_ms"), lap.get("s2_ms"), lap.get("s3_ms")),
    )


def _corner_comparison(
    subject: LapRef, reference: LapRef, label: str | None
) -> dict[str, Any] | None:
    """`analyse_corners`, reduced to the handful of facts a paragraph can carry."""
    try:
        payload = analyse_corners(subject, reference, ref_session_label=label)
    except AnalysisError:
        return None

    corners = [
        corner for corner in payload["corners"] if _num(corner.get("time_loss_ms")) is not None
    ]
    corners.sort(key=lambda corner: (-float(corner["time_loss_ms"]), int(corner["n"])))

    worst: list[dict[str, Any]] = []
    for corner in corners[:TOP_CORNERS]:
        loss = _num(corner.get("time_loss_ms"))
        if loss is None or loss <= 0:
            # Only corners where time was actually lost. A "worst" corner that gained time
            # is not a focus item, and listing it as one would misdirect the next session.
            continue
        item: dict[str, Any] = {
            "corner": _int(corner.get("n")),
            "kind": corner.get("kind"),
            "apex_m": _round(corner.get("apex_m"), 0),
            "time_loss_ms": _int(loss),
            "time_loss_s": _round(loss / 1000.0, 3),
            "min_speed_kmh": _round(corner.get("min_speed_kmh"), 1),
            "ref_min_speed_kmh": _round(corner.get("ref_min_speed_kmh"), 1),
        }
        mine, theirs = _num(corner.get("min_speed_kmh")), _num(corner.get("ref_min_speed_kmh"))
        if mine is not None and theirs is not None:
            item["min_speed_delta_kmh"] = _round(mine - theirs, 1)
        brake, ref_brake = _num(corner.get("brake_point_m")), _num(corner.get("ref_brake_point_m"))
        if brake is not None:
            item["brake_point_m"] = _round(brake, 0)
        if ref_brake is not None:
            item["ref_brake_point_m"] = _round(ref_brake, 0)
        if brake is not None and ref_brake is not None:
            item["brake_point_delta_m"] = _round(brake - ref_brake, 1)
        worst.append({key: value for key, value in item.items() if value is not None})

    out: dict[str, Any] = {
        "reference_lap": reference.lap_number,
        "reference_session_id": reference.session_id,
        "reference_lap_time": _lap_time_str(reference.lap_time_ms),
        "subject_lap": subject.lap_number,
        "subject_lap_time": _lap_time_str(subject.lap_time_ms),
        "total_delta_ms": _int(payload.get("total_delta_ms")),
        "total_delta_s": _delta_str(payload.get("total_delta_ms")),
        "straights_time_loss_ms": _int(payload.get("straights_time_loss_ms")),
        "corners_segmented": len(payload["corners"]),
        "corner_numbering": CORNER_NUMBERING_NOTE,
        "worst_corners": worst,
    }
    if label:
        out["reference_label"] = label
    return {key: value for key, value in out.items() if value is not None}


def _corners_section(
    conn: Conn, session: Mapping[str, Any], best_lap: JsonRow | None, car: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """The subject lap measured against the session best and the circuit best.

    Returns `(section, omitted)`. Both comparisons are optional and independently so: a
    one-shot quali has no second lap to compare against, and the first visit to a circuit
    has no track benchmark. Neither is an error.
    """
    from f126.store import queries  # noqa: PLC0415

    omitted: dict[str, str] = {}
    lap_number = None if best_lap is None else _int(best_lap.get("lap_number"))
    if lap_number is None:
        omitted["corners"] = "no valid timed lap to analyse"
        return {}, omitted

    session_id = int(session["id"])
    subject = _lap_ref(conn, session, lap_number, car)
    if subject is None:
        omitted["corners"] = f"lap {lap_number} has no telemetry trace"
        return {}, omitted

    section: dict[str, Any] = {
        "subject_lap": lap_number,
        "subject_lap_time": _lap_time_str(best_lap.get("lap_time_ms")),
        "subject_is_session_best": True,
    }

    # vs the session's own next-best drawable lap. The subject IS the session best, so the
    # reference is the second-best lap: "how much of your own best was repeatable".
    own = queries.best_lap_with_telemetry(conn, session_id, car, exclude_lap=lap_number)
    if own is None:
        omitted["corners_vs_session_best"] = "no second drawable lap in this session"
    else:
        reference = _lap_ref(conn, session, int(own["lap_number"]), car)
        comparison = (
            None if reference is None else _corner_comparison(subject, reference, None)
        )
        if comparison is None:
            omitted["corners_vs_session_best"] = "reference lap could not be resampled"
        else:
            section["vs_session_best"] = comparison

    # vs the circuit benchmark, across every session recorded here — a race lap against the
    # weekend's qualifying time.
    track_id = _int(session.get("track_id"))
    if track_id is None or track_id < 0:
        omitted["corners_vs_track_best"] = "session did not record which circuit it was"
        return section, omitted

    track = queries.track_best_lap_with_telemetry(
        conn, track_id, exclude_session_id=session_id, exclude_lap=lap_number
    )
    if track is None:
        omitted["corners_vs_track_best"] = "no other session recorded at this circuit"
        return section, omitted

    ref_session = queries.session_summary(conn, int(track["session_id"]))
    if ref_session is None:
        omitted["corners_vs_track_best"] = "reference session is no longer in the database"
        return section, omitted

    reference = _lap_ref(
        conn, ref_session, int(track["lap_number"]), _player_index(ref_session)
    )
    label = _session_label(ref_session)
    comparison = None if reference is None else _corner_comparison(subject, reference, label)
    if comparison is None:
        omitted["corners_vs_track_best"] = "benchmark lap could not be resampled"
    else:
        section["vs_track_best"] = comparison
    return section, omitted


def _session_label(session: Mapping[str, Any]) -> str | None:
    """`"One-Shot Qualifying · Aug 07"` — the same caption the corners endpoint emits."""
    name = session.get("session_type_name") or "Session"
    started = _num(session.get("started_at_wall"))
    if started is None:
        return str(name)
    return f"{name} · {datetime.fromtimestamp(started, tz=UTC).strftime('%b %d')}"


# ---- the sheet -----------------------------------------------------------------------

#: What every suffix in the sheet means, restated for a reader that cannot look it up. The
#: sign conventions are here because they are the two things a writer would otherwise get
#: backwards, and a debrief that inverts them is worse than no debrief.
UNITS: dict[str, str] = {
    "_ms": "milliseconds",
    "_s": "seconds",
    "_kmh": "kilometres per hour",
    "_kg": "kilograms",
    "_m": "metres",
    "_c": "degrees Celsius",
    "_pct": "percent",
    "time_loss": "positive = slower than the reference through that corner",
    "total_delta": "positive = the subject lap is slower than the reference lap",
    "brake_point_delta_m": "negative = braked earlier than the reference",
    "min_speed_delta_kmh": "negative = slower through the corner than the reference",
    "places_gained": "positive = finished ahead of the grid slot",
    "corner": (
        "index of a detected braking zone in track order, NOT an official circuit turn "
        "number. Refer to a corner by its apex_m distance."
    ),
    "degradation_ms_per_lap": "lap time added per lap of tyre age; positive = losing time",
    "degradation_confidence": (
        "how well the degradation line fits the laps it was drawn through. Treat a 'weak' "
        "slope as unmeasured rather than as a result, and do not describe it as degradation."
    ),
}


def build_fact_sheet(
    conn: Conn, session_id: int, *, car_index: int | None = None
) -> dict[str, Any]:
    """Every number a debrief is allowed to state, for one session.

    Args:
        conn: a read connection. Nothing here writes.
        session_id: `sessions.id` — the representative segment, as the analysis endpoints
            use it.
        car_index: whose laps to describe. Defaults to the session's player car, which is
            the only car with telemetry and therefore the only one that can be cornered.

    Returns:
        A JSON-ready dict. `{"error": ...}` is never returned — an unknown session raises,
        and every *partial* session produces a partial sheet with the gaps named under
        `"omitted"`.

    Raises:
        AnalysisError: 404 when the session id is not in the database.
    """
    from f126.store import queries  # noqa: PLC0415

    session = queries.session_detail(conn, session_id)
    if session is None:
        raise AnalysisError(404, "session not found")

    car = _player_index(session) if car_index is None else int(car_index)
    all_laps = queries.laps_for_session(conn, session_id)
    laps = _sorted_laps(all_laps, car)
    events = queries.events_for_session(conn, session_id)
    recorded_stints = queries.tyre_stints_for_session(conn, session_id, car)
    stints_payload = build_stints(session_id, recorded_stints, all_laps, car_index=car)

    omitted: dict[str, str] = {}
    excluded = _excluded_laps(stints_payload, car)
    pace, best_lap = _pace_section(laps, excluded)
    stints = _stints_section(stints_payload, car)
    result = _result_section(session, car)
    standing = _standing_section(session, car)
    corners, corner_omissions = _corners_section(conn, session, best_lap, car)
    omitted.update(corner_omissions)

    sheet: dict[str, Any] = {
        "fact_sheet_version": FACT_SHEET_VERSION,
        "units": UNITS,
        "session": _session_section(session, all_laps),
        "pace": pace,
    }
    is_race = bool(sheet["session"].get("is_race"))
    if result is not None:
        sheet["result"] = result
    elif is_race:
        omitted["result"] = "the race produced no final classification packet"
    if standing is not None:
        sheet["standing"] = standing

    if stints:
        sheet["stints"] = stints
    else:
        omitted["stints"] = "no tyre stints recorded or derivable"

    if corners:
        sheet["corners"] = corners

    fuel = _fuel_section(laps)
    if fuel is not None:
        sheet["fuel"] = fuel
    else:
        omitted["fuel"] = "no lap recorded a usable fuel start/end pair"

    sheet["events"] = _events_section(events, car, len(stints), result, is_race=is_race)

    weather = _weather_section(session)
    if weather is not None:
        sheet["weather"] = weather
    else:
        omitted["weather"] = "no weather was recorded for this session"

    if omitted:
        sheet["omitted"] = dict(sorted(omitted.items()))
    return sheet

"""Pre-race strategy from the weekend's own telemetry, and nothing else.

Every number this module produces was measured at *this* circuit, in *these* sessions, by
*this* driver. There is no tyre database, no per-track table of pit-lane losses and no
compound model shipped in the source: a compound nobody ran this weekend comes back
`untested` with null model fields, and a plan that would need it is not offered. That
restraint is the whole point — a strategy sheet that quietly invents the hard tyre's
degradation is worse than one that says it does not know.

What is measured, and from what:

* **Degradation** — the least-squares lap-time line `stints.py` already fits, taken from
  the best-evidenced stint on each compound. Not re-derived here: `build_stints` is called
  and its `fit` is read, so the degradation on this page and the degradation on the stints
  page are the same number computed once.
* **Wear rate** — %/lap of the worst wheel, regressed against lap number over the
  `wear_samples` rows inside the stint, falling back to the stint's recorded end wear over
  the laps it ran when samples are missing.
* **Fuel burn** — mean of `fuel_start_kg - fuel_end_kg` over the laps `stints.py` did not
  exclude, which is exactly the set of laps that were racing rather than pitting.
* **Pit-lane loss** — the excess of a real pit lap over the driver's own clean-lap median,
  measured at this circuit. Only falls back to a constant when the weekend contains no
  stop at all, and says so when it does.

Evidence is tiered. A race or sprint stint beats a practice stint on the same compound,
because practice is push running on new tyres with a light car and its degradation and wear
numbers are roughly double race trim (measured: Miami softs 9-10 %/lap in P1, 5.6 %/lap in
the sprint). The tier that produced each number travels with it in the payload, so a model
built from practice can be recognised as such rather than trusted as race data.

Nothing here touches the database; the route hands in rows, exactly as `stints.py` works.
Deterministic for a given input: every ordering is total and every float is rounded on the
way out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any

import numpy as np

from f126.analysis.stints import build_stints, pit_flags

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

# ---- tuning constants -------------------------------------------------------------------

WEAR_CLIFF_PCT = 28.0
#: Max-wheel wear at which a set is treated as finished. Measured, not assumed: the softs
#: fell off a cliff at 28-31 % max-wheel wear in two races at two different circuits
#: (Jeddah, 30.5 % by lap 6; Miami, 28.0 % over a five-lap race and 34.7 % by the stop in
#: the sprint, where the last two laps cost 2.8 s and 5.3 s). 28 is the bottom of that
#: observed band, so a stint planned to this number stops *before* the collapse rather than
#: during it. Telemetry percent, not the in-game display, which reads roughly twice this.

MIN_WEAR_SAMPLE_LAPS = 2
#: Fewest distinct laps a wear rate may be measured over. One lap is not a rate: it holds
#: the scrub-in of a brand-new set and, on the first lap of a stint, part of a pit lane.

MIN_FUEL_LAPS = 3
#: Fewest laps behind a fuel-burn figure. Two laps and one mis-captured tank reading moves
#: the recommendation by a kilogram, which is a lap of fuel on a short race.

DEFAULT_PIT_LOSS_S = 21.0
#: Pit-lane loss used only when the weekend contains no measurable stop. 21 s is the middle
#: of the modern calendar's range (Monza ~19 s, Singapore ~28 s, most circuits 20-23 s) and
#: is flagged as a default in `pit_loss.source` wherever it is used, because a plan ranked
#: on a guessed pit loss is a plan whose stop count was chosen by that guess.

FUEL_MARGIN_LAPS = 0.45
#: Laps of fuel to carry beyond the race distance. It buys the formation lap, the in-lap,
#: and the extra throttle of a safety-car restart; at ~1 kg/lap it is under half a kilo, so
#: it costs almost nothing in lap time and removes the only failure that cannot be
#: recovered from. Validated against the real thing: the Miami sprint was run with 15.0 kg
#: for 14 laps at a measured 1.04 kg/lap, which is 14.42 laps of fuel — this rule asks for
#: 14.45.

MAX_STOPS = 2
#: One and two-stop plans only. Three stops has never beaten two over a race length this
#: recorder sees, and the enumeration grows as compounds^(stops+1).

MAX_PLANS = 12
#: How many ranked plans come back. Everything feasible is *considered* and counted in
#: `plans_considered`; this caps what is serialised, because past a dozen the list stops
#: being a decision and becomes a table.

SC_FLEXIBLE_WINDOW_LAPS = 3
#: A stop whose viable window is at least this wide can be taken under a safety car without
#: breaking the plan. Narrower than this and the window closes before a typical SC period
#: is over, so the stop has to be taken on schedule whatever the race is doing.

RACE_TRIM_SESSION_TYPES = frozenset(range(15, 18))
#: Session types whose running is race trim: the races themselves (sprints arrive as one
#: of these too — a real Miami sprint came in as 16 "Race 2", Montreal's as 15). The
#: sprint SHOOTOUTS (10-14) are qualifying-shaped and stay in the practice tier: a
#: shootout stint is an out-lap plus one flyer, which can't produce a degradation fit but
#: CAN produce a wear rate and a fuel delta — and on a real Montreal weekend those two
#: laps beat P1's seven-lap soft run for the "race evidence" tie-break and published a
#: 1.29 %/lap soft wear model from a tyre that never got hot.

PRACTICE_SESSION_TYPES = frozenset(range(1, 15))
#: Practice and qualifying. Usable evidence, clearly worse evidence: push laps on new tyres
#: with a light car. Tiered below race trim and labelled as such wherever it is used.

RACE_TIER = "race"
PRACTICE_TIER = "practice"
#: Order matters: `_TIER_RANK` is what "race-trim preferred" means in code.
_TIER_RANK = {RACE_TIER: 0, PRACTICE_TIER: 1}

WET_VISUAL_COMPOUNDS = frozenset({7, 8, 15})
#: Intermediates and wets. They appear in the compound table when they were run — the
#: recorder saw a sprint shootout on inters — but never in a plan: this module models a dry
#: race, and the two-compound rule it obeys does not apply to a wet one.

PLANNED_DRY_COMPOUNDS: tuple[int, ...] = (16, 17, 18)
#: The three dry compounds a plan may be built from, always listed even when untested, so
#: "we have no hard-tyre data" is a visible fact rather than a missing row.

#: `compound_visual` as the broadcast graphic names it, matching `frontend/lib/enums.ts`.
COMPOUND_NAMES: dict[int, str] = {
    7: "Intermediate",
    8: "Wet",
    15: "Wet",
    16: "Soft",
    17: "Medium",
    18: "Hard",
    19: "Soft",
    20: "Soft",
    21: "Medium",
    22: "Hard",
}

#: A per-lap tank delta outside this range is a garage refuel, a session reset or a lost
#: packet, not a lap of running. Real burn is ~1.0-3.5 kg/lap across the calendar.
_MIN_PLAUSIBLE_BURN_KG = 0.05
_MAX_PLAUSIBLE_BURN_KG = 8.0


def compound_name(visual: int | None) -> str:
    if visual is None:
        return "Unknown"
    return COMPOUND_NAMES.get(int(visual), f"Compound {int(visual)}")


def is_dry_compound(visual: int | None) -> bool:
    return visual is not None and int(visual) not in WET_VISUAL_COMPOUNDS


# ---- observations -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Session:
    """One session of the weekend, reduced to what the strategy cares about."""

    id: int
    session_type: int
    session_type_name: str
    tier: str | None
    total_laps: int | None
    started_at_wall: float | None

    @property
    def label(self) -> str:
        return f"{self.session_type_name} (session {self.id})"


@dataclass(frozen=True, slots=True)
class _Observation:
    """One stint's worth of evidence about one compound."""

    session: _Session
    compound_visual: int
    stint_no: int | None
    lap_start: int | None
    lap_end: int | None
    #: Laps genuinely run on this set — the stint's range with the shared pit laps taken
    #: off both ends, which is `pit_flags`' rule and the same one the fit uses.
    laps_on_set: int
    fit: dict[str, Any] | None
    wear_pct_per_lap: float | None
    wear_source: str | None
    wear_laps: int

    @property
    def fit_laps(self) -> int:
        return 0 if self.fit is None else int(self.fit.get("n_used") or 0)


def _tier(session_type: int | None) -> str | None:
    if session_type is None:
        return None
    if int(session_type) in RACE_TRIM_SESSION_TYPES:
        return RACE_TIER
    if int(session_type) in PRACTICE_SESSION_TYPES:
        return PRACTICE_TIER
    # Time trial (18), and the type-0 stub a session gets when its Session packet never
    # landed. Neither is running this can learn a compound from.
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _session_of(row: Mapping[str, Any]) -> _Session:
    session_type = _int_or_none(row.get("session_type"))
    return _Session(
        id=int(row["id"]),
        session_type=session_type if session_type is not None else 0,
        session_type_name=str(row.get("session_type_name") or "Session"),
        tier=_tier(session_type),
        total_laps=_int_or_none(row.get("total_laps")),
        started_at_wall=_float_or_none(row.get("started_at_wall")),
    )


def _wear_by_lap(rows: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """`lap_number -> worst-wheel wear %` from the wear-sample summary rows.

    The worst wheel, not the average: a set is finished when *one* corner is finished, and
    the rear-left at Jeddah reaches the cliff several laps before the front-right does.
    """
    out: dict[int, float] = {}
    for row in rows:
        lap = _int_or_none(row.get("lap_number"))
        wear = _float_or_none(row.get("max_wear_pct"))
        if lap is None or wear is None:
            continue
        out[lap] = max(out.get(lap, wear), wear)
    return out


def _wear_rate(
    wear_by_lap: Mapping[int, float], first_lap: int, last_lap: int
) -> tuple[float | None, str | None, int]:
    """Least-squares %/lap of worst-wheel wear across a stint's own laps.

    A regression rather than (end - start) / laps because the sample at either end of a
    stint is whatever the 1 Hz decimator happened to catch, and one unlucky sample would
    move a two-point rate by a whole lap's worth of wear.
    """
    laps = sorted(lap for lap in wear_by_lap if first_lap <= lap <= last_lap)
    if len(laps) < MIN_WEAR_SAMPLE_LAPS:
        return None, None, len(laps)
    x = np.asarray(laps, dtype=np.float64)
    y = np.asarray([wear_by_lap[lap] for lap in laps], dtype=np.float64)
    try:
        slope = float(np.polyfit(x, y, 1)[0])
    except (np.linalg.LinAlgError, ValueError):  # pragma: no cover - degenerate input
        return None, None, len(laps)
    if slope <= 0:
        # Wear does not go down. A non-positive slope means the window caught a set change
        # or a flashback rather than a set wearing, and dividing the cliff by it would hand
        # out an unbounded stint length.
        return None, None, len(laps)
    return slope, "wear_samples", len(laps)


def _wear_rate_from_end(
    stint: Mapping[str, Any], laps_on_set: int
) -> tuple[float | None, str | None, int]:
    """Fallback: the stint's recorded end wear spread over the laps it actually ran."""
    if laps_on_set < MIN_WEAR_SAMPLE_LAPS:
        return None, None, laps_on_set
    wear = stint.get("wear_end_pct")
    if not isinstance(wear, list | tuple):
        return None, None, laps_on_set
    values = [_float_or_none(v) for v in wear]
    peak = max((v for v in values if v is not None), default=None)
    if peak is None or peak <= 0:
        return None, None, laps_on_set
    return peak / laps_on_set, "stint_end_wear", laps_on_set


def _observe_session(
    session: _Session,
    stint_rows: Iterable[Mapping[str, Any]],
    lap_rows: Sequence[Mapping[str, Any]],
    wear_rows: Iterable[Mapping[str, Any]],
    *,
    car_index: int | None,
) -> tuple[list[dict[str, Any]], list[_Observation]]:
    """Run one session through `build_stints` and read the evidence out of the result."""
    stints = build_stints(session.id, stint_rows, lap_rows, car_index=car_index)["stints"]
    wear_by_lap = _wear_by_lap(wear_rows)
    observations: list[_Observation] = []

    for position, stint in enumerate(stints):
        compound = _int_or_none(stint.get("compound_visual"))
        lap_start = _int_or_none(stint.get("lap_start"))
        lap_end = _int_or_none(stint.get("lap_end"))
        if compound is None or lap_start is None or lap_end is None:
            continue
        after_pit, before_pit = pit_flags(position, len(stints))
        first = lap_start + (1 if after_pit else 0)
        last = lap_end - (1 if before_pit else 0)
        laps_on_set = max(0, last - first + 1)

        rate, source, wear_laps = _wear_rate(wear_by_lap, first, last)
        if rate is None:
            rate, source, wear_laps = _wear_rate_from_end(stint, laps_on_set)

        observations.append(
            _Observation(
                session=session,
                compound_visual=compound,
                stint_no=_int_or_none(stint.get("stint_no")),
                lap_start=lap_start,
                lap_end=lap_end,
                laps_on_set=laps_on_set,
                fit=stint.get("fit"),
                wear_pct_per_lap=rate,
                wear_source=source,
                wear_laps=wear_laps,
            )
        )
    return stints, observations


def _best(
    observations: Iterable[_Observation], key: Callable[[_Observation], bool]
) -> _Observation | None:
    """The most trustworthy observation satisfying `key`. Race trim first, then evidence."""
    candidates = [obs for obs in observations if key(obs)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda obs: (
            _TIER_RANK.get(obs.session.tier or PRACTICE_TIER, 9),
            -obs.fit_laps,
            -obs.laps_on_set,
            # Total order, so the same rows always produce the same sheet.
            obs.session.id,
            obs.stint_no or 0,
        ),
    )


# ---- per-compound models ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Model:
    """What a plan needs to know about one compound. Absent fields mean absent evidence."""

    compound_visual: int
    base_ms: float | None
    deg_ms_per_lap: float | None
    wear_pct_per_lap: float | None
    max_stint_laps: int | None

    @property
    def plannable(self) -> bool:
        return (
            self.base_ms is not None
            and self.deg_ms_per_lap is not None
            and self.max_stint_laps is not None
            and self.max_stint_laps >= 1
        )

    def stint_ms(self, laps: int) -> float:
        """Total time for `laps` consecutive laps on a fresh set of this compound."""
        assert self.base_ms is not None and self.deg_ms_per_lap is not None
        return laps * self.base_ms + self.deg_ms_per_lap * laps * (laps - 1) / 2.0

    def lap_ms(self, age: int) -> float:
        """Projected time of the lap run at tyre age `age` (0 = first lap on the set)."""
        assert self.base_ms is not None and self.deg_ms_per_lap is not None
        return self.base_ms + self.deg_ms_per_lap * age


def _compound_entry(
    compound: int,
    observations: Sequence[_Observation],
    race_laps: int,
) -> tuple[dict[str, Any], _Model]:
    """The payload row and the planning model for one compound."""
    dry = is_dry_compound(compound)
    pace_obs = _best(observations, lambda obs: obs.fit is not None)
    wear_obs = _best(observations, lambda obs: obs.wear_pct_per_lap is not None)

    entry: dict[str, Any] = {
        "compound_visual": compound,
        "name": compound_name(compound),
        "dry": dry,
        "untested": not observations,
        "stints_seen": len(observations),
        "evidence": None,
        "pace": None,
        "wear": None,
        "max_stint_laps": None,
        "plannable": False,
        "not_plannable_reason": None,
    }

    if not observations:
        entry["not_plannable_reason"] = "no stint on this compound was recorded this weekend"
        return entry, _Model(compound, None, None, None, None)

    base_ms = deg_ms = None
    if pace_obs is not None and pace_obs.fit is not None:
        fit = pace_obs.fit
        base_ms = _float_or_none(fit.get("base_ms"))
        deg_ms = _float_or_none(fit.get("deg_ms_per_lap"))
        entry["pace"] = {
            "base_ms": None if base_ms is None else round(base_ms, 1),
            "deg_ms_per_lap": None if deg_ms is None else round(deg_ms, 1),
            "r2": _round_or_none(fit.get("r2"), 4),
            "laps_used": pace_obs.fit_laps,
            "evidence": pace_obs.session.tier,
            "session_id": pace_obs.session.id,
            "session_label": pace_obs.session.label,
            "stint_no": pace_obs.stint_no,
            "lap_range": [pace_obs.lap_start, pace_obs.lap_end],
        }

    wear_rate = None
    if wear_obs is not None:
        wear_rate = wear_obs.wear_pct_per_lap
        entry["wear"] = {
            "pct_per_lap": _round_or_none(wear_rate, 2),
            "source": wear_obs.wear_source,
            "laps": wear_obs.wear_laps,
            "evidence": wear_obs.session.tier,
            "session_id": wear_obs.session.id,
            "session_label": wear_obs.session.label,
            "stint_no": wear_obs.stint_no,
        }

    # The compound's headline tier is the weaker of the two it is actually built from: a
    # race degradation slope paired with a practice wear rate is not race evidence.
    tiers = [
        obs.session.tier for obs in (pace_obs, wear_obs) if obs is not None and obs.session.tier
    ]
    entry["evidence"] = (
        max(tiers, key=lambda t: _TIER_RANK.get(t, 9)) if tiers else observations[0].session.tier
    )

    max_laps: int | None = None
    if wear_rate is not None and wear_rate > 0:
        max_laps = max(1, min(race_laps, int(WEAR_CLIFF_PCT // wear_rate)))
        entry["max_stint_laps"] = max_laps
        entry["projected_wear_at_max_pct"] = round(wear_rate * max_laps, 1)

    # Degradation is clamped at zero *for planning only*. A negative fitted slope means the
    # set got quicker as it aged — fuel burning off and the track rubbering in showing
    # through a short stint — and projecting it forward would recommend running one set to
    # the flag. The measured slope is reported unchanged above; this is the number the
    # allocator uses, and it is named so the two cannot be confused.
    planning_deg = None if deg_ms is None else max(deg_ms, 0.0)
    if planning_deg is not None and deg_ms is not None and planning_deg != deg_ms:
        entry["pace"]["deg_ms_per_lap_planned"] = 0.0

    model = _Model(compound, base_ms, planning_deg, wear_rate, max_laps)
    entry["plannable"] = model.plannable and dry
    if not entry["plannable"]:
        entry["not_plannable_reason"] = _why_not_plannable(dry, base_ms, wear_rate)
    return entry, model


def _why_not_plannable(dry: bool, base_ms: float | None, wear_rate: float | None) -> str:
    if not dry:
        return "wet-weather compound; this sheet plans a dry race"
    missing = []
    if base_ms is None:
        missing.append(
            "no degradation fit — no stint on it had enough clean laps to fit a line through"
        )
    if wear_rate is None:
        missing.append("no wear rate — no stint on it ran enough laps to measure one")
    return "; ".join(missing)


def _round_or_none(value: Any, digits: int) -> float | None:
    out = _float_or_none(value)
    return None if out is None else round(out, digits)


# ---- fuel -------------------------------------------------------------------------------


def _fuel_burn(
    tiered: Sequence[tuple[_Session, list[dict[str, Any]], Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Mean kg burned per racing lap, race trim preferred. `(payload, omission_reason)`.

    Only laps `stints.py` kept are counted, which is exactly the laps that were racing:
    out-laps, in-laps, deleted laps and anything over 107 % of the stint median are already
    flagged there, and every one of them burns an unrepresentative amount of fuel.
    """
    for tier in (RACE_TIER, PRACTICE_TIER):
        samples: list[float] = []
        used: list[int] = []
        for session, stints, lap_rows in tiered:
            if session.tier != tier:
                continue
            by_number = {
                _int_or_none(row.get("lap_number")): row
                for row in lap_rows
                if _int_or_none(row.get("lap_number")) is not None
            }
            before = len(samples)
            for stint in stints:
                for entry in stint["laps"]:
                    if entry["excluded"]:
                        continue
                    row = by_number.get(entry["lap_number"])
                    if row is None:
                        continue
                    start = _float_or_none(row.get("fuel_start_kg"))
                    end = _float_or_none(row.get("fuel_end_kg"))
                    if start is None or end is None:
                        continue
                    burn = start - end
                    if _MIN_PLAUSIBLE_BURN_KG <= burn <= _MAX_PLAUSIBLE_BURN_KG:
                        samples.append(burn)
            if len(samples) > before:
                used.append(session.id)
        if len(samples) >= MIN_FUEL_LAPS:
            return {
                "kg_per_lap": round(float(np.mean(samples)), 3),
                "laps_measured": len(samples),
                "evidence": tier,
                "session_ids": sorted(used),
            }, None

    return None, (
        "insufficient fuel data: fewer than "
        f"{MIN_FUEL_LAPS} laps at this circuit recorded both a start and an end tank "
        "reading. Captures made before per-lap fuel context was attached carry NULLs, and a "
        "fuel load guessed from a tank capacity would be a number this sheet invented."
    )


def _fuel_recommendation(burn: dict[str, Any] | None, race_laps: int) -> dict[str, Any] | None:
    if burn is None:
        return None
    slider_laps = race_laps + FUEL_MARGIN_LAPS
    return {
        **burn,
        "race_laps": race_laps,
        "margin_laps": FUEL_MARGIN_LAPS,
        "slider_laps": round(slider_laps, 2),
        "recommended_kg": round(slider_laps * burn["kg_per_lap"], 2),
    }


# ---- pit-lane loss ----------------------------------------------------------------------


def _pit_loss(
    tiered: Sequence[tuple[_Session, list[dict[str, Any]], Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Seconds lost to a stop, measured at this circuit when the weekend contains one.

    A stop shows up as the lap (or pair of laps) two consecutive stints share: the recorder
    closes a stint on the lap the car came in and opens the next on the lap it went out,
    which is the same lap for a stop taken cleanly. The loss is that lap's excess over the
    driver's own clean-lap median in the same session — their own pace, so it nets out the
    car, the fuel load and the circuit in one subtraction.
    """
    stops: list[dict[str, Any]] = []
    for session, stints, lap_rows in tiered:
        if session.tier != RACE_TIER or len(stints) < 2:
            continue
        times = {
            _int_or_none(row.get("lap_number")): _int_or_none(row.get("lap_time_ms"))
            for row in lap_rows
        }
        clean = [
            entry["lap_time_ms"]
            for stint in stints
            for entry in stint["laps"]
            if not entry["excluded"] and entry["lap_time_ms"]
        ]
        if len(clean) < 2:
            continue
        median = float(np.median(clean))
        for position in range(len(stints) - 1):
            affected = {
                _int_or_none(stints[position].get("lap_end")),
                _int_or_none(stints[position + 1].get("lap_start")),
            }
            excess = 0.0
            for lap in sorted(lap for lap in affected if lap is not None):
                lap_time = times.get(lap)
                if lap_time:
                    excess += max(0.0, lap_time - median)
            if excess > 0:
                stops.append(
                    {
                        "session_id": session.id,
                        "stop_after_stint": position + 1,
                        "laps": sorted(lap for lap in affected if lap is not None),
                        "loss_s": round(excess / 1000.0, 2),
                    }
                )

    if not stops:
        return {
            "seconds": DEFAULT_PIT_LOSS_S,
            "source": "default",
            "stops_measured": 0,
            "stops": [],
            "detail": (
                "no pit stop was recorded at this circuit this weekend, so a calendar-median "
                f"{DEFAULT_PIT_LOSS_S:.0f} s is assumed. Every plan's stop count depends on "
                "this number; drive a race with a stop in it and the sheet will measure it."
            ),
        }

    losses = sorted(stop["loss_s"] for stop in stops)
    return {
        "seconds": round(float(np.median(losses)), 2),
        "source": "measured",
        "stops_measured": len(stops),
        "stops": stops,
        "detail": (
            f"median of {len(stops)} stop(s) measured at this circuit, each as the pit lap's "
            "excess over the driver's own clean-lap median in the same race"
        ),
    }


# ---- plan enumeration -------------------------------------------------------------------


def _allocate(models: Sequence[_Model], race_laps: int) -> list[int] | None:
    """Split `race_laps` between the plan's stints for the least projected time.

    Each stint's cost in laps is `L*base + deg*L(L-1)/2`, which is convex in `L` for a
    non-negative slope, and the sum of convex functions under a fixed total is minimised by
    handing out laps one at a time to whichever stint's *next* lap is cheapest. That greedy
    step is exact here, not an approximation — and it is deterministic, because ties go to
    the earlier stint.
    """
    caps = [model.max_stint_laps or 0 for model in models]
    if race_laps < len(models) or sum(caps) < race_laps:
        return None
    laps = [1] * len(models)
    for _ in range(race_laps - len(models)):
        best_index = -1
        best_cost = math.inf
        for index, model in enumerate(models):
            if laps[index] >= caps[index]:
                continue
            cost = model.lap_ms(laps[index])
            if cost < best_cost:
                best_index, best_cost = index, cost
        if best_index < 0:  # pragma: no cover - guarded by the sum(caps) check above
            return None
        laps[best_index] += 1
    return laps


def _pit_windows(
    models: Sequence[_Model], laps: Sequence[int], race_laps: int
) -> list[dict[str, Any]]:
    """Earliest and latest lap each stop can be taken on and still finish the race.

    A stop after stint k can happen on any cumulative lap count that leaves the stints
    before it at least one lap each and within their wear caps, and leaves the stints after
    it enough cap to cover what remains. Both bounds are exact feasibility, not heuristics.
    """
    caps = [model.max_stint_laps or 0 for model in models]
    windows: list[dict[str, Any]] = []
    for stop in range(1, len(models)):
        remaining_cap = sum(caps[stop:])
        earliest = max(stop, race_laps - remaining_cap)
        latest = min(sum(caps[:stop]), race_laps - (len(models) - stop))
        planned = sum(laps[:stop])
        windows.append(
            {
                "stop": stop,
                "planned_lap": planned,
                "earliest_lap": earliest,
                "latest_lap": latest,
                "window_laps": max(0, latest - earliest),
            }
        )
    return windows


def _safety_car_note(windows: Sequence[Mapping[str, Any]], race_laps: int) -> dict[str, Any]:
    """How much a safety car could be used, in one word and one sentence."""
    if not windows:
        return {
            "flexibility": "none",
            "note": (
                "no stop left to take: a safety car costs the field less time than it costs "
                "you, and there is nothing to trade it for."
            ),
        }
    widest = max(windows, key=lambda w: (w["window_laps"], -w["stop"]))
    last_stop = windows[-1]["planned_lap"]
    laps_after = race_laps - last_stop
    if widest["window_laps"] >= SC_FLEXIBLE_WINDOW_LAPS:
        return {
            "flexibility": "flexible",
            "note": (
                f"stop {widest['stop']} can be taken anywhere from lap {widest['earliest_lap']} "
                f"to lap {widest['latest_lap']}, so a safety car inside that window is close "
                "to a free stop. Last stop is planned for lap "
                f"{last_stop}, leaving {laps_after} laps to run."
            ),
        }
    return {
        "flexibility": "tight",
        "note": (
            f"every stop has a window of {widest['window_laps']} lap(s) or less — the wear "
            "ceiling fixes when you box, so a safety car outside those laps cannot be used "
            f"without going past the cliff. Last stop is planned for lap {last_stop}."
        ),
    }


def _plan(
    compounds: Sequence[int],
    models: Mapping[int, _Model],
    race_laps: int,
    pit_loss_ms: float,
) -> dict[str, Any] | None:
    chain = [models[c] for c in compounds]
    laps = _allocate(chain, race_laps)
    if laps is None:
        return None

    stints: list[dict[str, Any]] = []
    total_ms = pit_loss_ms * (len(chain) - 1)
    lap_cursor = 1
    for model, length in zip(chain, laps, strict=True):
        total_ms += model.stint_ms(length)
        wear = None if model.wear_pct_per_lap is None else model.wear_pct_per_lap * length
        stints.append(
            {
                "compound_visual": model.compound_visual,
                "name": compound_name(model.compound_visual),
                "lap_start": lap_cursor,
                "lap_end": lap_cursor + length - 1,
                "laps": length,
                "projected_end_wear_pct": None if wear is None else round(wear, 1),
            }
        )
        lap_cursor += length

    windows = _pit_windows(chain, laps, race_laps)
    return {
        "stops": len(chain) - 1,
        "compounds": list(compounds),
        "label": " → ".join(compound_name(c) for c in compounds),
        "stints": stints,
        "total_time_ms": round(total_ms, 1),
        "pit_windows": windows,
        "safety_car": _safety_car_note(windows, race_laps),
    }


def _enumerate_plans(
    models: Mapping[int, _Model], race_laps: int, pit_loss_s: float
) -> tuple[list[dict[str, Any]], int]:
    """Every legal 1- and 2-stop plan over the compounds that have a model, ranked."""
    plannable = sorted(c for c, model in models.items() if model.plannable and is_dry_compound(c))
    pit_loss_ms = pit_loss_s * 1000.0
    plans: list[dict[str, Any]] = []
    for stops in range(1, MAX_STOPS + 1):
        for combo in product(plannable, repeat=stops + 1):
            # FIA two-compound rule: a dry race must be finished on at least two different
            # visual compounds. Enumerating the all-one-compound orderings and dropping them
            # here (rather than never generating them) keeps `plans_considered` honest.
            if len(set(combo)) < 2:
                continue
            plan = _plan(combo, models, race_laps, pit_loss_ms)
            if plan is not None:
                plans.append(plan)

    plans.sort(key=lambda p: (p["total_time_ms"], p["stops"], p["compounds"]))
    best = plans[0]["total_time_ms"] if plans else 0.0
    for rank, plan in enumerate(plans, start=1):
        plan["rank"] = rank
        plan["delta_to_best_ms"] = round(plan["total_time_ms"] - best, 1)
    return plans[:MAX_PLANS], len(plans)


# ---- the payload ------------------------------------------------------------------------


def build_strategy(
    track_id: int,
    sessions: Iterable[Mapping[str, Any]],
    stint_rows: Mapping[int, Sequence[Mapping[str, Any]]],
    lap_rows: Mapping[int, Sequence[Mapping[str, Any]]],
    wear_rows: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    race_laps: int,
    race_laps_source: str,
    track_name: str | None = None,
) -> dict[str, Any]:
    """Build the `GET /api/analysis/strategy` payload.

    Args:
        track_id: the circuit every session below was recorded at.
        sessions: session summary rows (`id`, `session_type`, `session_type_name`,
            `total_laps`, `started_at_wall`, `player_car_index`), any order.
        stint_rows: session id -> that session's `tyre_stints` rows, player car only.
        lap_rows: session id -> that session's `laps` rows, newest generation, player only.
        wear_rows: session id -> per-lap worst-wheel wear summary rows
            (`lap_number`, `max_wear_pct`). May be empty; the wear rate then falls back to
            the stints' recorded end wear.
        race_laps: the distance to plan for.
        race_laps_source: how `race_laps` was arrived at, echoed into the payload.
        track_name: display name, when the caller has one.

    Returns a JSON-serialisable dict. Deterministic for a given input.
    """
    rows = list(sessions)
    ordered = sorted(
        (_session_of(row) for row in rows),
        key=lambda s: (s.started_at_wall if s.started_at_wall is not None else 0.0, s.id),
    )
    #: Which car is ours, per session. It varies between sessions of one weekend and it is
    #: never safe to assume car 0; the route resolves it from `participants.is_player`.
    players = {int(row["id"]): _int_or_none(row.get("player_car_index")) for row in rows}

    tiered: list[tuple[_Session, list[dict[str, Any]], Sequence[Mapping[str, Any]]]] = []
    by_compound: dict[int, list[_Observation]] = {}
    contributions: dict[int, set[str]] = {}

    for session in ordered:
        if session.tier is None:
            # Time trial, and the type-0 stub a session gets when its Session packet never
            # landed. Neither is running a compound can be learned from.
            continue
        laps = lap_rows.get(session.id, ())
        stints, observations = _observe_session(
            session,
            stint_rows.get(session.id, ()),
            list(laps),
            wear_rows.get(session.id, ()),
            car_index=players.get(session.id),
        )
        tiered.append((session, stints, list(laps)))
        for observation in observations:
            by_compound.setdefault(observation.compound_visual, []).append(observation)

    # Compounds always listed, plus anything else the weekend actually ran (inters, wets).
    listed = sorted(set(PLANNED_DRY_COMPOUNDS) | set(by_compound))
    entries: list[dict[str, Any]] = []
    models: dict[int, _Model] = {}
    for compound in listed:
        observations = by_compound.get(compound, [])
        entry, model = _compound_entry(compound, observations, race_laps)
        entries.append(entry)
        models[compound] = model
        for source in ("pace", "wear"):
            block = entry.get(source)
            if isinstance(block, dict) and block.get("session_id") is not None:
                contributions.setdefault(int(block["session_id"]), set()).add(
                    f"{compound_name(compound).lower()} {source}"
                )

    pit_loss = _pit_loss(tiered)
    for stop in pit_loss["stops"]:
        contributions.setdefault(int(stop["session_id"]), set()).add("pit-lane loss")

    burn, fuel_omission = _fuel_burn(tiered)
    for session_id in (burn or {}).get("session_ids", []):
        contributions.setdefault(int(session_id), set()).add("fuel burn")

    plans, considered = _enumerate_plans(models, race_laps, pit_loss["seconds"])

    omitted: dict[str, str] = {}
    if fuel_omission is not None:
        omitted["fuel"] = fuel_omission
    if not plans:
        omitted["plans"] = _why_no_plans(entries, race_laps)

    return {
        "track_id": int(track_id),
        "track_name": track_name,
        "race_laps": int(race_laps),
        "race_laps_source": race_laps_source,
        "wear_cliff_pct": WEAR_CLIFF_PCT,
        "compounds": entries,
        "plans": plans,
        "plans_considered": considered,
        "fuel": _fuel_recommendation(burn, race_laps),
        "pit_loss_s": pit_loss["seconds"],
        "pit_loss": pit_loss,
        "sessions_used": [
            {
                "id": session.id,
                "session_type": session.session_type,
                "session_type_name": session.session_type_name,
                "evidence": session.tier,
                "started_at_wall": session.started_at_wall,
                "contributed": sorted(contributions.get(session.id, ())),
            }
            for session, _, _ in tiered
        ],
        "omitted": omitted,
    }


def _why_no_plans(entries: Sequence[Mapping[str, Any]], race_laps: int) -> str:
    plannable = [entry for entry in entries if entry["plannable"]]
    if len(plannable) < 2:
        names = ", ".join(entry["name"] for entry in plannable) or "none"
        return (
            f"no legal plan: the two-compound rule needs two dry compounds with a model and "
            f"this weekend has {len(plannable)} ({names}). Run longer stints on the untested "
            "compounds — a degradation line needs four clean laps on the set and a wear rate "
            "needs two."
        )
    # The furthest a legal plan could reach: the longest-lived compound in every stint but
    # one, plus the next-longest once, which is the cheapest way to satisfy the
    # two-compound rule.
    caps = sorted((entry["max_stint_laps"] or 0 for entry in plannable), reverse=True)
    reach = caps[0] * MAX_STOPS + caps[1]
    return (
        f"no legal plan: {race_laps} laps cannot be covered within the "
        f"{WEAR_CLIFF_PCT:.0f}% wear ceiling in {MAX_STOPS} stops or fewer — the longest "
        f"legal {MAX_STOPS + 1}-stint plan reaches {reach} laps."
    )

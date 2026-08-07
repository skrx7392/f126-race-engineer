"""Tyre stints and their degradation slope.

A stint is a run on one set of tyres. The game tells us where they are — the state layer
writes a `tyre_stints` row per set — but a capture that joined a session late, or a replay
of a raw log recorded before stint tracking existed, has laps and no stints, so this module
also reconstructs stints from the lap table when it has to.

The interesting part is the *fit*: how much slower each lap gets as the tyres go off. That
is a least-squares line through the stint's lap times against tyre age, which is only
meaningful once the laps that are not really racing laps have been taken out — out-laps,
in-laps, invalid laps, and laps ruined by traffic or a mistake. Those are flagged rather
than hidden (`docs/analysis-api.md`: "flagged, not hidden"), so the frontend can grey them
in the scatter instead of leaving unexplained gaps.

Nothing here touches the database; the route hands in rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# ---- tuning constants -----------------------------------------------------------------

OUTLIER_FACTOR = 1.07
#: A lap slower than 107% of the stint median did not measure tyre degradation, it measured
#: traffic, a lock-up, a lift for a yellow flag or a moment off-line. 107% is the same
#: threshold F1 itself uses to define a lap as unrepresentative, and on a 92 s lap it allows
#: 6.4 s of scatter — wider than any degradation curve, narrow enough to catch real ruin.

MIN_FIT_LAPS = 4
#: Fewest usable laps that may produce a fit. Two points define a line exactly and tell you
#: nothing; three make an r² that is mostly luck. Four is the smallest number at which the
#: residuals mean something, and it is the point below which a degradation number would be
#: more misleading on the dashboard than an honest `null`.

PERFECT_FIT_RESIDUAL_MS = 0.5
#: A stint where every lap took the same time makes r² a 0/0: there is no variance for the
#: line to explain. Sub-millisecond RMS residual on a 90-second lap is a perfect fit by any
#: standard a dashboard cares about, so that degenerate case reports 1.0 rather than the
#: meaningless 0.0 that floating-point noise would otherwise produce.

WEAR_ORDER: tuple[str, ...] = ("rl", "rr", "fl", "fr")
#: Wheel order for `wear_end_pct`, matching `types.WheelSet` and the game's own wheel arrays
#: so the frontend can index it the same way everywhere.

# ---- exclusion reasons ----------------------------------------------------------------

NO_TIME = "no_time"
INVALID = "invalid"
OUT_LAP = "out_lap"
IN_LAP = "in_lap"
OUTLIER = "outlier"


@dataclass(slots=True, frozen=True)
class StintFit:
    """Least-squares degradation line through a stint's usable laps."""

    deg_ms_per_lap: float
    base_ms: float
    r2: float
    n_used: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "deg_ms_per_lap": round(self.deg_ms_per_lap, 3),
            "base_ms": round(self.base_ms, 1),
            "r2": round(self.r2, 4),
            "n_used": self.n_used,
        }


def fit_degradation(tyre_age: Sequence[float], lap_time_ms: Sequence[float]) -> StintFit | None:
    """Least-squares line of lap time against tyre age, or None if it would not mean anything.

    `base_ms` is the fitted lap time at age zero — the pace the set had when it went on —
    and `deg_ms_per_lap` is how much each lap costs after that.
    """
    x = np.asarray(tyre_age, dtype=np.float64)
    y = np.asarray(lap_time_ms, dtype=np.float64)
    if x.size < MIN_FIT_LAPS or np.unique(x).size < 2:
        return None
    try:
        slope, intercept = (float(v) for v in np.polyfit(x, y, 1))
    except np.linalg.LinAlgError, ValueError:
        return None

    residual = float(((y - (slope * x + intercept)) ** 2).sum())
    total = float(((y - y.mean()) ** 2).sum())
    if total > 0:
        r2 = 1.0 - residual / total
    else:
        r2 = float((residual / x.size) ** 0.5 < PERFECT_FIT_RESIDUAL_MS)
    return StintFit(deg_ms_per_lap=slope, base_ms=intercept, r2=r2, n_used=int(x.size))


def _wear_list(raw: Any) -> list[float | None] | None:
    """`wear_at_end_json` as RL/RR/FL/FR percentages, whichever shape the writer stored.

    Three shapes are in the wild and all of them mean the same four numbers in the same
    wheel order: the bare array the state layer writes today (`{"tyre_wear_pct": [...]}`),
    a flat `{"rl": ..., "rr": ...}` mapping, and a plain list. Guessing wrong here is not a
    crash, it is four silent nulls on the dashboard, so all three are handled explicitly.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if any(wheel in raw for wheel in WEAR_ORDER):
            raw = [raw.get(wheel) for wheel in WEAR_ORDER]
        else:
            raw = next((v for v in raw.values() if isinstance(v, list | tuple)), None)
    if not isinstance(raw, list | tuple):
        return None
    out: list[float | None] = []
    for value in raw[:4]:
        try:
            out.append(None if value is None else round(float(value), 2))
        except TypeError, ValueError:
            out.append(None)
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except TypeError, ValueError:
        return None


def derive_stints(lap_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct stints from the lap table when `tyre_stints` has nothing to say.

    A new set is declared when the compound changes, or when tyre age drops — a pit stop
    onto the same compound changes no compound field but always resets age. Ordered by lap
    so the result is deterministic.
    """
    laps = sorted(
        (row for row in lap_rows if row.get("lap_number") is not None),
        key=lambda row: int(row["lap_number"]),
    )
    stints: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in laps:
        age = _int_or_none(row.get("tyre_age_laps"))
        previous_age = None if previous is None else _int_or_none(previous.get("tyre_age_laps"))
        changed = previous is None or any(
            row.get(field) != previous.get(field)
            for field in ("compound_actual", "compound_visual")
        )
        reset = age is not None and previous_age is not None and age < previous_age
        if changed or reset:
            stints.append(
                {
                    "stint_no": len(stints) + 1,
                    "car_index": _int_or_none(row.get("car_index")),
                    "compound_actual": _int_or_none(row.get("compound_actual")),
                    "compound_visual": _int_or_none(row.get("compound_visual")),
                    "lap_start": int(row["lap_number"]),
                    "lap_end": int(row["lap_number"]),
                    "wear_at_end_json": None,
                    "derived": True,
                }
            )
        else:
            stints[-1]["lap_end"] = int(row["lap_number"])
        previous = row
    return stints


def pit_flags(position: int, count: int) -> tuple[bool, bool]:
    """`(after_pit, before_pit)` for the stint at `position` of `count` in one car's run.

    A stint that is not the car's first began with a pit stop, so its first lap is an
    out-lap; one that is not the car's last ended with a pit stop, so its last lap is an
    in-lap. Both are real laps of the stint and both are useless for measuring how the set
    behaved — the tyre spent part of them in the pit lane.

    A function rather than two inline comparisons because `strategy.py` needs the identical
    rule to decide which laps of a stint a wear reading may be taken over, and the two
    modules disagreeing about where a stint's usable running starts would put the pit lap's
    wear into a per-lap rate.
    """
    return position > 0, position < count - 1


def _classify_laps(
    laps: list[Mapping[str, Any]],
    *,
    lap_start: int,
    lap_end: int,
    after_pit: bool,
    before_pit: bool,
) -> list[dict[str, Any]]:
    """Flag every lap of a stint with why (or whether) it is excluded from the fit."""
    entries: list[dict[str, Any]] = []
    for row in laps:
        lap_number = int(row["lap_number"])
        lap_time = _int_or_none(row.get("lap_time_ms"))
        valid = row.get("valid")
        reason: str | None = None
        if lap_time is None or lap_time <= 0:
            reason = NO_TIME
        elif after_pit and lap_number == lap_start:
            reason = OUT_LAP
        elif before_pit and lap_number == lap_end:
            reason = IN_LAP
        elif valid is False:
            reason = INVALID
        entries.append(
            {
                "lap_number": lap_number,
                "lap_time_ms": lap_time,
                "valid": None if valid is None else bool(valid),
                "excluded": reason is not None,
                "exclude_reason": reason,
            }
        )

    # The median is taken over the laps that survived the structural filters, so a stint
    # whose out-lap is 20 s slow does not drag its own outlier threshold out with it.
    times = [e["lap_time_ms"] for e in entries if not e["excluded"]]
    if times:
        cutoff = float(np.median(times)) * OUTLIER_FACTOR
        for entry in entries:
            if not entry["excluded"] and float(entry["lap_time_ms"]) > cutoff:
                entry["excluded"] = True
                entry["exclude_reason"] = OUTLIER
    return entries


def build_stints(
    session_id: int,
    stint_rows: Iterable[Mapping[str, Any]],
    lap_rows: Iterable[Mapping[str, Any]],
    *,
    car_index: int | None = None,
) -> dict[str, Any]:
    """Build the `GET /api/analysis/stints` payload.

    Args:
        session_id: echoed back in the payload.
        stint_rows: `tyre_stints` rows; empty falls back to reconstruction from `lap_rows`.
        lap_rows: the session's laps, newest generation each (`queries.laps_for_session`).
        car_index: restrict to one car; None keeps every car the rows mention.
    """
    laps = [row for row in lap_rows if row.get("lap_number") is not None]
    if car_index is not None:
        laps = [row for row in laps if _int_or_none(row.get("car_index")) == car_index]

    stints = [
        dict(row) | {"derived": False}
        for row in stint_rows
        if car_index is None or _int_or_none(row.get("car_index")) == car_index
    ]
    if not stints:
        stints = derive_stints(laps)

    by_car: dict[int | None, list[dict[str, Any]]] = {}
    for stint in stints:
        by_car.setdefault(_int_or_none(stint.get("car_index")), []).append(stint)
    for group in by_car.values():
        group.sort(key=lambda s: (_int_or_none(s.get("stint_no")) or 0, s.get("lap_start") or 0))

    payload: list[dict[str, Any]] = []
    for car, group in sorted(by_car.items(), key=lambda item: (item[0] is None, item[0])):
        car_laps = [row for row in laps if _int_or_none(row.get("car_index")) == car]
        for position, stint in enumerate(group):
            after_pit, before_pit = pit_flags(position, len(group))
            payload.append(
                _one_stint(stint, car_laps, after_pit=after_pit, before_pit=before_pit)
            )
    return {"session_id": int(session_id), "stints": payload}


def _one_stint(
    stint: Mapping[str, Any],
    car_laps: list[Mapping[str, Any]],
    *,
    after_pit: bool,
    before_pit: bool,
) -> dict[str, Any]:
    lap_start = _int_or_none(stint.get("lap_start"))
    lap_end = _int_or_none(stint.get("lap_end"))
    lo = lap_start if lap_start is not None else -(2**31)
    hi = lap_end if lap_end is not None else 2**31
    mine = sorted(
        (row for row in car_laps if lo <= int(row["lap_number"]) <= hi),
        key=lambda row: int(row["lap_number"]),
    )
    entries = _classify_laps(
        mine,
        lap_start=lo,
        lap_end=hi,
        after_pit=after_pit,
        before_pit=before_pit,
    )

    usable = [entry for entry in entries if not entry["excluded"]]
    base_lap = lap_start if lap_start is not None else (mine[0]["lap_number"] if mine else 0)
    fit = fit_degradation(
        [float(entry["lap_number"] - base_lap) for entry in usable],
        [float(entry["lap_time_ms"]) for entry in usable],
    )

    return {
        "stint_no": _int_or_none(stint.get("stint_no")),
        "car_index": _int_or_none(stint.get("car_index")),
        "compound_actual": _int_or_none(stint.get("compound_actual")),
        "compound_visual": _int_or_none(stint.get("compound_visual")),
        "lap_start": lap_start,
        "lap_end": lap_end,
        "derived": bool(stint.get("derived", False)),
        "laps": entries,
        "fit": fit.as_dict() if fit is not None else None,
        "wear_end_pct": _wear_list(stint.get("wear_at_end_json")),
    }

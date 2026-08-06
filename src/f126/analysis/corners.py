"""Corner segmentation and per-corner time loss.

A corner, for our purposes, is a *sustained speed minimum*: the driver slowed down, turned,
and accelerated again. That definition needs no track map, no GPS reference and no
per-circuit configuration — it falls out of the speed-versus-distance curve every lap
already carries — which matters because the game gives us no corner list.

The pipeline, all of it deterministic for a given lap:

1. resample speed onto the uniform distance grid (`resample.py`);
2. smooth it with a Savitzky-Golay filter, which flattens throttle ripple while leaving the
   depth and position of a real minimum where they were;
3. find minima with `scipy.signal.find_peaks` on the negated curve, gated on prominence
   (how deep the dip is) and spacing (how far apart two apexes must be);
4. walk out from each apex to where speed has recovered most of the way back to the local
   shoulder maxima — those are the corner's entry and exit;
5. read the braking point backwards from the apex as the start of the last sustained brake
   application, and the per-corner time loss as the rise in the cumulative delta between
   entry and exit.

Everything the segmentation depends on is a named constant in the block below, each with
the reason it holds the value it does. There are no other magic numbers in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.signal import find_peaks, savgol_filter

from f126.analysis.compare import LapRef, require_same_track
from f126.analysis.resample import (
    GRID_STEP_M,
    cumulative_delta_ms,
    make_grid,
    maybe_float,
    maybe_int,
    resample_onto,
)

if TYPE_CHECKING:
    from f126.analysis.resample import BoolArray, FloatArray

# ---- tuning constants ---------------------------------------------------------------
# The whole segmentation is defined by these ten numbers. Every one is in physical units
# (metres, km/h, pedal fraction) rather than samples, so they keep their meaning if the
# grid pitch ever changes.

SMOOTH_WINDOW_M = 45.0
#: Savitzky-Golay window length, in metres of track. Raw 20 Hz speed carries a few km/h of
#: throttle-modulation and kerb ripple. 45 m is long enough to erase that ripple, and short
#: enough that a real slow section — a hairpin holds minimum speed for ~60-80 m — is not
#: smeared into the straights on either side of it.

SMOOTH_POLYORDER = 2
#: A speed minimum is locally parabolic, and order 2 is the lowest order that can represent
#: one: it preserves both the depth and the position of the apex. Order 1 flattens the
#: minimum and drags the apex toward the longer shoulder; order 3+ starts re-fitting the
#: ripple the filter is there to remove.

MIN_PROMINENCE_KMH = 8.0
#: How far speed must drop below its surrounding shoulders before a dip counts as a corner.
#: Prominence, not depth: it measures the dip against the local maxima either side, so it
#: does not care whether the corner sits at 80 or 250 km/h. Every real corner on the
#: calendar costs far more than 8 km/h, so this threshold buys rejection of lifts,
#: kerb strikes and slipstream wobble at no cost in true corners.

MIN_CORNER_SPACING_M = 80.0
#: Minimum distance between two apexes. Closer than this and the two minima are one
#: complex — an S, a double-apex, a chicane's two halves — sharing a single braking event;
#: reporting them as separate rows would split that event's time loss across two lines and
#: hand the frontend two "braking points" for one brake application.

SHOULDER_RECOVERY = 0.90
#: Entry and exit are the points where speed has recovered this fraction of the way from
#: the apex back to the local shoulder maximum. Chasing 100% would chase the exact
#: straight-line peak, which is noisy and drifts with slipstream and fuel load; 0.90 is
#: stable lap to lap while still containing the whole slow section of the corner.

BRAKE_ON = 0.10
#: Brake input above which the pedal counts as applied. The brake axis of a pad or a wheel
#: rests at a small non-zero value and picks up noise, so 0 is not a usable threshold, and
#: anything above ~0.15 would miss the light trail-brake releases.

BRAKE_SUSTAIN_M = 15.0
#: How far a brake application must persist to be a braking point. Rejects the momentary
#: dabs and downshift blips that dot a straight without being the corner's braking event.

BRAKE_SEARCH_M = 400.0
#: How far back from an apex to look for its braking point. Longer than any real braking
#: zone (Bahrain's turn 1, one of the longest, is ~150 m) and short enough that a corner
#: never claims the previous corner's braking. The search is additionally floored at the
#: previous corner's exit, so this only binds on the first corner of a lap.

SLOW_MAX_KMH = 120.0
MEDIUM_MAX_KMH = 200.0
#: Corner class by minimum speed. Under 120 km/h is a 1st/2nd-gear corner, 120-200 km/h is
#: a 3rd/4th-gear medium, above 200 km/h is a lift-or-flat fast corner. These are the bands
#: a driver already thinks in, and they are what the frontend colours the table by.

MAX_CORNERS = 40
#: Sanity cap on the number of corners returned. The busiest circuit on the calendar has 20;
#: more than 40 means the prominence gate failed against pathological data, and an
#: unauthenticated endpoint should truncate rather than emit an unbounded list.

# ---- segmentation ---------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CornerBounds:
    """Index triple into the shared grid: where a corner starts, bottoms out and ends."""

    entry: int
    apex: int
    exit: int


def smooth_speed(speed: FloatArray, *, step_m: float = GRID_STEP_M) -> FloatArray:
    """Savitzky-Golay smoothing of speed-vs-distance, with the window given in metres.

    Falls back to the unsmoothed curve when the lap is too short to hold a window — a
    200 m fragment has no corners worth finding anyway, and a filter longer than its input
    is an error rather than a smoother.
    """
    window = int(round(SMOOTH_WINDOW_M / step_m)) | 1  # odd, as savgol wants
    window = max(window, (SMOOTH_POLYORDER + 1) | 1)  # and long enough to fit the polynomial
    if window > speed.size:
        window = speed.size if speed.size % 2 else speed.size - 1
    if window <= SMOOTH_POLYORDER:
        return speed.astype(np.float64, copy=True)
    return np.asarray(savgol_filter(speed, window, SMOOTH_POLYORDER), dtype=np.float64)


def find_apexes(smoothed: FloatArray, *, step_m: float = GRID_STEP_M) -> list[int]:
    """Grid indices of the speed minima that qualify as corners."""
    spacing = max(1, int(round(MIN_CORNER_SPACING_M / step_m)))
    peaks, _properties = find_peaks(-smoothed, prominence=MIN_PROMINENCE_KMH, distance=spacing)
    return [int(i) for i in peaks[:MAX_CORNERS]]


def _recovery_index(
    smoothed: FloatArray, apex: int, floor: int, ceiling: int, *, forwards: bool
) -> int:
    """Walk out from `apex` to where speed has recovered to the local shoulder threshold.

    `floor`/`ceiling` bound the search to this corner's half of the gap to its neighbours,
    so a corner never annexes the one before or after it.
    """
    lo, hi = (apex, ceiling) if forwards else (floor, apex)
    window = smoothed[lo : hi + 1]
    if window.size == 0:
        return apex
    apex_speed = float(smoothed[apex])
    shoulder = float(window.max())
    threshold = apex_speed + SHOULDER_RECOVERY * (shoulder - apex_speed)
    reached = np.flatnonzero(window >= threshold)
    if reached.size == 0:
        return apex
    # Forwards: the first recovery after the apex. Backwards: the last one before it.
    return int(lo + (reached[0] if forwards else reached[-1]))


def corner_bounds(smoothed: FloatArray, apexes: list[int]) -> list[CornerBounds]:
    """Entry/apex/exit index triples, one per apex, never overlapping."""
    bounds: list[CornerBounds] = []
    last = smoothed.size - 1
    for n, apex in enumerate(apexes):
        floor = 0 if n == 0 else (apexes[n - 1] + apex) // 2
        ceiling = last if n == len(apexes) - 1 else (apex + apexes[n + 1]) // 2
        entry = _recovery_index(smoothed, apex, floor, ceiling, forwards=False)
        exit_ = _recovery_index(smoothed, apex, floor, ceiling, forwards=True)
        bounds.append(CornerBounds(entry=entry, apex=apex, exit=exit_))
    return bounds


def _sustained_runs(flags: BoolArray, minimum: int) -> list[tuple[int, int]]:
    """Half-open `[start, end)` spans where `flags` is True for at least `minimum` samples."""
    padded = np.concatenate(([False], flags, [False]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return [(int(s), int(e)) for s, e in zip(starts, ends, strict=True) if e - s >= minimum]


def brake_point(
    grid: FloatArray,
    brake: FloatArray,
    apex: int,
    floor: int,
    *,
    step_m: float = GRID_STEP_M,
) -> float | None:
    """Distance at which braking for this corner began, or None if the driver never braked.

    The *last* sustained application before the apex, not the first: a dab mid-straight is
    not the braking point for the corner that follows it, but the pedal press that runs
    into the apex always is.
    """
    start = max(floor, apex - int(BRAKE_SEARCH_M / step_m))
    if apex <= start:
        return None
    window = np.asarray(brake[start : apex + 1] > BRAKE_ON, dtype=np.bool_)
    runs = _sustained_runs(window, max(1, int(round(BRAKE_SUSTAIN_M / step_m))))
    if not runs:
        return None
    return float(grid[start + runs[-1][0]])


def classify(min_speed_kmh: float) -> str:
    """`slow` | `medium` | `fast` from a corner's minimum speed."""
    if min_speed_kmh < SLOW_MAX_KMH:
        return "slow"
    if min_speed_kmh < MEDIUM_MAX_KMH:
        return "medium"
    return "fast"


def _window_min(values: FloatArray, lo: int, hi: int) -> float:
    window = values[lo : hi + 1]
    finite = window[np.isfinite(window)]
    return float(finite.min()) if finite.size else float("nan")


def analyse_corners(
    subject: LapRef, reference: LapRef | None, *, step_m: float = GRID_STEP_M
) -> dict[str, Any]:
    """Build the `GET /api/analysis/corners` payload.

    Segmentation runs on the *subject* lap over its own distance coverage, so a partial
    reference lap costs reference columns rather than corners. The reference is resampled
    onto the same grid and is NaN outside its own coverage; every field derived from it is
    `null` there, which is the documented meaning of `null` in this API.

    Raises:
        AnalysisError: 422 if the reference lap is from a different track, or the subject
            lap covers too little distance to build a grid.
    """
    if reference is not None:
        require_same_track(subject, reference)

    grid = make_grid(subject.trace.start_m, subject.trace.end_m, step_m=step_m)
    lap = resample_onto(subject.trace, grid)
    speed, brake = lap.channel("speed_kmh"), lap.channel("brake")
    smoothed = smooth_speed(speed, step_m=step_m)
    bounds = corner_bounds(smoothed, find_apexes(smoothed, step_m=step_m))

    if reference is None:
        delta = np.full(grid.shape, np.nan, dtype=np.float64)
        ref_lap = None
    else:
        ref_lap = resample_onto(reference.trace, grid)
        delta = cumulative_delta_ms(lap.session_time_s, ref_lap.session_time_s)

    corners: list[dict[str, Any]] = []
    accounted = 0.0
    for n, bound in enumerate(bounds):
        floor = 0 if n == 0 else bounds[n - 1].exit
        min_speed = _window_min(speed, bound.entry, bound.exit)
        loss = float(delta[bound.exit] - delta[bound.entry])
        if np.isfinite(loss):
            accounted += loss
        corners.append(
            {
                "n": n + 1,
                "entry_m": maybe_float(float(grid[bound.entry]), digits=1),
                "apex_m": maybe_float(float(grid[bound.apex]), digits=1),
                "exit_m": maybe_float(float(grid[bound.exit]), digits=1),
                "min_speed_kmh": maybe_float(min_speed, digits=1),
                "ref_min_speed_kmh": (
                    None
                    if ref_lap is None
                    else maybe_float(
                        _window_min(ref_lap.channel("speed_kmh"), bound.entry, bound.exit),
                        digits=1,
                    )
                ),
                "brake_point_m": brake_point(grid, brake, bound.apex, floor, step_m=step_m),
                "ref_brake_point_m": (
                    None
                    if ref_lap is None
                    else brake_point(
                        grid, ref_lap.channel("brake"), bound.apex, floor, step_m=step_m
                    )
                ),
                "time_loss_ms": maybe_int(loss),
                "kind": classify(min_speed) if np.isfinite(min_speed) else None,
            }
        )

    covered = np.flatnonzero(np.isfinite(delta))
    total = float(delta[covered[-1]]) if covered.size else float("nan")
    straights = total - accounted if np.isfinite(total) else float("nan")

    return {
        "session_id": int(subject.session_id),
        "lap_number": int(subject.lap_number),
        "ref_lap_number": None if reference is None else int(reference.lap_number),
        "corners": corners,
        "straights_time_loss_ms": maybe_int(straights),
        "total_delta_ms": maybe_int(total),
    }

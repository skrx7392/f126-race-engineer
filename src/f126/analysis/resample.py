"""Uniform-distance resampling: the common ground every comparison stands on.

Two laps are never sampled in the same places. 20 Hz is a sample every ~1.4 m through a
hairpin and every ~4.4 m at 320 km/h, and the phase drifts lap to lap, so comparing raw
samples index-by-index compares different bits of track. Everything downstream — the delta
trace, corner segmentation, per-corner time loss — is therefore defined on a uniform
distance grid, and the rules for getting onto that grid live here.

Two invariants this module establishes and the rest of the package relies on:

* a `LapTrace` is a *function of distance*: strictly increasing in both distance and
  session time, so `distance -> session_time` can be inverted and interpolated;
* a `ResampledLap` is NaN wherever the grid falls outside the lap's own coverage, so a
  partial lap never silently extrapolates into track it never drove.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

FloatArray = np.ndarray[Any, np.dtype[np.float64]]
BoolArray = np.ndarray[Any, np.dtype[np.bool_]]

GRID_STEP_M = 5.0
#: Grid pitch. 5 m is 60 ms of running at 300 km/h and 225 ms at 80 km/h: fine enough that
#: a braking point lands in the right bin, coarse enough that a full lap is ~1100 points —
#: one series the browser draws at 60 fps without decimation.

MAX_GRID_POINTS = 4096
#: Guard for an unauthenticated endpoint. The longest circuit on the calendar (Spa, 7004 m)
#: needs 1401 points at `GRID_STEP_M`, so a request for more than this means corrupt
#: distances rather than a long lap, and is refused instead of allocating.

MIN_TRACE_SAMPLES = 2
#: Below two usable samples there is nothing to interpolate between.

#: How each channel is filled between two samples. "linear" for quantities that really do
#: vary continuously; "hold" for states where an interpolated value would be a lie — there
#: is no gear 4.5 and no half-open DRS — which take the nearer of the two neighbours.
CHANNEL_KINDS: dict[str, str] = {
    "speed_kmh": "linear",
    "throttle": "linear",
    "brake": "linear",
    "steer": "linear",
    "gear": "hold",
    "rpm": "linear",
    "drs_or_aero": "hold",
}

#: The telemetry_samples columns a trace carries, in the order the API emits them.
TRACE_CHANNELS: tuple[str, ...] = tuple(CHANNEL_KINDS)

DISTANCE_COLUMN = "lap_distance_m"
TIME_COLUMN = "session_time_s"


@dataclass(slots=True)
class AnalysisError(Exception):
    """A domain failure that maps straight onto an HTTP status.

    Lives in this module rather than the package `__init__` so every other analysis module
    can import it without an import cycle; `f126.analysis` re-exports it.

    Only two statuses are ever used, matching the conventions the rest of the web layer
    already follows: 404 for "that lap/session is not in the database" and 422 for "those
    are real rows but they cannot be compared".
    """

    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"{self.status_code}: {self.detail}"


@dataclass(slots=True, frozen=True)
class LapTrace:
    """One lap's samples as parallel float arrays, strictly increasing in distance.

    Build with `trace_from_rows`; the constructor does not re-validate, so nothing else
    should assemble one from unsorted data.
    """

    distance_m: FloatArray
    session_time_s: FloatArray
    channels: dict[str, FloatArray]

    def __len__(self) -> int:
        return int(self.distance_m.size)

    @property
    def start_m(self) -> float:
        return float(self.distance_m[0])

    @property
    def end_m(self) -> float:
        return float(self.distance_m[-1])

    def channel(self, name: str) -> FloatArray:
        return self.channels[name]


@dataclass(slots=True, frozen=True)
class ResampledLap:
    """A lap evaluated on a shared grid. NaN wherever the lap did not cover the grid."""

    grid_m: FloatArray
    session_time_s: FloatArray
    channels: dict[str, FloatArray]

    def __len__(self) -> int:
        return int(self.grid_m.size)

    def channel(self, name: str) -> FloatArray:
        return self.channels[name]

    @property
    def covered(self) -> BoolArray:
        return np.isfinite(self.session_time_s)


def _column(rows: list[Mapping[str, Any]], name: str) -> FloatArray:
    """One column as float64, with SQL NULL and non-numeric junk becoming NaN."""
    out = np.empty(len(rows), dtype=np.float64)
    for i, row in enumerate(rows):
        value = row.get(name)
        try:
            out[i] = np.nan if value is None else float(value)
        except TypeError, ValueError:
            out[i] = np.nan
    return out


def _forward_mask(values: FloatArray) -> BoolArray:
    """Keep a sample only when it strictly exceeds every sample kept before it.

    A single pass that removes duplicates and backwards jumps at once. Backwards jumps are
    real: a flashback rewinds distance, and while the writer bumps `generation` so readers
    can filter, a segment boundary or a mid-lap rejoin can still leave one row out of order.
    """
    if values.size == 0:
        return np.zeros(0, dtype=np.bool_)
    running = np.maximum.accumulate(values)
    mask = np.empty(values.size, dtype=np.bool_)
    mask[0] = True
    mask[1:] = values[1:] > running[:-1]
    return mask


def _fill_gaps(values: FloatArray, reference: FloatArray) -> FloatArray:
    """Linearly bridge NaN holes in a channel against `reference` (the distance axis).

    One dropped field — a null gear in a single row — must not punch a hole through the
    whole resampled channel. All-NaN channels stay all-NaN: there is nothing to bridge.
    """
    finite = np.isfinite(values)
    if finite.all() or not finite.any():
        return values
    filled = values.copy()
    filled[~finite] = np.interp(reference[~finite], reference[finite], values[finite])
    return filled


def trace_from_rows(
    rows: Iterable[Mapping[str, Any]], *, channels: tuple[str, ...] = TRACE_CHANNELS
) -> LapTrace:
    """Turn `telemetry_samples` rows into a clean, strictly-increasing `LapTrace`.

    Rows may arrive in any order and may contain NULLs; what comes out is sorted by
    distance, deduplicated, and monotonic in both distance and session time.

    Raises:
        AnalysisError: 404 when fewer than two usable samples survive cleaning.
    """
    materialised = list(rows)
    if not materialised:
        raise AnalysisError(404, "no telemetry recorded for that lap")

    distance = _column(materialised, DISTANCE_COLUMN)
    time = _column(materialised, TIME_COLUMN)
    usable = np.isfinite(distance) & np.isfinite(time)
    order = np.argsort(distance[usable], kind="stable")
    index = np.flatnonzero(usable)[order]

    distance, time = distance[index], time[index]
    keep = _forward_mask(distance)
    distance, time, index = distance[keep], time[keep], index[keep]
    keep = _forward_mask(time)
    distance, time, index = distance[keep], time[keep], index[keep]

    if distance.size < MIN_TRACE_SAMPLES:
        raise AnalysisError(404, "lap has too few usable telemetry samples")

    picked = [materialised[i] for i in index.tolist()]
    values = {name: _fill_gaps(_column(picked, name), distance) for name in channels}
    return LapTrace(distance_m=distance, session_time_s=time, channels=values)


def make_grid(start_m: float, end_m: float, *, step_m: float = GRID_STEP_M) -> FloatArray:
    """A uniform grid covering `[start_m, end_m]` without ever running past `end_m`.

    Raises:
        AnalysisError: 422 when the range is empty or implausibly wide.
    """
    span = end_m - start_m
    if not np.isfinite(span) or span < step_m:
        raise AnalysisError(422, "laps do not overlap over enough distance to compare")
    points = int(span // step_m) + 1
    if points > MAX_GRID_POINTS:
        raise AnalysisError(422, "lap distance range is too large to resample")
    return start_m + step_m * np.arange(points, dtype=np.float64)


def overlap(*traces: LapTrace, step_m: float = GRID_STEP_M) -> FloatArray:
    """The grid over the distance range every given lap actually drove."""
    start = max(trace.start_m for trace in traces)
    end = min(trace.end_m for trace in traces)
    return make_grid(start, end, step_m=step_m)


def _hold(grid: FloatArray, distance: FloatArray, values: FloatArray) -> FloatArray:
    """Nearest-sample fill for state channels."""
    if distance.size == 1:
        return np.full(grid.shape, values[0], dtype=np.float64)
    right = np.clip(np.searchsorted(distance, grid), 1, distance.size - 1)
    left = right - 1
    nearer_left = (grid - distance[left]) <= (distance[right] - grid)
    return np.where(nearer_left, values[left], values[right])


def resample_onto(trace: LapTrace, grid: FloatArray) -> ResampledLap:
    """Evaluate one lap on `grid`; NaN outside the distance the lap covered."""
    distance = trace.distance_m
    inside = (grid >= distance[0]) & (grid <= distance[-1])
    time = np.where(inside, np.interp(grid, distance, trace.session_time_s), np.nan)
    channels: dict[str, FloatArray] = {}
    for name, values in trace.channels.items():
        if CHANNEL_KINDS.get(name, "linear") == "hold":
            filled = _hold(grid, distance, values)
        else:
            filled = np.interp(grid, distance, values)
        channels[name] = np.where(inside, filled, np.nan)
    return ResampledLap(grid_m=grid, session_time_s=time, channels=channels)


def cumulative_delta_ms(subject: FloatArray, reference: FloatArray) -> FloatArray:
    """Running time delta of `subject` against `reference`, in ms, along a shared grid.

    Each lap's elapsed time is measured from the first grid point *both* laps covered, so
    the curve starts at exactly zero and only ever reflects time gained or lost inside the
    compared window. Positive = the subject is behind; negative = the subject is ahead —
    the sign convention `docs/analysis-api.md` freezes for `delta_ms`.

    NaN wherever either lap is missing, which is how a partial reference lap reports "no
    opinion here" instead of a fabricated gap.
    """
    both = np.isfinite(subject) & np.isfinite(reference)
    out = np.full(subject.shape, np.nan, dtype=np.float64)
    if not both.any():
        return out
    first = int(np.argmax(both))
    out[both] = ((subject[both] - subject[first]) - (reference[both] - reference[first])) * 1000.0
    return out


def json_floats(values: FloatArray, *, digits: int = 3) -> list[float | None]:
    """Array -> JSON list, with every non-finite value becoming `null` ("unknown")."""
    rounded = np.round(values, digits)
    return [None if not np.isfinite(v) else float(v) for v in rounded]


def json_ints(values: FloatArray) -> list[int | None]:
    """Array -> JSON list of ints, with every non-finite value becoming `null`."""
    return [None if not np.isfinite(v) else int(round(float(v))) for v in values]


def maybe_float(value: float, *, digits: int = 3) -> float | None:
    return None if not np.isfinite(value) else float(round(float(value), digits))


def maybe_int(value: float) -> int | None:
    return None if not np.isfinite(value) else int(round(float(value)))

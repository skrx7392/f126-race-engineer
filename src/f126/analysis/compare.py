"""Two laps on one distance grid, plus the cumulative time delta between them.

This is the arithmetic behind `GET /api/analysis/compare` and the substrate the corner
analysis stands on: `corners.py` asks for exactly the same delta curve and reads it at the
corner boundaries it finds.

Nothing here touches the database. The route hands in rows; this builds the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from f126.analysis.resample import (
    GRID_STEP_M,
    AnalysisError,
    LapTrace,
    cumulative_delta_ms,
    json_floats,
    json_ints,
    overlap,
    resample_onto,
)

if TYPE_CHECKING:
    from f126.analysis.resample import FloatArray, ResampledLap

#: Channels the compare payload carries per lap, in `docs/analysis-api.md` order. The trace
#: holds more (steer, rpm, drs_or_aero); the chart overlay only draws these four, and an
#: unauthenticated endpoint should not ship arrays nobody plots.
COMPARE_CHANNELS: tuple[str, ...] = ("speed_kmh", "throttle", "brake", "gear")

#: Channels emitted as integers rather than floats.
INTEGER_CHANNELS: frozenset[str] = frozenset({"gear"})


@dataclass(slots=True, frozen=True)
class LapRef:
    """One lap's identity, timing and samples — everything a comparison needs.

    `track_id` is what decides whether two laps may be compared at all, so it travels with
    the lap rather than being looked up again downstream.
    """

    session_id: int
    lap_number: int
    trace: LapTrace
    car_index: int | None = None
    track_id: int | None = None
    track_name: str | None = None
    lap_time_ms: int | None = None
    sectors_ms: tuple[int | None, int | None, int | None] = (None, None, None)

    def identity(self) -> dict[str, Any]:
        return {
            "session_id": int(self.session_id),
            "lap_number": int(self.lap_number),
            "car_index": None if self.car_index is None else int(self.car_index),
            "lap_time_ms": None if self.lap_time_ms is None else int(self.lap_time_ms),
        }


def require_same_track(a: LapRef, b: LapRef) -> None:
    """Refuse to compare laps of different circuits.

    Strict inequality, so an unknown track (NULL `track_id`, which happens when a capture
    starts mid-session before the session packet lands) only compares against another
    unknown one. Overlaying Monaco on Monza produces a plausible-looking delta trace that
    means nothing at all, and that is worth a hard 422.
    """
    if a.track_id != b.track_id:
        raise AnalysisError(422, "laps are from different tracks")


def _channel_payload(lap: ResampledLap) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in COMPARE_CHANNELS:
        values = lap.channel(name)
        out[name] = json_ints(values) if name in INTEGER_CHANNELS else json_floats(values)
    return out


def sectors_payload(lap: LapRef) -> list[int | None]:
    return [None if value is None else int(value) for value in lap.sectors_ms]


def delta_for(a: LapRef, b: LapRef, grid: FloatArray) -> FloatArray:
    """The cumulative `a`-vs-`b` delta in ms, evaluated on `grid`."""
    return cumulative_delta_ms(
        resample_onto(a.trace, grid).session_time_s,
        resample_onto(b.trace, grid).session_time_s,
    )


def compare_laps(a: LapRef, b: LapRef, *, step_m: float = GRID_STEP_M) -> dict[str, Any]:
    """Build the `GET /api/analysis/compare` payload for two laps.

    The grid spans only the distance *both* laps covered: a comparison against a partial
    out-lap is honest about where it has nothing to say rather than extrapolating.

    Raises:
        AnalysisError: 422 if the tracks differ or the laps barely overlap.
    """
    require_same_track(a, b)
    grid = overlap(a.trace, b.trace, step_m=step_m)
    left, right = resample_onto(a.trace, grid), resample_onto(b.trace, grid)
    delta = cumulative_delta_ms(left.session_time_s, right.session_time_s)

    return {
        "track_id": a.track_id,
        "track_name": a.track_name or b.track_name,
        "points": int(grid.size),
        "step_m": float(step_m),
        "grid_m": json_floats(grid, digits=2),
        "a": a.identity() | _channel_payload(left),
        "b": b.identity() | _channel_payload(right),
        "delta_ms": json_floats(delta, digits=1),
        "sectors_a": sectors_payload(a),
        "sectors_b": sectors_payload(b),
    }

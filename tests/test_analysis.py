"""Tier 1 of the analysis tests: pure functions against synthetic laps.

Everything here runs with no database, no HTTP and no game, because everything in
`f126.analysis` is a function from rows to numbers. That is the whole point of keeping the
package free of I/O: a corner detector you can only test against a real Postgres is a
corner detector you do not test.

The laps are analytic. `synthetic_lap` integrates a speed-versus-distance function into a
20 Hz trace the way the car actually produces one — samples close together in the slow
corners, far apart on the straights — so the resampler and the segmentation are exercised
against known answers rather than against a recorded lap whose truth nobody knows.

Tier 2 (`tests/test_analysis_api.py`) puts the same code in front of real Bahrain data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from f126.analysis import corners as corners_mod
from f126.analysis import resample as resample_mod
from f126.analysis import stints as stints_mod
from f126.analysis import strategy as strategy_mod
from f126.analysis.compare import LapRef, compare_laps
from f126.analysis.corners import analyse_corners, brake_point, find_apexes, smooth_speed
from f126.analysis.resample import (
    GRID_STEP_M,
    AnalysisError,
    cumulative_delta_ms,
    make_grid,
    overlap,
    resample_onto,
    trace_from_rows,
)
from f126.analysis.stints import build_stints, derive_stints, fit_degradation
from f126.analysis.strategy import build_strategy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# --------------------------------------------------------------------------------------------
# Synthetic laps
# --------------------------------------------------------------------------------------------


def synthetic_lap(
    speed_fn: Any,
    length_m: float,
    *,
    hz: float = 20.0,
    brake_fn: Any = None,
    start_time: float = 0.0,
    resolution_m: float = 0.5,
) -> list[dict[str, Any]]:
    """A 20 Hz `telemetry_samples`-shaped trace for a given speed-versus-distance curve.

    The speed curve is integrated to get distance-versus-time, then sampled at a fixed
    rate — which is what the car does, and why the samples come out unevenly spaced in
    distance. `brake_fn` defaults to "on wherever the car is decelerating".
    """
    fine_d = np.arange(0.0, length_m + resolution_m, resolution_m)
    speed_ms = np.asarray(speed_fn(fine_d), dtype=np.float64) / 3.6
    step_time = np.diff(fine_d) / (0.5 * (speed_ms[:-1] + speed_ms[1:]))
    fine_t = np.concatenate(([0.0], np.cumsum(step_time)))

    sample_t = np.arange(0.0, fine_t[-1], 1.0 / hz)
    distance = np.interp(sample_t, fine_t, fine_d)
    speed = np.asarray(speed_fn(distance), dtype=np.float64)
    if brake_fn is None:
        slope = np.gradient(speed, distance)
        brake = np.where(slope < -0.02, 1.0, 0.0)
    else:
        brake = np.asarray(brake_fn(distance), dtype=np.float64)

    return [
        {
            "lap_distance_m": float(d),
            "session_time_s": float(start_time + t),
            "speed_kmh": float(v),
            "throttle": float(1.0 - b),
            "brake": float(b),
            "steer": 0.0,
            "gear": int(np.clip(v // 40, 1, 8)),
            "rpm": int(8000 + v * 10),
            "drs_or_aero": 0,
        }
        for d, t, v, b in zip(distance, sample_t, speed, brake, strict=True)
    ]


def constant_speed_rows(
    length_m: float, speed_kmh: float, *, step_m: float = 2.0, start_time: float = 0.0
) -> list[dict[str, Any]]:
    """The simplest analytic lap: `session_time = distance / speed`, exactly."""
    distance = np.arange(0.0, length_m + step_m, step_m)
    time = start_time + distance / (speed_kmh / 3.6)
    return [
        {
            "lap_distance_m": float(d),
            "session_time_s": float(t),
            "speed_kmh": speed_kmh,
            "throttle": 1.0,
            "brake": 0.0,
            "steer": 0.0,
            "gear": 7,
            "rpm": 11000,
            "drs_or_aero": 0,
        }
        for d, t in zip(distance, time, strict=True)
    ]


#: Three corners at 500/1500/2500 m, chosen so their minima land one in each `kind` band:
#: 100 km/h (slow), 160 (medium), 240 (fast). Straight-line speed is 300 km/h.
THREE_CORNERS: tuple[tuple[float, float, float], ...] = (
    (500.0, 200.0, 60.0),
    (1500.0, 140.0, 70.0),
    (2500.0, 60.0, 80.0),
)
TRACK_M = 3000.0
BASE_SPEED_KMH = 300.0


def three_corner_speed(
    distance: Any, dips: tuple[tuple[float, float, float], ...] = THREE_CORNERS
) -> Any:
    speed = np.full_like(np.asarray(distance, dtype=np.float64), BASE_SPEED_KMH)
    for centre, depth, width in dips:
        speed -= depth * np.exp(-0.5 * ((np.asarray(distance) - centre) / width) ** 2)
    return speed


def square_brake(zones: tuple[tuple[float, float], ...]) -> Any:
    def brake_fn(distance: Any) -> Any:
        d = np.asarray(distance, dtype=np.float64)
        out = np.zeros_like(d)
        for lo, hi in zones:
            out[(d >= lo) & (d <= hi)] = 1.0
        return out

    return brake_fn


def lap_ref(rows: list[dict[str, Any]], **overrides: Any) -> LapRef:
    fields: dict[str, Any] = {
        "session_id": 1,
        "lap_number": 1,
        "car_index": 0,
        "track_id": 2,
        "track_name": "Bahrain",
        "lap_time_ms": 92_000,
        "sectors_ms": (28_000, 34_000, 30_000),
    }
    fields.update(overrides)
    return LapRef(trace=trace_from_rows(rows), **fields)


# --------------------------------------------------------------------------------------------
# Resampling
# --------------------------------------------------------------------------------------------


def test_trace_is_a_function_of_distance() -> None:
    """Unsorted, duplicated and rewound rows still come out strictly increasing."""
    rows = constant_speed_rows(200.0, 180.0)
    scrambled = [rows[5], rows[0], rows[0], *rows[1:5], *rows[5:]]
    scrambled.insert(3, rows[2] | {"session_time_s": rows[2]["session_time_s"] - 4.0})

    trace = trace_from_rows(scrambled)
    assert np.all(np.diff(trace.distance_m) > 0)
    assert np.all(np.diff(trace.session_time_s) > 0)
    assert trace.start_m == 0.0
    assert trace.end_m == 200.0
    assert set(trace.channels) == set(resample_mod.TRACE_CHANNELS)


def test_trace_rejects_a_lap_with_nothing_in_it() -> None:
    with pytest.raises(AnalysisError) as empty:
        trace_from_rows([])
    assert empty.value.status_code == 404

    with pytest.raises(AnalysisError) as thin:
        trace_from_rows([{"lap_distance_m": 1.0, "session_time_s": 1.0}])
    assert thin.value.status_code == 404


def test_nulls_in_one_channel_do_not_hole_the_rest() -> None:
    rows = constant_speed_rows(100.0, 200.0)
    rows[3]["speed_kmh"] = None
    rows[4]["gear"] = None
    trace = trace_from_rows(rows)
    assert np.all(np.isfinite(trace.channel("speed_kmh")))
    assert trace.channel("speed_kmh")[3] == pytest.approx(200.0)
    assert np.all(np.isfinite(trace.channel("gear")))


def test_resampling_reproduces_a_known_analytic_curve() -> None:
    """At 180 km/h (50 m/s) the exact answer for time at distance d is d/50."""
    trace = trace_from_rows(constant_speed_rows(1000.0, 180.0, step_m=7.0))
    grid = make_grid(0.0, 1000.0)
    lap = resample_onto(trace, grid)

    assert grid[1] - grid[0] == pytest.approx(GRID_STEP_M)
    assert np.all(np.isfinite(lap.session_time_s))
    assert lap.session_time_s == pytest.approx(grid / 50.0, abs=1e-9)
    assert lap.channel("speed_kmh") == pytest.approx(np.full(grid.size, 180.0))


def test_resampling_is_linear_between_samples() -> None:
    """A speed ramp is exactly recoverable: linear interpolation of a linear function."""
    distance = np.arange(0.0, 1000.0, 25.0)
    speed = 100.0 + 0.2 * distance  # km/h, 100 -> 300 over the lap
    time = np.cumsum(np.concatenate(([0.0], np.diff(distance) / (speed[:-1] / 3.6))))
    rows = [
        {"lap_distance_m": float(d), "session_time_s": float(t), "speed_kmh": float(v)}
        for d, t, v in zip(distance, time, speed, strict=True)
    ]
    lap = resample_onto(trace_from_rows(rows), make_grid(0.0, 900.0))
    expected = 100.0 + 0.2 * lap.grid_m
    assert lap.channel("speed_kmh") == pytest.approx(expected, abs=1e-9)


def test_state_channels_are_held_not_averaged() -> None:
    """Gear must never come back as 4.5: an interpolated state is a state that never was."""
    rows = constant_speed_rows(100.0, 200.0, step_m=10.0)
    for i, row in enumerate(rows):
        row["gear"] = 3 + (i % 2)
    lap = resample_onto(trace_from_rows(rows), make_grid(0.0, 100.0))
    assert set(np.unique(lap.channel("gear"))) <= {3.0, 4.0}


def test_a_lap_never_extrapolates_past_the_track_it_covered() -> None:
    trace = trace_from_rows(constant_speed_rows(400.0, 200.0, start_time=10.0))
    lap = resample_onto(trace, make_grid(0.0, 900.0))
    assert np.all(np.isfinite(lap.session_time_s[lap.grid_m <= 400.0]))
    assert np.all(np.isnan(lap.session_time_s[lap.grid_m > 400.0]))
    assert np.all(np.isnan(lap.channel("speed_kmh")[lap.grid_m > 400.0]))


def test_grid_bounds_and_refusals() -> None:
    grid = make_grid(10.0, 27.0, step_m=5.0)
    assert grid.tolist() == [10.0, 15.0, 20.0, 25.0]  # never runs past the end

    with pytest.raises(AnalysisError) as no_overlap:
        make_grid(100.0, 101.0)
    assert no_overlap.value.status_code == 422

    with pytest.raises(AnalysisError) as too_wide:
        make_grid(0.0, resample_mod.MAX_GRID_POINTS * GRID_STEP_M * 2)
    assert too_wide.value.status_code == 422


def test_overlap_grid_covers_only_shared_distance() -> None:
    a = trace_from_rows(constant_speed_rows(1000.0, 200.0))
    b = trace_from_rows(constant_speed_rows(600.0, 200.0, start_time=100.0))
    b.distance_m[:] += 120.0  # b starts late and finishes early
    grid = overlap(a, b)
    assert grid[0] == pytest.approx(120.0)
    assert grid[-1] <= 720.0


# --------------------------------------------------------------------------------------------
# Cumulative delta
# --------------------------------------------------------------------------------------------


def test_delta_sign_convention_negative_means_ahead() -> None:
    """900 m at 180 km/h is 18.0 s; at 150 km/h it is 21.6 s. The faster lap must go negative."""
    fast = trace_from_rows(constant_speed_rows(900.0, 180.0))
    slow = trace_from_rows(constant_speed_rows(900.0, 150.0, start_time=500.0))
    grid = overlap(fast, slow)

    ahead = cumulative_delta_ms(
        resample_onto(fast, grid).session_time_s, resample_onto(slow, grid).session_time_s
    )
    assert ahead[0] == pytest.approx(0.0)  # measured from the start of the window
    assert np.all(np.diff(ahead) < 0)  # the gap only ever grows in the faster lap's favour
    assert ahead[-1] == pytest.approx(-3600.0, abs=1.0)

    behind = cumulative_delta_ms(
        resample_onto(slow, grid).session_time_s, resample_onto(fast, grid).session_time_s
    )
    assert behind[-1] == pytest.approx(3600.0, abs=1.0)


def test_delta_ignores_the_absolute_session_clock() -> None:
    """Lap 3 of one session against lap 40 of another: only elapsed time may matter."""
    early = trace_from_rows(constant_speed_rows(500.0, 200.0, start_time=12.5))
    late = trace_from_rows(constant_speed_rows(500.0, 200.0, start_time=3612.5))
    grid = overlap(early, late)
    delta = cumulative_delta_ms(
        resample_onto(early, grid).session_time_s, resample_onto(late, grid).session_time_s
    )
    assert delta == pytest.approx(np.zeros(grid.size), abs=1e-6)


def test_delta_has_no_opinion_where_the_reference_never_drove() -> None:
    full = trace_from_rows(constant_speed_rows(1000.0, 200.0))
    partial = trace_from_rows(constant_speed_rows(400.0, 200.0))
    grid = make_grid(0.0, 1000.0)
    delta = cumulative_delta_ms(
        resample_onto(full, grid).session_time_s, resample_onto(partial, grid).session_time_s
    )
    assert np.all(np.isfinite(delta[grid <= 400.0]))
    assert np.all(np.isnan(delta[grid > 400.0]))


# --------------------------------------------------------------------------------------------
# Corner segmentation
# --------------------------------------------------------------------------------------------


def test_three_corner_profile_yields_exactly_three_corners() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    result = analyse_corners(lap_ref(rows), None)
    found = result["corners"]

    assert len(found) == 3
    for corner, (centre, _depth, _width) in zip(found, THREE_CORNERS, strict=True):
        assert corner["apex_m"] == pytest.approx(centre, abs=15.0)
        assert corner["entry_m"] < corner["apex_m"] < corner["exit_m"]
    assert [c["n"] for c in found] == [1, 2, 3]
    assert [c["kind"] for c in found] == ["slow", "medium", "fast"]
    assert [round(c["min_speed_kmh"]) for c in found] == [100, 160, 240]


def test_corner_windows_are_disjoint_and_ordered() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    found = analyse_corners(lap_ref(rows), None)["corners"]
    for earlier, later in zip(found, found[1:], strict=False):
        assert earlier["exit_m"] < later["entry_m"]


def test_a_shallow_lift_is_not_a_corner() -> None:
    """The prominence gate is what stops a 4 km/h lift becoming a phantom corner."""
    dips = (*THREE_CORNERS, (2000.0, 4.0, 40.0))
    rows = synthetic_lap(lambda d: three_corner_speed(d, dips), TRACK_M)
    found = analyse_corners(lap_ref(rows), None)["corners"]
    assert len(found) == 3
    assert all(abs(c["apex_m"] - 2000.0) > 100.0 for c in found)


def test_a_double_apex_is_reported_as_one_corner() -> None:
    """Two minima 40 m apart share one braking event, so they are one row, not two."""
    dips = ((1480.0, 150.0, 45.0), (1520.0, 150.0, 45.0))
    rows = synthetic_lap(lambda d: three_corner_speed(d, dips), TRACK_M)
    found = analyse_corners(lap_ref(rows), None)["corners"]
    assert len(found) == 1
    assert found[0]["apex_m"] == pytest.approx(1500.0, abs=30.0)


def test_brake_points_land_where_the_driver_hit_the_pedal() -> None:
    zones = tuple((centre - 200.0, centre - 30.0) for centre, _d, _w in THREE_CORNERS)
    rows = synthetic_lap(three_corner_speed, TRACK_M, brake_fn=square_brake(zones))
    found = analyse_corners(lap_ref(rows), None)["corners"]

    for corner, (centre, _depth, _width) in zip(found, THREE_CORNERS, strict=True):
        assert corner["brake_point_m"] == pytest.approx(centre - 200.0, abs=GRID_STEP_M * 2)
        assert corner["brake_point_m"] < corner["entry_m"]  # brake before you turn in


def test_a_dab_on_the_straight_is_not_a_braking_point() -> None:
    grid = np.arange(0.0, 1000.0, GRID_STEP_M)
    brake = np.zeros_like(grid)
    brake[(grid >= 300.0) & (grid <= 304.0)] = 1.0  # a 5 m blip, below BRAKE_SUSTAIN_M
    assert brake_point(grid, brake, apex=160, floor=0) is None

    brake[(grid >= 600.0) & (grid <= 700.0)] = 1.0  # a real application
    assert brake_point(grid, brake, apex=160, floor=0) == pytest.approx(600.0)


def test_braking_point_is_the_last_application_before_the_apex() -> None:
    zones = ((1150.0, 1160.0), (1350.0, 1480.0))  # a dab, then the real thing
    rows = synthetic_lap(
        lambda d: three_corner_speed(d, (THREE_CORNERS[1],)), TRACK_M, brake_fn=square_brake(zones)
    )
    found = analyse_corners(lap_ref(rows), None)["corners"]
    assert len(found) == 1
    assert found[0]["brake_point_m"] == pytest.approx(1350.0, abs=GRID_STEP_M * 2)


def test_time_loss_is_attributed_to_the_corner_that_caused_it() -> None:
    """Drive corner 2 slower and nothing else: the loss must show up in corner 2's row."""
    reference = synthetic_lap(three_corner_speed, TRACK_M)
    slower_dips = (THREE_CORNERS[0], (1500.0, 170.0, 70.0), THREE_CORNERS[2])
    subject = synthetic_lap(lambda d: three_corner_speed(d, slower_dips), TRACK_M)

    result = analyse_corners(lap_ref(subject, lap_number=12), lap_ref(reference, lap_number=9))
    found = result["corners"]

    assert result["ref_lap_number"] == 9
    assert len(found) == 3
    assert found[1]["time_loss_ms"] > 200  # the corner we deliberately ruined
    assert abs(found[0]["time_loss_ms"]) < 15
    assert abs(found[2]["time_loss_ms"]) < 15
    assert found[1]["min_speed_kmh"] < found[1]["ref_min_speed_kmh"]
    assert result["total_delta_ms"] > 0

    accounted = sum(c["time_loss_ms"] for c in found)
    assert result["straights_time_loss_ms"] == pytest.approx(
        result["total_delta_ms"] - accounted, abs=2
    )


def test_a_lap_compared_with_itself_loses_no_time() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    result = analyse_corners(lap_ref(rows), lap_ref(rows))
    assert result["total_delta_ms"] == 0
    assert all(corner["time_loss_ms"] == 0 for corner in result["corners"])
    assert result["straights_time_loss_ms"] == 0


def test_corners_without_a_reference_still_segment() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    result = analyse_corners(lap_ref(rows), None)
    assert result["ref_lap_number"] is None
    assert result["total_delta_ms"] is None
    assert len(result["corners"]) == 3
    for corner in result["corners"]:
        assert corner["time_loss_ms"] is None
        assert corner["ref_min_speed_kmh"] is None
        assert corner["ref_brake_point_m"] is None
        assert corner["min_speed_kmh"] is not None


def test_segmentation_is_deterministic() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    first = analyse_corners(lap_ref(rows), None)
    second = analyse_corners(lap_ref(list(reversed(rows))), None)
    assert first == second


def test_corners_refuse_a_reference_from_another_circuit() -> None:
    rows = synthetic_lap(three_corner_speed, TRACK_M)
    with pytest.raises(AnalysisError) as exc:
        analyse_corners(lap_ref(rows, track_id=2), lap_ref(rows, track_id=9))
    assert exc.value.status_code == 422


def test_smoothing_preserves_the_minimum_it_is_asked_to_find() -> None:
    """A filter that moved the apex would make every brake point and time loss wrong."""
    grid = np.arange(0.0, 3000.0, GRID_STEP_M)
    clean = three_corner_speed(grid)
    noisy = clean + 3.0 * np.sin(grid / 4.0)  # throttle ripple, well under the prominence gate

    apexes = find_apexes(smooth_speed(noisy))
    assert [round(float(grid[i]), -1) for i in apexes] == [500.0, 1500.0, 2500.0]
    assert float(np.abs(smooth_speed(noisy) - clean).max()) < 3.0


def test_corner_kind_bands() -> None:
    assert corners_mod.classify(80.0) == "slow"
    assert corners_mod.classify(corners_mod.SLOW_MAX_KMH - 0.1) == "slow"
    assert corners_mod.classify(corners_mod.SLOW_MAX_KMH) == "medium"
    assert corners_mod.classify(corners_mod.MEDIUM_MAX_KMH - 0.1) == "medium"
    assert corners_mod.classify(corners_mod.MEDIUM_MAX_KMH) == "fast"


# --------------------------------------------------------------------------------------------
# Compare payload
# --------------------------------------------------------------------------------------------


def test_compare_payload_arrays_are_parallel() -> None:
    fast = synthetic_lap(three_corner_speed, TRACK_M)
    slow_dips = tuple((c, d * 1.05, w) for c, d, w in THREE_CORNERS)
    slow = synthetic_lap(lambda d: three_corner_speed(d, slow_dips), TRACK_M)

    payload = compare_laps(
        lap_ref(fast, lap_number=12, lap_time_ms=92_345),
        lap_ref(slow, lap_number=15, lap_time_ms=93_100),
    )
    points = payload["points"]
    assert points == len(payload["grid_m"]) == len(payload["delta_ms"])
    for side in ("a", "b"):
        for channel in ("speed_kmh", "throttle", "brake", "gear"):
            assert len(payload[side][channel]) == points
    assert payload["a"]["lap_number"] == 12
    assert payload["b"]["lap_time_ms"] == 93_100
    assert payload["track_id"] == 2 and payload["track_name"] == "Bahrain"
    assert payload["sectors_a"] == [28_000, 34_000, 30_000]
    assert all(isinstance(g, int) or g is None for g in payload["a"]["gear"])
    assert payload["delta_ms"][0] == 0.0
    assert payload["delta_ms"][-1] < 0  # the faster lap is `a`, so the delta goes negative


def test_compare_refuses_two_different_circuits() -> None:
    rows = constant_speed_rows(500.0, 200.0)
    with pytest.raises(AnalysisError) as exc:
        compare_laps(lap_ref(rows, track_id=2), lap_ref(rows, track_id=13))
    assert exc.value.status_code == 422


def test_compare_refuses_laps_that_never_share_track() -> None:
    a = constant_speed_rows(300.0, 200.0)
    b = [row | {"lap_distance_m": row["lap_distance_m"] + 2000.0} for row in a]
    with pytest.raises(AnalysisError) as exc:
        compare_laps(lap_ref(a), lap_ref(b))
    assert exc.value.status_code == 422


def test_compare_grid_is_bounded_by_the_shorter_lap() -> None:
    full = constant_speed_rows(1200.0, 220.0)
    partial = constant_speed_rows(500.0, 220.0)
    payload = compare_laps(lap_ref(full), lap_ref(partial))
    assert payload["grid_m"][-1] <= 500.0
    assert all(value is not None for value in payload["delta_ms"])


# --------------------------------------------------------------------------------------------
# Stints
# --------------------------------------------------------------------------------------------


def degrading_laps(
    lap_start: int,
    count: int,
    *,
    base_ms: int = 92_000,
    deg_ms: float = 80.0,
    car_index: int = 0,
    compound: int = 16,
    noise_ms: float = 40.0,
    seed: int = 7,
) -> list[dict[str, Any]]:
    """A stint whose lap times really are `base + deg * age`, plus reproducible noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for age in range(count):
        rows.append(
            {
                "car_index": car_index,
                "lap_number": lap_start + age,
                "lap_time_ms": int(round(base_ms + deg_ms * age + rng.normal(0.0, noise_ms))),
                "valid": True,
                "compound_actual": compound + 1,
                "compound_visual": compound,
                "tyre_age_laps": age,
            }
        )
    return rows


def test_fit_recovers_a_known_degradation_slope() -> None:
    laps = degrading_laps(1, 14, base_ms=92_000, deg_ms=80.0)
    fit = fit_degradation(
        [float(row["tyre_age_laps"]) for row in laps],
        [float(row["lap_time_ms"]) for row in laps],
    )
    assert fit is not None
    assert fit.deg_ms_per_lap == pytest.approx(80.0, abs=10.0)
    assert fit.base_ms == pytest.approx(92_000.0, abs=60.0)
    assert fit.r2 > 0.9
    assert fit.n_used == 14


def test_fit_needs_enough_laps_to_mean_anything() -> None:
    assert fit_degradation([0.0, 1.0, 2.0], [92_000.0, 92_100.0, 92_200.0]) is None
    assert fit_degradation([], []) is None
    assert fit_degradation([2.0] * 6, [92_000.0] * 6) is None  # no spread in tyre age
    assert fit_degradation([0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]) is not None


def test_a_flat_stint_fits_a_flat_line() -> None:
    fit = fit_degradation([0.0, 1.0, 2.0, 3.0, 4.0], [92_000.0] * 5)
    assert fit is not None
    assert fit.deg_ms_per_lap == pytest.approx(0.0, abs=1e-6)
    assert fit.r2 == pytest.approx(1.0)


def test_outliers_are_flagged_excluded_and_still_returned() -> None:
    laps = degrading_laps(1, 14)
    laps[6]["lap_time_ms"] = 106_000  # traffic: ~115% of the median
    stint = {
        "car_index": 0,
        "stint_no": 1,
        "compound_visual": 16,
        "compound_actual": 17,
        "lap_start": 1,
        "lap_end": 14,
        "wear_at_end_json": {"rl": 41.0, "rr": 44.5, "fl": 38.0, "fr": 39.5},
    }
    payload = build_stints(3, [stint], laps)
    only = payload["stints"][0]

    ruined = next(lap for lap in only["laps"] if lap["lap_number"] == 7)
    assert ruined["excluded"] is True
    assert ruined["exclude_reason"] == stints_mod.OUTLIER
    assert ruined["lap_time_ms"] == 106_000  # flagged, not hidden
    assert len(only["laps"]) == 14
    assert only["fit"]["n_used"] == 13
    assert only["fit"]["deg_ms_per_lap"] == pytest.approx(80.0, abs=15.0)
    assert only["wear_end_pct"] == [41.0, 44.5, 38.0, 39.5]
    assert only["derived"] is False


def test_the_outlier_threshold_is_the_documented_107_percent() -> None:
    laps = degrading_laps(1, 8, deg_ms=0.0, noise_ms=0.0)
    laps[3]["lap_time_ms"] = int(92_000 * stints_mod.OUTLIER_FACTOR) - 1
    laps[4]["lap_time_ms"] = int(92_000 * stints_mod.OUTLIER_FACTOR) + 1
    entries = build_stints(
        1, [{"car_index": 0, "stint_no": 1, "lap_start": 1, "lap_end": 8}], laps
    )["stints"][0]["laps"]
    assert entries[3]["excluded"] is False
    assert entries[4]["excluded"] is True


def test_in_and_out_laps_are_excluded_but_kept() -> None:
    first = degrading_laps(1, 10, seed=1)
    second = degrading_laps(11, 10, compound=18, seed=2)
    stints = [
        {"car_index": 0, "stint_no": 1, "compound_visual": 16, "lap_start": 1, "lap_end": 10},
        {"car_index": 0, "stint_no": 2, "compound_visual": 18, "lap_start": 11, "lap_end": 20},
    ]
    payload = build_stints(5, stints, first + second)
    one, two = payload["stints"]

    assert one["laps"][-1]["exclude_reason"] == stints_mod.IN_LAP
    assert one["laps"][0]["exclude_reason"] is None  # the session's first lap is not an out-lap
    assert two["laps"][0]["exclude_reason"] == stints_mod.OUT_LAP
    assert two["laps"][-1]["exclude_reason"] is None  # nothing follows it, so it is not an in-lap
    assert one["fit"]["n_used"] == 9
    assert two["fit"]["n_used"] == 9


def test_invalid_and_untimed_laps_are_excluded() -> None:
    laps = degrading_laps(1, 8)
    laps[2]["valid"] = False
    laps[3]["lap_time_ms"] = None
    laps[4]["lap_time_ms"] = 0
    entries = build_stints(
        1, [{"car_index": 0, "stint_no": 1, "lap_start": 1, "lap_end": 8}], laps
    )["stints"][0]["laps"]
    assert entries[2]["exclude_reason"] == stints_mod.INVALID
    assert entries[3]["exclude_reason"] == stints_mod.NO_TIME
    assert entries[4]["exclude_reason"] == stints_mod.NO_TIME


def test_a_short_stint_reports_no_fit_rather_than_a_bad_one() -> None:
    laps = degrading_laps(1, 3)
    payload = build_stints(1, [{"car_index": 0, "stint_no": 1, "lap_start": 1, "lap_end": 3}], laps)
    assert payload["stints"][0]["fit"] is None
    assert len(payload["stints"][0]["laps"]) == 3


def test_stints_are_derived_from_compound_changes_when_none_were_recorded() -> None:
    laps = degrading_laps(1, 6, compound=16) + degrading_laps(7, 6, compound=18)
    payload = build_stints(9, [], laps)
    assert [s["stint_no"] for s in payload["stints"]] == [1, 2]
    assert [s["compound_visual"] for s in payload["stints"]] == [16, 18]
    assert [(s["lap_start"], s["lap_end"]) for s in payload["stints"]] == [(1, 6), (7, 12)]
    assert all(s["derived"] is True for s in payload["stints"])
    assert all(s["wear_end_pct"] is None for s in payload["stints"])


def test_a_new_set_of_the_same_compound_is_still_a_new_stint() -> None:
    """Age resets even when the compound does not; that is the only signal a pit stop leaves."""
    laps = degrading_laps(1, 6) + degrading_laps(7, 6)  # both start at tyre_age_laps 0
    derived = derive_stints(laps)
    assert [(s["lap_start"], s["lap_end"]) for s in derived] == [(1, 6), (7, 12)]


def test_stints_can_be_restricted_to_one_car() -> None:
    laps = degrading_laps(1, 6, car_index=0) + degrading_laps(1, 6, car_index=4)
    stints = [
        {"car_index": 0, "stint_no": 1, "lap_start": 1, "lap_end": 6},
        {"car_index": 4, "stint_no": 1, "lap_start": 1, "lap_end": 6},
    ]
    everyone = build_stints(2, stints, laps)
    assert [s["car_index"] for s in everyone["stints"]] == [0, 4]
    assert all(len(s["laps"]) == 6 for s in everyone["stints"])

    one = build_stints(2, stints, laps, car_index=4)
    assert [s["car_index"] for s in one["stints"]] == [4]


def test_stint_payload_is_json_ready() -> None:
    import json

    laps = degrading_laps(1, 6)
    payload = build_stints(1, [{"car_index": 0, "stint_no": 1, "lap_start": 1, "lap_end": 6}], laps)
    assert json.loads(json.dumps(payload))["session_id"] == 1


# --------------------------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------------------------
#
# The numbers asserted here are the ones two real races produced (see `decisions.md`): softs
# at 5.9-6.1 %/lap in race trim, a 28-31 % cliff, ~1.05 kg/lap at Miami and ~1.26 at Jeddah,
# and a pit-lane loss in the mid-teens. The fixtures below are shaped to those figures so a
# regression in the model reads as an implausible race, not just a changed number.


def strategy_session(
    session_id: int,
    session_type: int,
    *,
    name: str | None = None,
    total_laps: int | None = None,
    started: float = 1_786_100_000.0,
    player: int = 21,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "session_type": session_type,
        "session_type_name": name or f"Session {session_type}",
        "track_id": 30,
        "track_name": "Miami",
        "total_laps": total_laps,
        "started_at_wall": started,
        "player_car_index": player,
    }


def race_laps_rows(
    specs: Sequence[tuple[int, int, int, float, float]],
    *,
    car_index: int = 21,
    fuel_start: float = 15.0,
    burn: float = 1.05,
) -> list[dict[str, Any]]:
    """Laps from `(lap_number, compound, tyre_age, lap_time_ms, wear_at_end)` tuples."""
    rows = []
    fuel = fuel_start
    for lap, compound, age, lap_ms, _wear in specs:
        rows.append(
            {
                "session_id": 1,
                "car_index": car_index,
                "lap_number": lap,
                "generation": 0,
                "lap_time_ms": int(lap_ms),
                "valid": True,
                "compound_actual": compound,
                "compound_visual": compound,
                "tyre_age_laps": age,
                "fuel_start_kg": round(fuel, 3),
                "fuel_end_kg": round(fuel - burn, 3),
            }
        )
        fuel -= burn
    return rows


def wear_rows(session_id: int, per_lap: Mapping[int, float]) -> list[dict[str, Any]]:
    return [
        {"session_id": session_id, "lap_number": lap, "max_wear_pct": pct}
        for lap, pct in sorted(per_lap.items())
    ]


def two_stint_race() -> tuple[list[dict[str, Any]], dict[int, Any], dict[int, Any], dict[int, Any]]:
    """A 13-lap race: six laps on softs, a stop, seven on mediums. Jeddah's shape."""
    sessions = [strategy_session(115, 15, name="Race", total_laps=13)]
    laps = race_laps_rows(
        [
            (1, 16, 0, 95_364, 6.1),
            (2, 16, 1, 92_472, 12.2),
            (3, 16, 2, 92_342, 18.3),
            (4, 16, 3, 93_255, 24.4),
            (5, 16, 4, 95_277, 30.5),
            (6, 17, 0, 110_415, 0.0),  # the stop: in-lap and out-lap in one
            (7, 17, 1, 93_979, 4.0),
            (8, 17, 2, 93_843, 8.0),
            (9, 17, 3, 94_589, 12.0),
            (10, 17, 4, 94_162, 16.0),
            (11, 17, 5, 94_011, 20.0),
            (12, 17, 6, 93_916, 24.0),
        ],
        burn=1.26,
    )
    stints = [
        {"session_id": 115, "car_index": 21, "stint_no": 1, "compound_visual": 16,
         "lap_start": 1, "lap_end": 6, "wear_at_end_json": {"tyre_wear_pct": [30.5, 28, 24, 26]}},
        {"session_id": 115, "car_index": 21, "stint_no": 2, "compound_visual": 17,
         "lap_start": 6, "lap_end": 13, "wear_at_end_json": {"tyre_wear_pct": [27.0, 25, 22, 23]}},
    ]
    wear = wear_rows(
        115,
        {1: 6.1, 2: 12.2, 3: 18.3, 4: 24.4, 5: 30.5, 6: 30.5,
         7: 4.0, 8: 8.0, 9: 12.0, 10: 16.0, 11: 20.0, 12: 24.0},
    )
    return sessions, {115: stints}, {115: laps}, {115: wear}


def build_two_stint_strategy(**overrides: Any) -> dict[str, Any]:
    sessions, stints, laps, wear = two_stint_race()
    kwargs: dict[str, Any] = {
        "race_laps": 13,
        "race_laps_source": "Race (session 115)",
        "track_name": "Jeddah",
    }
    kwargs.update(overrides)
    return build_strategy(30, sessions, stints, laps, wear, **kwargs)


def test_strategy_models_every_compound_it_saw_and_confesses_the_ones_it_did_not() -> None:
    payload = build_two_stint_strategy()
    by_compound = {c["compound_visual"]: c for c in payload["compounds"]}

    # All three dry compounds are always listed, tested or not.
    assert set(by_compound) >= {16, 17, 18}

    soft = by_compound[16]
    assert soft["untested"] is False
    assert soft["evidence"] == "race"
    assert soft["pace"]["deg_ms_per_lap"] is not None
    assert soft["wear"]["pct_per_lap"] == pytest.approx(6.1, abs=0.2)
    assert soft["wear"]["source"] == "wear_samples"
    assert soft["plannable"] is True

    # The hard was never fitted this weekend. It appears, empty, and says why.
    hard = by_compound[18]
    assert hard["untested"] is True
    assert hard["pace"] is None
    assert hard["wear"] is None
    assert hard["max_stint_laps"] is None
    assert hard["plannable"] is False
    assert "no stint" in hard["not_plannable_reason"]


def test_strategy_caps_a_stint_at_the_wear_cliff() -> None:
    payload = build_two_stint_strategy()
    by_compound = {c["compound_visual"]: c for c in payload["compounds"]}
    assert payload["wear_cliff_pct"] == strategy_mod.WEAR_CLIFF_PCT

    # 6.1 %/lap into a 28 % ceiling is four laps, not the five the driver actually ran to
    # 30.5 % — the point of the ceiling is that it stops short of the collapse.
    assert by_compound[16]["max_stint_laps"] == 4
    for plan in payload["plans"]:
        for stint in plan["stints"]:
            assert stint["projected_end_wear_pct"] <= strategy_mod.WEAR_CLIFF_PCT


def test_strategy_plans_obey_the_two_compound_rule_and_cover_the_distance() -> None:
    payload = build_two_stint_strategy()
    assert payload["plans"], "a two-compound weekend must produce at least one plan"
    for plan in payload["plans"]:
        assert len(set(plan["compounds"])) >= 2
        assert sum(stint["laps"] for stint in plan["stints"]) == payload["race_laps"]
        assert plan["stints"][0]["lap_start"] == 1
        assert plan["stints"][-1]["lap_end"] == payload["race_laps"]
        assert plan["stops"] == len(plan["stints"]) - 1
        assert len(plan["pit_windows"]) == plan["stops"]


def test_strategy_ranks_plans_by_projected_time() -> None:
    payload = build_two_stint_strategy()
    times = [plan["total_time_ms"] for plan in payload["plans"]]
    assert times == sorted(times)
    assert [plan["rank"] for plan in payload["plans"]] == list(range(1, len(times) + 1))
    assert payload["plans"][0]["delta_to_best_ms"] == 0
    assert all(plan["delta_to_best_ms"] >= 0 for plan in payload["plans"])


def test_strategy_pit_windows_bracket_the_planned_stop() -> None:
    payload = build_two_stint_strategy()
    for plan in payload["plans"]:
        for window in plan["pit_windows"]:
            assert window["earliest_lap"] <= window["planned_lap"] <= window["latest_lap"]
            assert window["window_laps"] == window["latest_lap"] - window["earliest_lap"]
        assert plan["safety_car"]["flexibility"] in ("flexible", "tight", "none")
        assert plan["safety_car"]["note"]


def test_strategy_measures_pit_loss_from_the_weekends_own_stop() -> None:
    payload = build_two_stint_strategy()
    loss = payload["pit_loss"]
    assert loss["source"] == "measured"
    assert loss["stops_measured"] == 1
    # 110.4 s pit lap against a ~94 s clean-lap median.
    assert payload["pit_loss_s"] == pytest.approx(16.5, abs=1.0)
    assert loss["stops"][0]["laps"] == [6]


def test_strategy_falls_back_to_a_named_pit_loss_and_says_so() -> None:
    """A weekend with no stop must not silently borrow a number from somewhere."""
    sessions = [strategy_session(1, 15, name="Race", total_laps=6)]
    laps = race_laps_rows([(lap, 16, lap - 1, 92_000 + lap * 60, 0.0) for lap in range(1, 7)])
    stints = {
        1: [
            {"session_id": 1, "car_index": 21, "stint_no": 1, "compound_visual": 16,
             "lap_start": 1, "lap_end": 6, "wear_at_end_json": {"tyre_wear_pct": [24, 22, 20, 21]}}
        ]
    }
    payload = build_strategy(
        30, sessions, stints, {1: laps}, {}, race_laps=6, race_laps_source="request"
    )
    assert payload["pit_loss_s"] == strategy_mod.DEFAULT_PIT_LOSS_S
    assert payload["pit_loss"]["source"] == "default"
    assert "no pit stop" in payload["pit_loss"]["detail"]


def test_strategy_fuel_recommendation_matches_the_tank_the_race_was_run_on() -> None:
    payload = build_two_stint_strategy()
    fuel = payload["fuel"]
    assert fuel["evidence"] == "race"
    assert fuel["kg_per_lap"] == pytest.approx(1.26, abs=0.02)
    assert fuel["slider_laps"] == pytest.approx(13.45, abs=0.001)
    assert fuel["recommended_kg"] == pytest.approx(13.45 * 1.26, abs=0.1)
    assert "fuel" not in payload["omitted"]


def test_strategy_omits_fuel_rather_than_inventing_it() -> None:
    """Historical captures have NULL fuel columns; the sheet must say so, not guess."""
    sessions, stints, laps, wear = two_stint_race()
    stripped = {
        sid: [{k: v for k, v in row.items() if not k.startswith("fuel_")} for row in rows]
        for sid, rows in laps.items()
    }
    payload = build_strategy(
        30, sessions, stints, stripped, wear, race_laps=13, race_laps_source="request"
    )
    assert payload["fuel"] is None
    assert "insufficient fuel data" in payload["omitted"]["fuel"]
    # Everything that does not depend on fuel still came out.
    assert payload["plans"]
    assert payload["pit_loss"]["source"] == "measured"


def test_strategy_prefers_race_trim_over_practice_for_the_same_compound() -> None:
    """Practice softs wear at roughly twice race-trim rate; the tier decides which wins."""
    sessions, stints, laps, wear = two_stint_race()
    sessions.append(strategy_session(9, 1, name="Practice 1", started=1_786_000_000.0))
    stints[9] = [
        {"session_id": 9, "car_index": 21, "stint_no": 1, "compound_visual": 16,
         "lap_start": 1, "lap_end": 6, "wear_at_end_json": {"tyre_wear_pct": [60, 55, 50, 52]}}
    ]
    laps[9] = race_laps_rows([(lap, 16, lap - 1, 90_000 + lap * 500, 0.0) for lap in range(1, 7)])
    wear[9] = wear_rows(9, {lap: 10.0 * lap for lap in range(1, 7)})

    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    assert soft["evidence"] == "race"
    assert soft["wear"]["evidence"] == "race"
    assert soft["wear"]["session_id"] == 115
    assert soft["pace"]["session_id"] == 115
    # The practice session is still listed as read, with what it did (and did not) give.
    used = {s["id"]: s for s in payload["sessions_used"]}
    assert used[9]["evidence"] == "practice"
    assert used[115]["contributed"]


def test_a_shootout_flyer_is_not_race_evidence() -> None:
    """Regression from the live Montreal weekend: the one-shot sprint shootout (type 14)
    ran two laps on softs and its 1.29 %/lap "wear rate" — a tyre that never got hot —
    outranked P1's seven-lap 7 %/lap run for the race-evidence tie-break. Shootouts are
    qualifying-shaped and must sit in the practice tier."""
    sessions, stints, laps, wear = two_stint_race()
    sessions.append(strategy_session(359, 14, name="One-Shot Sprint Shootout"))
    stints[359] = [
        {"session_id": 359, "car_index": 21, "stint_no": 1, "compound_visual": 16,
         "lap_start": 1, "lap_end": 2, "wear_at_end_json": {"tyre_wear_pct": [2.6, 2.4, 2.1, 2.2]}}
    ]
    laps[359] = race_laps_rows([(1, 16, 0, 104_222, 0.0), (2, 16, 1, 0, 0.0)])
    wear[359] = wear_rows(359, {1: 1.3, 2: 2.6})

    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    # The real race stint keeps the wear model; the shootout is read but practice-tier.
    assert soft["wear"]["session_id"] == 115
    used = {s["id"]: s for s in payload["sessions_used"]}
    assert used[359]["evidence"] == "practice"
    # And the fuel figure must not blend in shootout laps while race laps exist.
    assert 359 not in payload["fuel"]["session_ids"]


def test_strategy_uses_practice_when_that_is_all_there_is() -> None:
    sessions = [strategy_session(9, 1, name="Practice 1")]
    laps = race_laps_rows([(lap, 16, lap - 1, 92_000 + lap * 90, 0.0) for lap in range(1, 9)])
    stints = {
        9: [
            {"session_id": 9, "car_index": 21, "stint_no": 1, "compound_visual": 16,
             "lap_start": 1, "lap_end": 8, "wear_at_end_json": {"tyre_wear_pct": [24, 22, 20, 21]}}
        ]
    }
    payload = build_strategy(
        30, sessions, stints, {9: laps}, {}, race_laps=8, race_laps_source="request"
    )
    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    assert soft["evidence"] == "practice"
    assert soft["wear"]["source"] == "stint_end_wear"
    assert payload["fuel"]["evidence"] == "practice"


def test_strategy_never_plans_on_a_wet_compound() -> None:
    sessions, stints, laps, wear = two_stint_race()
    sessions.append(strategy_session(183, 14, name="One-Shot Sprint Shootout"))
    stints[183] = [
        {"session_id": 183, "car_index": 21, "stint_no": 1, "compound_visual": 7,
         "lap_start": 1, "lap_end": 8, "wear_at_end_json": {"tyre_wear_pct": [4, 4, 3, 3]}}
    ]
    laps[183] = race_laps_rows([(lap, 7, lap - 1, 99_000 + lap * 20, 0.0) for lap in range(1, 9)])
    wear[183] = wear_rows(183, {lap: 0.5 * lap for lap in range(1, 9)})

    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    inter = next(c for c in payload["compounds"] if c["compound_visual"] == 7)
    assert inter["dry"] is False
    assert inter["untested"] is False
    assert inter["plannable"] is False
    assert "wet-weather" in inter["not_plannable_reason"]
    assert all(7 not in plan["compounds"] for plan in payload["plans"])


def test_strategy_refuses_to_plan_a_race_the_tyres_cannot_reach() -> None:
    """Two stops of soft-only running cannot cover 40 laps under the cliff, and it says so."""
    payload = build_two_stint_strategy(race_laps=40)
    assert payload["plans"] == []
    assert "no legal plan" in payload["omitted"]["plans"]


def test_strategy_says_why_one_compound_is_not_enough() -> None:
    """The Miami shape: only the soft was run long enough to model."""
    sessions = [strategy_session(229, 16, name="Race 2", total_laps=14)]
    laps = race_laps_rows([(lap, 16, lap - 1, 89_000 + lap * 1_100, 0.0) for lap in range(1, 7)])
    stints = {
        229: [
            {"session_id": 229, "car_index": 21, "stint_no": 1, "compound_visual": 16,
             "lap_start": 1, "lap_end": 7,
             "wear_at_end_json": {"tyre_wear_pct": [34.7, 33, 26, 33]}}
        ]
    }
    wear = {229: wear_rows(229, {lap: 5.8 * lap for lap in range(1, 7)})}
    payload = build_strategy(
        30, sessions, stints, {229: laps}, wear, race_laps=14, race_laps_source="request"
    )
    assert payload["plans"] == []
    assert "two-compound rule" in payload["omitted"]["plans"]
    assert payload["plans_considered"] == 0


def test_strategy_is_deterministic_and_json_ready() -> None:
    import json

    first = build_two_stint_strategy()
    second = build_two_stint_strategy()
    assert first == second
    assert json.loads(json.dumps(first)) == first


# ---- feasibility plans, and degradation derived from wear -----------------------------------
#
# The weekend this recorder actually sees is not a race: it is a practice session driven two
# laps at a time, which can never fit a degradation line (four clean laps) and therefore used
# to leave the strategy page empty until a race had already been run. These fixtures are that
# weekend — three sets, an out-lap, two flyers and an in-lap on each — plus the one long run
# that changes everything, because a single stint anywhere at the circuit calibrates lap time
# against wear and lends a slope to all three compounds.


def practice_only_weekend() -> (
    tuple[list[dict[str, Any]], dict[int, Any], dict[int, Any], dict[int, Any]]
):
    """One practice session, three sets, two measurable laps on each. Montreal P1's shape.

    The stints share their boundary laps the way the recorder writes them: the lap the car
    came in on is the last lap of one stint and the first of the next, so it is an in-lap and
    an out-lap at once and neither stint may measure anything over it.
    """
    sessions = [strategy_session(41, 1, name="Practice 1")]
    laps = race_laps_rows(
        [
            (1, 16, 0, 92_400, 0.0),
            (2, 16, 1, 92_500, 0.0),
            (3, 16, 2, 110_200, 0.0),  # box: in-lap of the softs, out-lap of the mediums
            (4, 17, 1, 93_100, 0.0),
            (5, 17, 2, 93_200, 0.0),
            (6, 17, 3, 110_400, 0.0),  # box again
            (7, 18, 1, 93_800, 0.0),
            (8, 18, 2, 93_900, 0.0),
        ]
    )
    stints = {
        41: [
            {"session_id": 41, "car_index": 21, "stint_no": 1, "compound_visual": 16,
             "lap_start": 1, "lap_end": 3,
             "wear_at_end_json": {"tyre_wear_pct": [15.0, 14, 12, 13]}},
            {"session_id": 41, "car_index": 21, "stint_no": 2, "compound_visual": 17,
             "lap_start": 3, "lap_end": 6,
             "wear_at_end_json": {"tyre_wear_pct": [10.5, 10, 9, 9]}},
            {"session_id": 41, "car_index": 21, "stint_no": 3, "compound_visual": 18,
             "lap_start": 6, "lap_end": 8,
             "wear_at_end_json": {"tyre_wear_pct": [5.0, 4.8, 4.2, 4.4]}},
        ]
    }
    # 5.0 %/lap on the softs, 3.5 on the mediums, 2.5 on the hards — each set's own count,
    # restarting when it goes on the car.
    wear = {
        41: wear_rows(
            41,
            {1: 5.0, 2: 10.0, 3: 15.0, 4: 3.5, 5: 7.0, 6: 10.5, 7: 2.5, 8: 5.0},
        )
    }
    return sessions, stints, {41: laps}, wear


def add_long_run(
    sessions: list[dict[str, Any]],
    stints: dict[int, Any],
    laps: dict[int, Any],
    wear: dict[int, Any],
    *,
    session_id: int = 42,
    compound: int = 17,
    ms_per_lap: int = 300,
    base_ms: int = 92_000,
    wear_per_lap: float = 3.5,
    session_type: int = 15,
    name: str = "Race",
) -> None:
    """Bolt a six-lap stint onto a weekend: the one run that can calibrate wear against time."""
    sessions.append(
        strategy_session(
            session_id, session_type, name=name, total_laps=14, started=1_786_200_000.0
        )
    )
    stints[session_id] = [
        {"session_id": session_id, "car_index": 21, "stint_no": 1, "compound_visual": compound,
         "lap_start": 1, "lap_end": 6,
         "wear_at_end_json": {"tyre_wear_pct": [wear_per_lap * 6, 19, 17, 16]}}
    ]
    laps[session_id] = race_laps_rows(
        [(lap, compound, lap - 1, base_ms + (lap - 1) * ms_per_lap, 0.0) for lap in range(1, 7)]
    )
    wear[session_id] = wear_rows(session_id, {lap: wear_per_lap * lap for lap in range(1, 7)})


def test_a_practice_only_weekend_still_gets_plans_ranked_by_a_stated_rule() -> None:
    """Two laps a set is a wear rate, and a wear rate is enough to know a plan *finishes*."""
    sessions, stints, laps, wear = practice_only_weekend()
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )

    by_compound = {c["compound_visual"]: c for c in payload["compounds"]}
    for compound, rate in ((16, 5.0), (17, 3.5), (18, 2.5)):
        row = by_compound[compound]
        assert row["wear"]["pct_per_lap"] == pytest.approx(rate, abs=0.01)
        assert row["pace"] is None, "two laps cannot fit a degradation line"
        assert row["feasible"] is True
        assert row["plannable"] is False
        assert "feasibility plans" in row["not_plannable_reason"]

    assert payload["plans"], "a weekend with three wear rates has feasible plans"
    assert payload["plans_ranking"] == "heuristic"
    assert "ranked by rule" in payload["plans_ranking_note"]
    assert "plans" not in payload["omitted"]
    # Nothing ran long enough to calibrate one, and the sheet says so rather than going quiet.
    assert payload["wear_calibration"] is None
    assert "wear_calibration" in payload["omitted"]


def test_a_heuristic_plan_never_projects_a_time_it_cannot_measure() -> None:
    sessions, stints, laps, wear = practice_only_weekend()
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )
    for plan in payload["plans"]:
        assert plan["total_time_ms"] is None
        assert plan["delta_to_best_ms"] is None
        # Everything the wear rates *do* support is still real.
        assert sum(stint["laps"] for stint in plan["stints"]) == 14
        assert len(plan["pit_windows"]) == plan["stops"]
        for stint in plan["stints"]:
            assert stint["projected_end_wear_pct"] <= strategy_mod.WEAR_CLIFF_PCT
        for window in plan["pit_windows"]:
            assert window["earliest_lap"] <= window["planned_lap"] <= window["latest_lap"]


def test_the_heuristic_is_fewest_stops_then_the_softer_compound_earlier() -> None:
    sessions, stints, laps, wear = practice_only_weekend()
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )
    ordering = [(plan["stops"], plan["compounds"]) for plan in payload["plans"]]
    assert ordering == sorted(ordering)
    # Soft → Hard is the only one-stopper that starts on the softest compound and still
    # reaches the flag (softs last 5 laps, hards 11), so it is what the rule puts first.
    assert payload["plans"][0]["stops"] == 1
    assert payload["plans"][0]["compounds"] == [16, 18]
    assert [plan["rank"] for plan in payload["plans"]] == list(
        range(1, len(payload["plans"]) + 1)
    )


def test_one_long_run_calibrates_wear_against_time_for_the_whole_circuit() -> None:
    """The headline of this feature: a six-lap medium stint gives the softs and the hards a
    degradation slope they never drove for."""
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear)
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )

    calibration = payload["wear_calibration"]
    # 300 ms a lap over 3.5 % of wear a lap is 85.7 ms per percent.
    assert calibration["ms_per_wear_pct"] == pytest.approx(85.7, abs=0.5)
    assert calibration["compound_visual"] == 17
    assert calibration["session_id"] == 42
    assert calibration["laps_used"] == 6
    assert calibration["evidence"] == "race"
    assert "assumption" in calibration

    by_compound = {c["compound_visual"]: c for c in payload["compounds"]}
    soft, hard = by_compound[16], by_compound[18]
    assert soft["pace"]["source"] == "derived"
    assert soft["pace"]["deg_ms_per_lap"] == pytest.approx(85.7 * 5.0, abs=3.0)
    assert hard["pace"]["deg_ms_per_lap"] == pytest.approx(85.7 * 2.5, abs=3.0)
    # A derived slope has no r² of its own: it was never fitted to these laps.
    assert soft["pace"]["r2"] is None
    # Base pace is the middle of what the compound actually did here, and says so.
    assert soft["pace"]["base_ms"] == pytest.approx(92_450, abs=1.0)
    assert soft["pace"]["base_source"]["source"] == "median_clean_laps"
    assert soft["pace"]["base_source"]["session_ids"] == [41]

    assert soft["plannable"] is True
    assert hard["plannable"] is True
    assert payload["plans_ranking"] == "time"
    assert all(plan["total_time_ms"] is not None for plan in payload["plans"])
    assert "wear_calibration" not in payload["omitted"]


def test_a_derived_slope_names_both_the_stints_it_was_assembled_from() -> None:
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear)
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )
    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    provenance = soft["pace"]["derived_from"]

    # The slope's parent: the race stint the coefficient was measured on.
    assert provenance["calibration"]["session_id"] == 42
    assert provenance["calibration"]["session_label"] == "Race (session 42)"
    assert provenance["calibration"]["stint_no"] == 1
    assert provenance["calibration"]["lap_range"] == [1, 6]
    assert provenance["calibration"]["laps_used"] == 6
    assert provenance["calibration"]["compound_visual"] == 17

    # The wear rate's parent: this compound's own practice run.
    assert provenance["wear"]["session_id"] == 41
    assert provenance["wear"]["stint_no"] == 1
    assert provenance["wear"]["source"] == "wear_samples"
    assert provenance["wear_pct_per_lap"] == pytest.approx(5.0, abs=0.01)
    assert provenance["ms_per_wear_pct"] == payload["wear_calibration"]["ms_per_wear_pct"]

    # And the sessions list says what each one gave.
    used = {s["id"]: s for s in payload["sessions_used"]}
    assert "wear-to-time calibration" in used[42]["contributed"]
    assert "soft wear" in used[41]["contributed"]
    assert "soft pace" in used[42]["contributed"]


def test_a_compounds_own_fit_beats_a_derived_slope() -> None:
    """Derivation fills gaps; it never overrides a line fitted to the compound's own laps."""
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear)
    # A six-lap practice run on the softs, going off three times as fast as the coefficient
    # would predict for them (85.7 ms/% × 5 %/lap ≈ 429 ms/lap).
    add_long_run(
        sessions,
        stints,
        laps,
        wear,
        session_id=43,
        compound=16,
        ms_per_lap=900,
        base_ms=91_500,
        wear_per_lap=5.0,
        session_type=2,
        name="Practice 2",
    )
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )

    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    assert soft["pace"]["source"] == "fit"
    assert soft["pace"]["deg_ms_per_lap"] == pytest.approx(900, abs=5.0)
    assert soft["pace"]["r2"] is not None
    assert "derived_from" not in soft["pace"]
    # Race trim still owns the coefficient, even though the practice run is longer-lived
    # evidence about the softs specifically.
    assert payload["wear_calibration"]["session_id"] == 42
    assert payload["wear_calibration"]["compound_visual"] == 17
    # The hard, which fitted nothing of its own, is still derived.
    hard = next(c for c in payload["compounds"] if c["compound_visual"] == 18)
    assert hard["pace"]["source"] == "derived"


def test_a_negative_calibration_is_reported_raw_and_clamped_for_planning() -> None:
    """Same rule a negative fitted slope already obeys — a set does not get quicker with age."""
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear, ms_per_lap=-400, base_ms=93_500)
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )

    calibration = payload["wear_calibration"]
    assert calibration["ms_per_wear_pct"] == pytest.approx(-400 / 3.5, abs=1.0)
    assert calibration["ms_per_wear_pct_planned"] == 0.0

    soft = next(c for c in payload["compounds"] if c["compound_visual"] == 16)
    assert soft["pace"]["deg_ms_per_lap"] < 0, "the measured number is reported unchanged"
    assert soft["pace"]["deg_ms_per_lap_planned"] == 0.0

    # And the plans really were built on zero, not on the negative: every stint costs exactly
    # its laps × base pace, plus the pit loss.
    assert payload["plans_ranking"] == "time"
    bases = {
        c["compound_visual"]: c["pace"]["base_ms"] for c in payload["compounds"] if c["pace"]
    }
    plan = payload["plans"][0]
    flat = sum(stint["laps"] * bases[stint["compound_visual"]] for stint in plan["stints"])
    assert plan["total_time_ms"] == pytest.approx(
        flat + plan["stops"] * payload["pit_loss_s"] * 1000.0, abs=5.0
    )


def test_a_wet_compound_never_borrows_the_dry_coefficient() -> None:
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear)
    sessions.append(strategy_session(44, 2, name="Practice 2", started=1_786_300_000.0))
    stints[44] = [
        {"session_id": 44, "car_index": 21, "stint_no": 1, "compound_visual": 7,
         "lap_start": 1, "lap_end": 3, "wear_at_end_json": {"tyre_wear_pct": [1.5, 1.4, 1.2, 1.3]}}
    ]
    laps[44] = race_laps_rows([(lap, 7, lap - 1, 99_000 + lap * 20, 0.0) for lap in range(1, 4)])
    wear[44] = wear_rows(44, {lap: 0.5 * lap for lap in range(1, 4)})

    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )
    inter = next(c for c in payload["compounds"] if c["compound_visual"] == 7)
    assert inter["pace"] is None
    assert inter["feasible"] is False
    assert "wet-weather" in inter["not_plannable_reason"]
    assert all(7 not in plan["compounds"] for plan in payload["plans"])


def test_a_short_run_that_barely_wore_does_not_become_a_calibration() -> None:
    """Milliseconds per percent divides by the wear span; a stint that wore nothing would
    report the driver's lap-to-lap scatter as seconds of degradation."""
    sessions, stints, laps, wear = practice_only_weekend()
    add_long_run(sessions, stints, laps, wear, wear_per_lap=0.2)
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
    )
    assert payload["wear_calibration"] is None
    assert payload["plans_ranking"] == "heuristic"


def test_no_plan_contains_a_token_stint() -> None:
    """Regression from the live Montreal weekend: with every slope clamped to zero the
    ranking collapsed to base pace and the 'fastest' plans ran the mandatory second
    compound for one token lap — an out-lap and in-lap around a stop, no racing. Every
    stint in every plan, both ranking modes, must run at least MIN_STINT_LAPS."""
    from f126.analysis.strategy import MIN_STINT_LAPS

    sessions, stints, laps, wear = two_stint_race()
    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    assert payload["plans"]
    for plan in payload["plans"]:
        for stint in plan["stints"]:
            assert stint["laps"] >= MIN_STINT_LAPS, (plan["compounds"], stint)


def test_time_ranking_survives_a_compound_that_only_has_a_wear_rate() -> None:
    """A third compound with a wear rate and no pace must not demote a ranked plan set.

    A projected time is strictly more information than a rule, so a weekend that has already
    modelled two compounds keeps its ranking when a third turns up half-measured — the third
    is simply left out of the plans rather than dragging the whole sheet down to heuristic.
    """
    sessions, stints, laps, wear = two_stint_race()
    # Three laps on the hards in practice, all of them timeless — the capture joined the
    # session late and never got a lap time, which is a real shape in this archive. The wear
    # samples still landed, so the set's life is known and its pace is not.
    sessions.append(strategy_session(77, 1, name="Practice 1", started=1_786_000_000.0))
    stints[77] = [
        {"session_id": 77, "car_index": 21, "stint_no": 1, "compound_visual": 18,
         "lap_start": 1, "lap_end": 3, "wear_at_end_json": {"tyre_wear_pct": [9.0, 8, 7, 7]}}
    ]
    laps[77] = race_laps_rows([(lap, 18, lap - 1, 0, 0.0) for lap in range(1, 4)])
    wear[77] = wear_rows(77, {lap: 3.5 * lap for lap in range(1, 4)})

    payload = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    hard = next(c for c in payload["compounds"] if c["compound_visual"] == 18)
    assert hard["wear"]["pct_per_lap"] == pytest.approx(3.5, abs=0.01)
    assert hard["feasible"] is True
    assert hard["plannable"] is False
    assert "base lap time" in hard["not_plannable_reason"]
    assert payload["plans_ranking"] == "time"
    assert all(plan["total_time_ms"] is not None for plan in payload["plans"])
    assert all(18 not in plan["compounds"] for plan in payload["plans"])


def test_derived_strategy_is_deterministic_and_json_ready() -> None:
    import json

    def build() -> dict[str, Any]:
        sessions, stints, laps, wear = practice_only_weekend()
        add_long_run(sessions, stints, laps, wear)
        return build_strategy(
            30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
        )

    first, second = build(), build()
    assert first == second
    assert json.loads(json.dumps(first)) == first

    def build_heuristic() -> dict[str, Any]:
        sessions, stints, laps, wear = practice_only_weekend()
        return build_strategy(
            30, sessions, stints, laps, wear, race_laps=14, race_laps_source="request"
        )

    assert build_heuristic() == build_heuristic()
    assert json.loads(json.dumps(build_heuristic())) == build_heuristic()


def test_strategy_finds_the_player_wherever_the_game_put_them() -> None:
    """Car 21 is where a real career session puts the player; car 0 is an AI."""
    sessions, stints, laps, wear = two_stint_race()
    mine = build_strategy(
        30, sessions, stints, laps, wear, race_laps=13, race_laps_source="request"
    )
    # The same rows relabelled onto car 0, with the session saying so, must model the same.
    moved_sessions = [dict(s, player_car_index=0) for s in sessions]
    moved_stints = {sid: [dict(r, car_index=0) for r in rows] for sid, rows in stints.items()}
    moved_laps = {sid: [dict(r, car_index=0) for r in rows] for sid, rows in laps.items()}
    moved = build_strategy(
        30, moved_sessions, moved_stints, moved_laps, wear, race_laps=13,
        race_laps_source="request",
    )
    assert [c["pace"] for c in mine["compounds"]] == [c["pace"] for c in moved["compounds"]]

    # ...and an AI's rows sharing the session must not be picked up as ours.
    with_ai = {sid: [*rows, *[dict(r, car_index=3) for r in rows]] for sid, rows in laps.items()}
    unchanged = build_strategy(
        30, sessions, stints, with_ai, wear, race_laps=13, race_laps_source="request"
    )
    assert unchanged["compounds"] == mine["compounds"]

# Phase 2 analysis API — contract

Read-only GET endpoints (the project invariant: no mutating HTTP surface, ever). All under
`/api`. JSON responses; times ms, distances metres, speeds km/h unless suffixed. `null` =
unknown. This document is the contract between the backend analysis module and the frontend
analysis pages; field names are frozen, additive changes only.

Existing (Phase 1): `GET /api/sessions?limit=`, `GET /api/sessions/{id}`,
`GET /api/sessions/{id}/laps?car_index=`.

## `GET /api/sessions/{id}/laps/{lap}/telemetry?car_index=<player default>`
One lap's trace for charts. Downsampling: none (rows are already 20 Hz).
```json
{"session_id": 3, "lap_number": 12, "car_index": 21, "points": 1180,
 "distance_m": [...], "speed_kmh": [...], "throttle": [...], "brake": [...],
 "steer": [...], "gear": [...], "rpm": [...], "drs_or_aero": [...],
 "session_time_s": [...]}
```
Arrays are parallel, ordered by `distance_m`. 404 if lap/session unknown; 503 if DB down.

## `GET /api/analysis/compare?session_a=3&lap_a=12&session_b=3&lap_b=15`
Two laps resampled onto a common distance grid (same-session or cross-session at the same
track; 422 if track ids differ).
```json
{"track_id": 2, "track_name": "...", "grid_m": [...],       // uniform grid, ~5 m step
 "a": {"session_id":3, "lap_number":12, "lap_time_ms": 92345,
       "speed_kmh": [...], "throttle": [...], "brake": [...], "gear": [...]},
 "b": { ... same shape ... },
 "delta_ms": [...],   // cumulative time delta a-vs-b along the grid (negative = a ahead)
 "sectors_a": [s1,s2,s3], "sectors_b": [s1,s2,s3]}
```

## `GET /api/analysis/corners?session_id=3&lap=12&ref=best`
Corner segmentation for one lap versus a reference (`ref=best` — session-best valid lap of
the player — or an explicit lap number).
```json
{"session_id": 3, "lap_number": 12, "ref_lap_number": 9,
 "corners": [
   {"n": 1, "entry_m": 210.0, "apex_m": 285.0, "exit_m": 350.0,
    "min_speed_kmh": 118.2, "ref_min_speed_kmh": 121.0,
    "brake_point_m": 155.0, "ref_brake_point_m": 162.5,
    "time_loss_ms": 142,     // positive = slower than ref through this corner
    "kind": "slow|medium|fast"},
   ...],
 "straights_time_loss_ms": 85,   // loss not attributable to any corner
 "total_delta_ms": 612}
```
Segmentation is computed from the 20 Hz samples (speed minima via prominence on a smoothed
speed-vs-distance curve; corner bounds where speed crosses local pre/post maxima thresholds).
Deterministic for a given lap; tuning constants live in one module with rationale comments.

## `GET /api/analysis/stints?session_id=3`
```json
{"session_id": 3, "stints": [
  {"stint_no": 1, "car_index": 21, "compound_visual": 16, "lap_start": 1, "lap_end": 14,
   "laps": [{"lap_number": 2, "lap_time_ms": 93120, "valid": true, "excluded": false}, ...],
   "fit": {"deg_ms_per_lap": 78.4, "base_ms": 92110, "r2": 0.91, "n_used": 11},
   "wear_end_pct": [31.0, 33.5, 20.1, 22.0]},
  ...]}
```
Fit is least-squares linear over valid, non-excluded laps (excluded = in/out laps, laps with
pit status, laps > 107% of stint median — flagged, not hidden).

## Frontend pages (SPA routes, consuming the above)
- `#/sessions` — session browser: table (date, track, type, laps, best lap), newest first.
- `#/sessions/{id}` — detail: lap table per car (player default), lap-time chart, stint strip;
  entry points to compare/corners.
- `#/compare?...` — uPlot overlay: speed/throttle/brake/gear vs distance + delta_ms pane;
  lap pickers (same session or same-track sessions).
- `#/corners?...` — corner table with time-loss bars + min-speed deltas; clicking a corner
  zooms the compare charts to that distance window (shared state with compare view).
- `#/stints?...` — lap-time scatter + fitted deg lines per stint, wear-at-end chips.
Live pit wall stays the default route (`#/` = current behavior, untouched).

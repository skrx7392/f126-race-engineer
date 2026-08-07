# Analysis API — contract (Phases 2–3)

Read-only GET endpoints (the project invariant: no mutating HTTP surface, ever). All under
`/api`. JSON responses; times ms, distances metres, speeds km/h unless suffixed. `null` =
unknown. This document is the contract between the backend analysis module and the frontend
analysis pages; field names are frozen, additive changes only.

Existing (Phase 1): `GET /api/sessions?limit=`, `GET /api/sessions/{id}`,
`GET /api/sessions/{id}/laps?car_index=`.

Everything below is scoped to **the player's car**, resolved from `participants.is_player`
and falling back to `sessions.player_car_index`. The player is *not* car 0 — in every real
career capture this recorder has taken they are car 21, and it varies between sessions.

## `GET /api/sessions/{id}/laps/{lap}/telemetry?car_index=<player default>`
One lap's trace for charts. Downsampling: none (rows are already 20 Hz).
```json
{"session_id": 3, "lap_number": 12, "car_index": 21, "points": 1180,
 "distance_m": [...], "speed_kmh": [...], "throttle": [...], "brake": [...],
 "steer": [...], "gear": [...], "rpm": [...], "drs_or_aero": [...],
 "session_time_s": [...]}
```
Arrays are parallel, ordered by `distance_m`, which is **strictly increasing**. The axis is
emitted rounded to centimetres, and at 20 Hz a car crawling off the grid or through the pit
lane puts two consecutive samples inside the same centimetre; those collisions are dropped
here (the later sample of each pair is kept, being the more recent observation of that point
on track, which also keeps `session_time_s` monotonic). `points` counts what is emitted.
404 if lap/session unknown; 503 if DB down.

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

## `GET /api/analysis/corners?session_id=3&lap=12&ref=best[&ref_session=7]`
Corner segmentation for one lap versus a reference.

| `ref` | Reference lap |
| --- | --- |
| `best` (default) | the player's fastest valid, telemetry-backed lap **in that session** |
| `track_best` | the same, widened to **every session at the same circuit** — a race lap against the weekend's quali benchmark |
| a lap number | that lap |

`ref_session` reads the reference out of another session instead of the subject's own; it
combines with `ref=best` ("that session's best") or an explicit lap number. A reference from
a different circuit is a **422**, matching `compare`.

Automatic resolution (`best`, `track_best`) never returns the subject lap itself — that
produced an all-zeros table that read like a real, perfectly-matched comparison. When the
subject is the only drawable lap, the corners still come back with every reference-derived
field `null` and `self_reference: true`. An explicit `ref=<subject lap>` is honoured as an
identity check but is still flagged.

Corner numbering is anchored to the **subject** lap: segmentation runs on its speed trace
alone and the reference is only read through those windows, so corner `n` means the same
corner regardless of which reference — or session — was chosen.
```json
{"session_id": 3, "lap_number": 12, "ref_lap_number": 9,
 "ref_session_id": 7,                       // may differ from session_id
 "ref_session_label": "One-Shot Qualifying · Aug 07",   // caption, null when same-session
 "self_reference": false,                   // true = nothing but the subject to compare to
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

## `GET /api/analysis/strategy?track_id=30[&race_laps=14]`
Pre-race fuel load and pit strategy for one circuit, computed from **that circuit's own
recorded running and nothing else**. There is no tyre database and no per-track table of pit
losses in this project: a compound nobody drove at this track comes back `untested` with
every model field `null`, and a plan that would need it is not offered. A missing input is
never a default.

Evidence comes in two grades and the payload always says which is in play. **Two laps** on a
set is a wear rate, which is enough to know how long that set lasts and therefore enough to
enumerate plans and say they finish — those come back ranked by a stated rule, with every
projected time `null` (`plans_ranking: "heuristic"`). **Four clean laps** on any one set at
the circuit additionally calibrates lap time against *cumulative wear*, giving a track-level
`ms_per_wear_pct` that every compound with a wear rate can be multiplied by to get a slope —
those plans are ranked on projected time as before (`plans_ranking: "time"`), with each
borrowed slope marked `source: "derived"` and naming both stints it was assembled from. A
compound with its own four-lap fit always keeps it.

`race_laps` omitted defaults to the most recent race-type session (`session_type` 15–17) at
this circuit that recorded its own `total_laps`. A circuit with sessions but no such race is
a **422** naming `race_laps`, because guessing a race length silently decides the stop count.
**404** when nothing at all was recorded at `track_id`; **503** if the DB is down.
`track_id` must be ≥ 0 — `-1` is the recorder's "the Session packet never landed" sentinel,
not a circuit.

```json
{"track_id": 30, "track_name": "Miami", "race_laps": 14,
 "race_laps_source": "Race 2 (session 229)",   // or "request"
 "wear_cliff_pct": 28.0,
 "wear_calibration": {                          // null when nothing here ran long enough
   "ms_per_wear_pct": 195.0, "r2": 0.6728, "laps_used": 6, "wear_span_pct": 29.0,
   "compound_visual": 16, "name": "Soft", "evidence": "race",
   "session_id": 229, "session_label": "Race 2 (session 229)",
   "stint_no": 1, "lap_range": [1, 7],
   "assumption": "time lost per percent of worst-wheel wear is treated as the same ..."},
 "compounds": [
   {"compound_visual": 16, "name": "Soft", "dry": true, "untested": false, "stints_seen": 2,
    "evidence": "race",                        // "race" | "practice" | null
    "pace": {"base_ms": 88643.2, "deg_ms_per_lap": 1162.4, "r2": 0.6924, "laps_used": 6,
             "evidence": "race", "session_id": 229, "session_label": "Race 2 (session 229)",
             "stint_no": 1, "lap_range": [1, 7], "source": "fit"},
    "wear": {"pct_per_lap": 5.87, "source": "wear_samples", "laps": 6, "evidence": "race",
             "session_id": 229, "session_label": "Race 2 (session 229)", "stint_no": 1},
    "max_stint_laps": 4, "projected_wear_at_max_pct": 23.5,
    "feasible": true, "plannable": true, "not_plannable_reason": null},
   {"compound_visual": 17, "name": "Medium", "dry": true, "untested": false, "stints_seen": 1,
    "evidence": "race",
    // No four-lap run of its own, so the slope is the track coefficient × its wear rate.
    "pace": {"base_ms": 88665.0, "deg_ms_per_lap": 735.1, "r2": null, "laps_used": 3,
             "evidence": "race", "session_id": 229, "session_label": "Race 2 (session 229)",
             "stint_no": 1, "lap_range": [1, 7], "source": "derived",
             "base_source": {"source": "median_clean_laps", "laps": 3, "evidence": "race",
                             "session_ids": [229]},
             "derived_from": {
               "ms_per_wear_pct": 195.0, "wear_pct_per_lap": 3.77,
               "calibration": {"compound_visual": 16, "name": "Soft", "evidence": "race",
                               "session_id": 229, "session_label": "Race 2 (session 229)",
                               "stint_no": 1, "lap_range": [1, 7], "laps_used": 6,
                               "r2": 0.6728, "wear_span_pct": 29.0},
               "wear": {"source": "wear_samples", "laps": 6, "evidence": "race",
                        "session_id": 229, "session_label": "Race 2 (session 229)",
                        "stint_no": 2}}},
    "wear": {"pct_per_lap": 3.77, "source": "wear_samples", "laps": 6, "evidence": "race",
             "session_id": 229, "session_label": "Race 2 (session 229)", "stint_no": 2},
    "max_stint_laps": 7, "projected_wear_at_max_pct": 26.4,
    "feasible": true, "plannable": true, "not_plannable_reason": null},
   {"compound_visual": 18, "name": "Hard", "dry": true, "untested": true, "stints_seen": 0,
    "evidence": null, "pace": null, "wear": null, "max_stint_laps": null,
    "feasible": false, "plannable": false,
    "not_plannable_reason": "no stint on this compound was recorded this weekend"}],
 "plans": [
   {"rank": 1, "stops": 1, "compounds": [16, 18], "label": "Soft → Hard",
    "stints": [{"compound_visual": 16, "name": "Soft", "lap_start": 1, "lap_end": 4,
                "laps": 4, "projected_end_wear_pct": 23.5}, ...],
    "total_time_ms": 1253110.0, "delta_to_best_ms": 0.0,   // both null when heuristic
    "pit_windows": [{"stop": 1, "planned_lap": 4, "earliest_lap": 2, "latest_lap": 4,
                     "window_laps": 2}],
    "safety_car": {"flexibility": "flexible", "note": "stop 1 can be taken anywhere ..."}}],
 "plans_considered": 6,
 "plans_ranking": "time",                       // "time" | "heuristic" | null
 "plans_ranking_note": "ranked on projected race time: every compound in these plans ...",
 "fuel": {"kg_per_lap": 1.064, "laps_measured": 17, "evidence": "race",
          "session_ids": [196, 229], "race_laps": 14, "margin_laps": 0.45,
          "slider_laps": 14.45, "recommended_kg": 15.37},
 "pit_loss_s": 16.52,
 "pit_loss": {"seconds": 16.52, "source": "measured", "stops_measured": 2,
              "stops": [{"session_id": 229, "stop_after_stint": 1, "laps": [7],
                         "loss_s": 13.17}, ...],
              "detail": "median of 2 stop(s) measured at this circuit, ..."},
 "sessions_used": [{"id": 229, "session_type": 16, "session_type_name": "Race 2",
                    "evidence": "race", "started_at_wall": 1786129983.9,
                    "contributed": ["fuel burn", "pit-lane loss", "soft pace", "soft wear"]}],
 "omitted": {}}
```

| Field | Meaning |
| --- | --- |
| `wear_cliff_pct` | Max-wheel wear treated as the performance cliff. **Telemetry percent**, which is roughly *half* what the in-game display shows; the ratio is uncalibrated and is never applied to a computed number. |
| `compounds[].untested` | No stint on this compound at this circuit. Every model field is `null`. All three dry compounds are always listed, so an untested one is a visible fact rather than a missing row. |
| `compounds[].evidence` | Tier of the weaker of the two models below it. `race` = session type 10–17 (the races and the sprint weekend's sessions); `practice` = types 1–9. Race trim always wins the tie-break when both exist, because practice degradation and wear run at roughly double race rate. |
| `wear_calibration` | The circuit's **wear-to-time coefficient**: one stint's lap time regressed against *cumulative worst-wheel wear* instead of lap number, so the slope is ms lost per percent of wear rather than per lap. Chosen from the stints with ≥ 4 clean laps (same exclusion rules as the fit) and ≥ 2 % of wear covered, race trim preferred, dry slicks only. `null` when no stint qualified, with the reason in `omitted.wear_calibration`. `ms_per_wear_pct_planned` appears (as `0.0`) when the measured coefficient was negative and planning clamped it. `assumption` states, in the payload, the thing this rests on: that a percent of wear costs the same lap time on all three dry compounds here. |
| `compounds[].pace` | `source: "fit"` — the `stints` endpoint's own line through this compound's laps against tyre age, taken from the best-evidenced stint, not recomputed. `source: "derived"` — `wear_calibration.ms_per_wear_pct × wear.pct_per_lap`, for a compound with a wear rate and no four-lap run of its own; `r2` is `null` (nothing was fitted to these laps), `base_ms` is the **median of this compound's clean laps at this circuit** (`base_source`; a stint average, not an age-zero pace, and not fuel-normalised), and `session_id`/`stint_no`/`lap_range` describe the *calibration* stint, because that is where the slope was measured. `derived_from` names both parents — the calibration stint and the wear-rate stint. A direct fit always beats a derived slope for the same compound. `deg_ms_per_lap_planned` appears (as `0.0`) only when the slope was negative and the planner clamped it. |
| `compounds[].wear` | Worst-wheel %/lap. `source` is `wear_samples` (least-squares over the stint's own 1 Hz samples) or `stint_end_wear` (end reading ÷ laps run) when samples are missing. |
| `compounds[].max_stint_laps` | `floor(wear_cliff_pct / wear.pct_per_lap)`, capped at `race_laps`. `null` without a wear rate. |
| `compounds[].feasible` | Dry, with a measured wear rate: enough to know how long a set of it lasts, and therefore enough to put it in a plan and say that plan finishes. Two laps on a set is enough. |
| `compounds[].plannable` | `feasible`, plus a pace model (fitted or derived) — enough to rank a plan on projected time. `not_plannable_reason` is populated whenever this is false, and says whether the compound is still usable for feasibility. |
| `plans` | Every legal 1- and 2-stop ordering. Legal = at least two distinct dry compounds (FIA two-compound rule) and every stint inside its wear cap. Capped at 12 returned; `plans_considered` is the full count. Empty means no legal plan — `omitted.plans` says why. |
| `plans_ranking` | How the returned set was ordered, because the two orderings are not comparable. `time`: every compound in the set has a pace model, plans are ranked on `total_time_ms`, and rank 1 is the fastest. `heuristic`: at least one compound has a wear rate and no pace model, so **no time is projected for any plan in the set** — `total_time_ms` and `delta_to_best_ms` are `null` — and the order is fewest stops first, then the softer compound (lower visual number) earlier. `null` only when `plans` is empty. Time ranking is attempted first and kept whenever it yields anything, over the compounds that have pace models; the feasibility set is the fallback, so gaining a wear rate never costs a circuit its ranked plans. `plans_ranking_note` is the rule in words. |
| `plans[].total_time_ms` | Sum of per-lap times from the degradation models, plus `pit_loss_s` per stop. Stint lengths are the allocation that minimises this, exactly (the objective is convex and separable). `null` under heuristic ranking, where lap counts are instead split so no stint ends closer to the wear ceiling than any other. Pit windows, projected wear and the safety-car note are real in both modes. |
| `plans[].pit_windows` | Per stop: the planned box lap and the earliest/latest lap it could be taken on while still covering the race inside every wear cap. Exact feasibility bounds, not heuristics. |
| `plans[].safety_car` | `flexible` when some stop's window is ≥ 3 laps wide (a safety car inside it is close to a free stop), `tight` when the wear ceiling fixes when you box, `none` with no stop left. |
| `fuel` | `kg_per_lap` is the mean of `fuel_start_kg - fuel_end_kg` over the laps the stint fit did **not** exclude — i.e. racing laps, not in/out/deleted ones. `recommended_kg = (race_laps + margin_laps) × kg_per_lap`; `slider_laps = race_laps + margin_laps`, which is the unit the game's fuel slider uses. `null` when fewer than 3 laps carried both tank readings, with the reason in `omitted.fuel`. |
| `pit_loss` | `measured`: the pit lap's excess over the driver's own clean-lap median in the same race, median over every stop found at this circuit. `default`: a named constant, used only when the circuit has no recorded stop, and always flagged. |
| `sessions_used` | Every session read, in start order, with `contributed` naming what each one supplied. A session read that gave nothing appears with an empty list. |
| `omitted` | Section name → why it could not be computed. Keys seen here: `fuel`, `plans`, `wear_calibration` (the last only when some compound would have used a derived slope and no stint could calibrate one). Present sections are always real numbers; a missing input never becomes a zero. |

Deterministic for a given `(track_id, race_laps)` and a given database state. Sessions are
deduplicated to one row per `session_uid` (the same rule `GET /api/sessions` uses), so a
race split across recorder segments counts its fuel and its stops once.

## `GET /api/sessions/{id}/debrief`
The stored post-session debrief. **404 when the session has none**, which is the ordinary
state of a session that was just recorded rather than an error; 503 if the DB is down.
```json
{"id": 7, "session_id": 102, "created_at": 1770002000.0,
 "model": "example-model:latest", "prompt_version": 1,
 "fact_sheet": { ... },          // the deterministic input, see below
 "text": "P3 from P7, and the race was won in the first stint..."}
```
Append-only: regenerating writes a new row and this returns the newest, so a bad generation
supersedes rather than destroys the one before it.

`text` is **prose an LLM wrote from `fact_sheet` and nothing else**. Every number in the
fact sheet was computed by `f126.analysis.factsheet` out of the tables the endpoints above
read; the system prompt forbids the model from calculating, converting or estimating any
figure that is not already there. Storing both is what makes a debrief auditable — any claim
in the text can be checked against the numbers it was handed. Render `text` as plain text
(`white-space: pre-wrap`); it is never markdown or HTML.

Fact-sheet shape (sections are omitted when they could not be computed, and the reason is
recorded under `omitted` — a missing input never becomes a zero):

| Section | Contents |
| --- | --- |
| `units` | what every suffix and sign convention means, restated for the model |
| `session` | track, type, start time, driver, scheduled laps, duration, `is_race` |
| `result` | **race only** — finish/grid position, places gained, points, pit stops, status |
| `standing` | **non-race only** — position on the timing sheet, field size |
| `pace` | lap counts, personal best + sectors, median/IQR over representative laps, and which laps were excluded from that and why |
| `stints` | per stint: compound, lap range, laps on the set, degradation slope with r² and a `degradation_confidence` word |
| `corners` | the best lap against the session's next-best lap *and* against the circuit benchmark; top 3 time-loss corners each, with min-speed and brake-point deltas |
| `fuel` | median burn per lap, laps measured, laps dropped as garage refuels |
| `events` | flashbacks, safety cars, penalties, collisions, pit stops |
| `weather` | conditions, track/air temperature, rain probability ahead |

Corner numbers in the sheet are **detected braking zones in track order, not the circuit's
official turn numbers** — segmentation finds 8 zones on Jeddah's 27-turn lap. The sheet says
so in-band and the prompt forbids "Turn N" phrasing; corners are named by `apex_m`.

**There is no route that generates a debrief.** The HTTP surface stays read-only. Generation
happens in the serve process when a session closes with reason `finished` (a fire-and-forget
task off the capture path, skipped when the feature is unconfigured), and on demand from
`f126 debrief <session_id> [--regenerate]`.

## Phase 3 — career (season progress, per-track evolution, PBs)

Career sessions are grouped into **weekends** and **seasons** with no manual input, and a
`career_tags` table (keyed by `session_uid`, so a backfill re-derivation never loses it;
written only by `f126 tag`, never by HTTP) can pin a season/round where the derivation is
wrong. Derivation rules, stated here and in the payload:

* Sessions considered: one per `session_uid` (the session-list dedup rule), `track_id >= 0`,
  time-trial sessions excluded (they are not career rounds; they still count for PBs).
* **Weekend** = maximal chronological run of sessions sharing `track_id`, split when the gap
  between consecutive sessions there exceeds 48 h.
* **Round** = 1-based chronological index of the weekend within its season.
* **Season** starts at 1 and increments when a weekend starts at a `track_id` already
  visited that season (a 24-round career never repeats a circuit inside one season).
* A tag on any session pins its weekend's `(season, round)`; later weekends derive forward
  from the pinned value. Disagreeing tags inside one weekend: newest `updated_at` wins and
  the weekend carries `tag_conflict: true`.
* In a weekend with more than one race-type session (15–17), the **last is the grand prix**
  (`race`), the earlier ones are sprints (`sprint` is the last of those); with exactly one,
  it is the `race`. `quali` is the weekend's last session of type 5–9 (the GP qualifying —
  shootouts 10–14 set the sprint grid and are not poles).
* Totals: `points` sums the game's classification points over race-type sessions (sprint
  included); `wins`/`podiums`/`fastest_laps` count the GP only; `sprint_wins` is separate;
  `poles` = `quali.position == 1`. Nothing is inferred when a classification packet is
  missing — those fields are `null` and the session still appears.
* **Consistency** = the representative-lap rule the fact sheet already uses (valid,
  non-in/out, non-excluded, within 107 % of the median): `laps_used`, `median_ms`,
  `iqr_ms`, and `cv_pct` (sample stdev / mean × 100). Race laps for a weekend's headline
  figure; per-session on the track page.

### `GET /api/career/overview`
```json
{"seasons": [
  {"season": 1, "rounds": 4,
   "totals": {"points": 73, "wins": 2, "podiums": 3, "poles": 3, "fastest_laps": 1,
              "sprint_wins": 2, "races": 4},
   "weekends": [
     {"season": 1, "round": 2, "track_id": 30, "track_name": "Miami",
      "format": "sprint",                    // "sprint" | "standard"
      "started_at_wall": 1786100000.0, "ended_at_wall": 1786140000.0,
      "session_ids": [180, 196, 210, 229, 240],
      "sessions": [{"id": 180, "session_type": 1, "session_type_name": "Practice 1",
                    "started_at_wall": 1786100000.0, "best_lap_ms": 89012}, ...],
      "quali":  {"session_id": 210, "position": 1, "best_lap_ms": 87274},
      "sprint": {"session_id": 196, "position": 1, "grid_position": 12, "points": 8,
                 "status": "finished"},
      "race":   {"session_id": 229, "position": 1, "grid_position": 1, "points": 25,
                 "pit_stops": 1, "best_lap_ms": 88643, "fastest_lap": true,
                 "status": "finished"},
      "points": 33,
      "consistency": {"session_id": 229, "laps_used": 11, "median_ms": 89120,
                      "iqr_ms": 410, "cv_pct": 0.38},
      "tags": null,                          // or {"season": 1, "round": 2, "note": "..."}
      "tag_conflict": false},
     ...]}],
 "career_totals": { ...same shape as totals, summed over seasons... },
 "pbs": [
   {"track_id": 30, "track_name": "Miami", "best_lap_ms": 87274, "session_id": 210,
    "session_label": "One-Shot Qualifying · Aug 07", "lap_number": 1,
    "compound_visual": 16, "compound_name": "Soft", "set_at_wall": 1786120000.0,
    "theoretical_ms": 87100, "top_speed_kmh": 342.0},
   ...],
 "untracked_sessions": 2,
 "notes": {"weekend_rule": "...", "season_rule": "..."}}
```
`pbs` covers every circuit with laps (time trial included), fastest first by nothing —
track order is chronological first-visit. `theoretical_ms` is the sum of the circuit's best
valid sectors, `null` when any sector is missing. Weekends are chronological within a
season; seasons ascend. 503 if the DB is down; an empty database returns empty arrays, not
an error.

### `GET /api/career/tracks/{track_id}`
Per-track progress: every visit, every session, and the PB detail. **404** when the circuit
has no sessions; `track_id >= 0` as everywhere else.
```json
{"track_id": 30, "track_name": "Miami",
 "pb": { ...the overview `pbs` entry..., 
   "sectors": {"s1_ms": 28450, "s1_session_id": 210, "s1_lap_number": 1,
               "s2_ms": ..., "s2_session_id": ..., "s2_lap_number": ...,
               "s3_ms": ..., "s3_session_id": ..., "s3_lap_number": ...}},
 "visits": [
   {"season": 1, "round": 2, "started_at_wall": 1786100000.0,
    "session_ids": [180, 196, 210, 229],
    "best_lap_ms": 87274, "best_lap_session_id": 210,
    "quali": {...}, "sprint": {...}, "race": {...},        // same shapes as overview
    "consistency": {...}},
   ...],
 "sessions": [
   {"id": 180, "session_type": 1, "session_type_name": "Practice 1",
    "started_at_wall": 1786100000.0, "best_lap_ms": 89012, "laps_total": 9,
    "laps_used": 6, "median_ms": 89500, "iqr_ms": 620, "cv_pct": 0.55,
    "top_speed_kmh": 341.0},
   ...]}
```
`visits` are the career weekends at this circuit (chart series come from here — the page
derives them, the payload does not duplicate them); `sessions` is every session at the
circuit including time trials, chronological, with per-session consistency.

### `f126 tag` (CLI, not HTTP)
`f126 tag <session_id> --season N [--round M] [--note TEXT]` upserts the row for that
session's `session_uid`; `f126 tag <session_id> --clear` deletes it; `f126 tag --list`
prints every tag. Direct database write from the CLI, same DSN the service uses — the HTTP
surface stays read-only.

## Frontend pages (SPA routes, consuming the above)
- `#/sessions` — session browser: table (date, track, type, laps, best lap), newest first.
- `#/sessions/{id}` — detail: lap table per car (player default), lap-time chart, stint strip;
  entry points to compare/corners.
- `#/compare?...` — uPlot overlay: speed/throttle/brake/gear vs distance + delta_ms pane;
  lap pickers (same session or same-track sessions).
- `#/corners?...` — corner table with time-loss bars + min-speed deltas; clicking a corner
  zooms the compare charts to that distance window (shared state with compare view).
- `#/stints?...` — lap-time scatter + fitted deg lines per stint, wear-at-end chips.
- `#/strategy?track={id}[&laps={n}]` — the pre-race sheet: fuel load in kilograms *and* in
  slider laps, ranked plan cards (stint bars at true lap width, projected loss against the
  best plan, pit window, safety-car note), the per-compound model table with its evidence
  tier and untested rows, and a provenance footer naming every session it was built from.
  The plan section carries a **ranking badge** — "ranked on pace models" or "ranked by rule
  — no pace model yet; times not projected" — and a rule-ranked set shows an em-dash where
  each plan's race time and delta would be. The degradation column marks a **derived** slope
  beside the figure, so it cannot be read as a line fitted to that tyre, and the provenance
  footer states the circuit's coefficient and the assumption it rests on.
  Track-scoped, not session-scoped: the picker lists circuits that have sessions, and both
  the sessions browser and the session detail page link into it by track.
- `#/career` — season overview: a totals strip (points, wins, podiums, poles, fastest laps,
  sprint wins) per season and career-wide, a chronological weekend table (round, track,
  format badge, quali/sprint/race results, points, race consistency) linking each row to
  its track page, and the PB board (per circuit: best lap, compound, theoretical best, top
  speed). Derivation notes are shown, not hidden — the season/round numbers are computed
  and the page says by what rule; a pinned tag is marked.
- `#/career/tracks/{track_id}` — one circuit's progress: PB card with the three best
  sectors and where each was set, a uPlot evolution chart of best and median lap per visit
  (x = "S1 R2"-style visit labels), the visit table, and the per-session consistency table.
Live pit wall stays the default route (`#/` = current behavior, untouched).

The session detail page also carries a collapsible **Debrief** card above the instruments,
captioned `model · generated at`. A session without one says "No debrief yet — run: f126
debrief {id}" rather than rendering the not-found error panel: absence is normal here.

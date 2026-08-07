# Autonomous decisions log

Decisions I (Claude) made without asking, for the repo owner's later review. Newest last.
Reversals welcome — flag anything and I'll adjust.

## 2026-08-06 — implementation kickoff

1. **Python 3.14.6 chosen** as runtime: latest GA (3.15 is beta, excluded per the
   latest-GA-releases rule). Pins: FastAPI 0.141.1, psycopg 3.3.4, Svelte 5.56.8, Vite 8.2.1.
2. **Spec source order changed for autonomy:** parser is transcribed from the two reference
   implementations (MacManley/f1-25-udp C headers for 2025; volodymyr-fed/F1Game.UDP C# for
   2026 incl. CarTelemetry2) instead of blocking on extracting the EA forum post through your
   browser (bot-blocked, needs interactive Chrome). Byte-size pin tests still enforce exact
   wire sizes; the golden fixture from your first real session is the final validation.
   Extracting the official EA doc into docs/ stays open as a follow-up.
3. **PR-requirement rule removed from the main ruleset** (you said ignore PRs in greenfield).
   Force-push and branch-deletion protection kept. Re-add the pull_request rule when the
   greenfield phase ends — noted as a follow-up.
4. **Module contracts frozen before fan-out:** `src/f126/types.py` (parser→state seam) and
   `docs/ws-protocol.md` (state→web→frontend seam) written by the orchestrator so seven
   parallel subagents (Opus 5) can build modules without file conflicts.
5. **Workstream split:** parser / capture+replay / state / store / web-backend / frontend /
   deploy+CI+README — strictly disjoint file ownership; integration wiring (`main.py`) done
   at merge time by the orchestrator, one commit per workstream.
6. **ghcr package will be flipped public immediately after the first image push** (GitHub
   cannot set visibility on a package that doesn't exist yet).
7. **`f126` database + role created on the shared Postgres** (postgres namespace) with a
   generated password stored only as a k8s Secret in the new `f126` namespace; the role gets
   rights on its own database only (database owner, no superuser/createrole).
8. **Post-build review round (Opus reviewer, 15 findings, all addressed or accepted):**
   fixed the raw_file off-by-one blocker + 11 hardening items (commit af3e9fc). Two
   accepted-as-is: WS over-cap clients still get a polite accept-then-1013 close (the
   flood concern is bounded by uvicorn limit_concurrency=64 instead — preserves the
   client-visible backoff signal); /api/sessions/{id}'s per-hit sample counts stay
   (trivial at our row counts, revisit if it ever shows in metrics). CI tests against
   Postgres 18 (latest GA) while the deployment target runs 16.14 — accepted: schema floor
   is PG15 (NULLS NOT DISTINCT), verified working on 16.14 at deploy.
9. **Shared-Postgres access detour (transparency):** bootstrapping the `f126` role on a
   pre-existing shared Postgres needed a temporary, unix-socket-only auth relaxation, since
   reverted and verified — details in `decisions-private.md` (local, gitignored: the entry
   describes one specific cluster, not this project).

## 2026-08-06 — history rewrite

10. **Git history rewritten** (git-filter-repo, run by the owner; two passes) to replace all
    environment-specific values — hostnames, LAN addresses, cluster identifiers, database role
    names — with placeholders across every blob and commit message. Force-pushed with the
    branch protection ruleset lifted for the push and restored immediately after. All 18
    commits re-hashed; working tree and test suite unaffected.

## 2026-08-07 — first real race (Jeddah, 13 laps, format 2026)

11. **Menu packets no longer wipe the parser's 2026 merge cache.** `PacketParser` reset its
    `Extras2026` cache on any `session_uid` change, including the `uid 0` packets menus and
    loading screens emit — the same packets the session lifecycle already ignores outright
    (decision 7). After any menu bounce, every CarTelemetry decoded before the next CarStatus
    carried `energy_store_j=None`, so the pit-wall energy panel went dark and stayed dark.
    The cache now ignores `uid 0` exactly as the tracker does; a genuine session change still
    clears it. Measured on the Jeddah capture: 230 menu packets, 9 uid flips.

12. **2026 per-lap harvest/deploy energy now reaches the slow frame.** `decode_status` nulls
    the ERS fields on 2026 (the energy model lives on `TelemetryView` there) but only the
    store and deploy mode were forwarded through the merge cache — `harvested_lap_j` and
    `deployed_lap_j` were therefore `None` in *100%* of slow frames on a 2026 session. Both
    are now carried across with the other two. Verified against the race capture: 100% null
    before, 2% after (the 25 s before the first CarStatus, same as every other energy field).

13. **Tyre age is the game's own counter, not a stint-derived one.** Measured on the race
    capture, `CarStatus.tyre_age_laps` is *laps completed on the set*: 0 for the whole lap
    the tyres are fitted on, 7 while lap 13 runs on a set fitted during lap 6. It is passed
    through unmodified — recomputing it would put the dashboard out of step with the number
    the game shows the driver. The apparent off-by-one was the *stint strip*, not the age.

14. **A pit lap belongs to the stint it started on.** The in-lap is driven on the old set and
    the out-lap on the new one, so a raw `lap_start`/`lap_end` pair shares its boundary lap
    with the previous stint: "Soft 1–6 | Medium 6–13" counted lap 6 twice and implied an
    8-lap stint on a set that had 7 laps on it. Ranges are now normalised for display only
    (`displayStintRanges`) so they are contiguous and non-overlapping — "Soft 1–6 | Medium
    7–13" — and the counts agree with the tyre age. Stored data and the degradation fit are
    unchanged; this is a presentation convention, applied in both render sites.

15. **The telemetry endpoint emits a strictly-increasing distance axis.** The trace is already
    monotonic in float metres, but the wire format rounds to centimetres and slow running
    (standing start, pit-lane crawl, safety car) collapses neighbouring 20 Hz samples onto one
    value — real race laps carried up to 11 such duplicates. Duplicates are dropped after
    rounding, keeping the later sample of each pair. Charts bisect and key on this axis, so a
    non-monotonic one is invalid input, not a cosmetic wrinkle.

16. **Corner references never silently resolve to the subject lap**, and can now come from
    another session at the same circuit (`ref=track_best`, `ref_session=`). `ref=best` on a
    session's own fastest lap used to return that lap and render a full table of ±0.000 that
    read like a real result; automatic resolution now excludes the subject and reports
    `self_reference` when there is genuinely nothing else. Corner numbering is anchored to the
    subject lap alone — the reference is read through the subject's windows, never segmented
    itself — so corner `n` is stable across reference sessions. Additive response fields only.

17. **Keyed `{#each}` blocks over server data are keyed by index.** The chart legend was keyed
    on the series label, and two series share a label whenever a lap is charted against itself
    (`"S102 L3"` twice, and each pedal label twice over) — Svelte threw `each_key_duplicate`
    and took the whole trace pane down. Series are a fixed-length positional array, so the
    index *is* the identity. Same fix for the weather forecast strip, keyed on `offset_min`,
    which the game can repeat.

## 2026-08-07 — Phase 2.5, the post-session debrief

18. **The debrief is grounded, and the split is enforced in two places.** A deterministic
    builder (`analysis/factsheet.py`) computes every number from the recorded tables; the
    LLM is given that dict and told, in the system prompt, that it may not calculate,
    convert or estimate any figure that is not already in it. Both halves are stored on the
    `debriefs` row, so every sentence is checkable against the numbers it was handed. The
    fact sheet is a pure function of the rows — no clocks, no unordered iteration, floats
    rounded on the way out — and a test asserts two consecutive builds are byte-identical.

19. **No LLM anywhere near the live loop.** Generation happens in exactly two places: a
    fire-and-forget task when a session closes with reason `finished`, and `f126 debrief
    <id>`. The task flushes the DB writer first (the close callback fires before the final
    classification is committed, and a sheet built from a half-written session would be
    confidently wrong), logs and swallows every failure, and is cancelled rather than
    awaited at shutdown. **No mutating HTTP route was added** — a POST would have been
    convenient and would have cost the invariant that lets this dashboard sit on the open
    internet. `GET /api/sessions/{id}/debrief` reads; 404 means "not written yet", which the
    UI states in its own words instead of rendering the not-found error panel.

20. **Schema v2 adds `debriefs`, append-only.** Regeneration inserts a new row and readers
    take the newest, so a bad generation supersedes rather than destroys the one before it.
    `schema.sql` is `CREATE ... IF NOT EXISTS` throughout, so the v1→v2 upgrade needed no
    ALTER. `ON DELETE CASCADE`, like every other derived table: Postgres stays re-derivable
    from the raw captures.

21. **Four real-data bugs found by building the sheet against the actual race, and fixed.**
    Session 102 and the two qualis were run through the builder read-only against the live
    database before this shipped, which is the only reason these were caught:
    - the game writes a final-classification packet for *qualifying* too, carrying a stale
      `points: 15` and a `total_race_time_s` that is really just the lap time — the sheet
      claimed the driver had scored 15 championship points for one flying lap. `result` is
      now race-gated; non-race sessions get a narrow `standing` block instead.
    - `time_s: 255` on a lap-1 warning was reported as a 255-second penalty. 255 is the
      game's 0xFF "not applicable" byte; every optional u8 in an event payload now reads
      back as absent.
    - `grid_position: 0` (the "no grid slot" sentinel) survived the None-filter and read as
      "started from P0".
    - a practice session with five compound changes reported "4 pit stops". The stint-count
      fallback is now race-only; outside a race the field is omitted rather than guessed.

22. **`degradation_confidence` is in the sheet as a word, not left as an r².** Measured on
    the real race, both stints fitted at r² ≈ 0.003 and rounded to `0.00` at two decimals,
    making "no correlation at all" indistinguishable from "weak". r² is now carried to three
    decimals *and* accompanied by strong/fair/weak on the same bands the stints page renders
    (`analysis-format.ts::fitConfidence`), and the prompt forbids quoting a weak slope as
    degradation. Without this a debrief would state "your softs gained 0.73 s per lap".

23. **Corners are named by distance, never as "Turn N".** `analyse_corners` segments braking
    zones by speed prominence and numbers them in track order — that is 8 zones across
    Jeddah's 27 turns, so "corner 8" is the eighth *detected* zone. A debrief saying "Turn 8"
    sends the driver to the wrong corner, so the sheet carries the caveat in-band next to
    every comparison and the prompt requires the `apex_m` distance instead.

24. **`httpx` promoted from a dev dependency to a runtime one.** It was already in the
    lockfile (FastAPI's test client pulls it) but the shipped image installs only the main
    list, so `f126.llm` importing it would have failed in the pod and nowhere else. No new
    library was added — the OpenAI dialect is four fields of JSON over one POST.

25. **The in-cluster proxy URL lives only in gitignored files.** `deploy/.env` and
    `deploy/local/kustomization.yaml` carry the real service name; the tracked base ships no
    LLM env at all, which leaves the feature disabled by default in a fresh clone — the
    right default for a public repo. Worth recording: that proxy mounts the OpenAI surface
    under `/api/v1`, and plain `/v1` returns 404, so the configured base URL includes the
    prefix. Cluster access for this work was strictly read-only; nothing was applied.

## 2026-08-07 — Phase 2.6: pre-race strategy

26. **The reported lap-context bug does not exist as described, and the real one is a
    flashback.** The hypothesis was that `RowBuilder._player_index` is never updated from
    `PacketHeader.player_car_index`, so `record.car_index == self._player_index` never
    matches a real player at car 21. It *is* updated (`rows.py`, inside `feed()`), and the
    live database agrees: sessions 196 and 229 both carry compound, fuel, tyre age and top
    speed on all of the player's laps at `car_index = 21`. Reading the whole table, the only
    sessions with NULL context are 4 and 44 — and both are `joined_in_progress`, where the
    laps arrive through SessionHistory for running that happened before the recorder
    attached and there genuinely is no fuel or top speed to attach. Two real defects were
    found in the same code and fixed:
    - **`_player_index` was one packet stale relative to the callbacks that read it.**
      `StateBundle.feed()` runs `tracker.feed()` first (deliberately — lifecycle callbacks
      must land before the packet is written out), and the tracker's lap ledger publishes
      completed laps through `on_lap()` from *inside* that call. So `on_lap()` read an index
      derived from the *previous* packet, which on the process's very first packet is the
      initialisation value 0. A backfill whose raw log opens on a SessionHistory does hit
      this. Fixed at the root: `RowBuilder.observe_header()` absorbs the header and nothing
      else, and `StateBundle.feed()` calls it before the tracker so the index is never
      behind its own callbacks.
    - **A flashback left stale per-lap context, which was then written under the new
      generation.** Exactly the bug commit `aed1599` fixed for the stint wear peak, in the
      same file, not carried across to `_lap_context`: the fuel, top speed and ERS totals
      accumulated since the last crossing describe running that has been undone, and the
      re-driven lap was re-emitted carrying the abandoned timeline's numbers. Because
      `generation` is part of the laps primary key this is a *new row*, so the writer's
      COALESCE could not repair it. `on_generation` now clears the accumulators (cleared,
      not re-seeded from `self._status`, whose tank is the pre-rewind reading) and the
      rewind branch of `_on_lap_view` drops every snapshot at or after the lap being
      re-driven. This directly protects the fuel-burn input Phase 2.6 depends on.
    Regression tests cover all three: a two-car field driven end to end with
    `player_car_index = 21` (the player's laps carry context, car 0's carry none), a capture
    that opens on a SessionHistory, and a flashback whose re-driven lap must report the fuel
    and top speed it was actually re-driven on.

27. **The strategy engine measures everything or omits it — there is no tyre database.**
    `analysis/strategy.py` is pure (rows in, dict out, like `stints.py`) and every number in
    its output was measured at the circuit being planned for. A compound with no stint comes
    back `untested: true` with null model fields and is excluded from plan enumeration; a
    compound with a stint too short to fit a line through says so in
    `not_plannable_reason`. Validated read-only against the live database: Jeddah softs
    6.11 %/lap, Miami sprint softs 5.87 %/lap, Miami hards 2.88 %/lap, Miami fuel 1.064
    kg/lap, Jeddah fuel 1.26 kg/lap — all inside the bands measured by hand from the same
    races. Miami honestly produces **no plan at all** (only the soft ran long enough to
    model, and the two-compound rule needs two), which is the correct answer for that
    weekend and is stated in `omitted.plans` rather than papered over.

28. **Degradation is imported from `stints.py`, not re-derived.** The strategy calls
    `build_stints()` per session and reads its `fit`, so the slope on the strategy page and
    the slope on the stints page are the same number computed once, with the same 107 %
    outlier rule and the same four-lap floor. The one rule both modules needed —
    which laps of a stint are shared with a pit lane — was extracted into
    `stints.pit_flags()` rather than copied, because the two disagreeing about it would put
    the pit lap's wear into a per-lap rate.

29. **`WEAR_CLIFF_PCT = 28.0`, from the telemetry and not from the game's display.** The
    softs collapsed at 28–31 % max-wheel wear in two races at two circuits; 28 is the bottom
    of that band, so a stint planned to it stops *before* the collapse. The in-game OSD
    reads roughly double the telemetry figure, and that ratio is an eyeball comparison, not
    a calibration — so it is never applied to a computed number. The UI shows it once, in
    parentheses, smaller, always beside the real figure (`analysis-format.ts::inGameWear`,
    `IN_GAME_WEAR_FACTOR = 2`).

30. **A negative fitted degradation slope is clamped to zero for planning only.** Short
    stints on a light car fit negative sometimes — fuel burning off and the track rubbering
    in showing through — and projecting that forward recommends running one set to the flag.
    The measured slope is reported unchanged in `pace.deg_ms_per_lap`; the clamp is a
    separate, named field (`deg_ms_per_lap_planned`) so the two cannot be confused.

31. **Stint lengths are allocated exactly, not heuristically.** A stint of `L` laps costs
    `L·base + deg·L(L-1)/2`, which is convex in `L` for a non-negative slope, so handing
    laps out one at a time to whichever stint's *next* lap is cheapest is the exact optimum
    for a fixed total — and deterministic, since ties go to the earlier stint. Pit windows
    are exact feasibility bounds from the wear caps, not a rule of thumb.

32. **Pit loss is measured from the circuit's own stop, and flagged when it is not.** A stop
    is the lap two consecutive stints share; the loss is that lap's excess over the driver's
    own clean-lap median in the same race, which nets out the car, the fuel load and the
    circuit in one subtraction. Measured 16.4 s at Jeddah and 16.5 s at Miami. The fallback
    `DEFAULT_PIT_LOSS_S = 21.0` is used only when the circuit has no recorded stop and
    always arrives with `source: "default"`, because a plan's stop count is decided by this
    number.

33. **`FUEL_MARGIN_LAPS = 0.45`, validated against the tank the race was actually run on.**
    The Miami sprint was run with 15.0 kg for 14 laps at a measured 1.04 kg/lap — 14.42 laps
    of fuel. This rule asks for 14.45. Both units are emitted and shown, because the game
    asks for laps on the slider and kilograms on the setup screen; converting one into the
    other for the driver is the sort of arithmetic that gets done wrong under pressure.

34. **Race-trim evidence is defined as session types 10–17, as specified, with a caveat
    recorded here.** 15–17 are the races and this recorder saw a real Miami sprint arrive as
    type 16; 10–14 are the sprint weekend's shootout sessions, which are qualifying-shaped.
    A one-shot shootout contributes at most one lap, which cannot produce a fit and loses
    every tie-break to a longer run, so including them costs nothing in practice — but a
    sprint-weekend fuel figure can pick up a shootout lap, which is why `fuel.session_ids`
    is in the payload. Tightening this to `>= 15` plus an explicit sprint detector is the
    obvious follow-up if it ever matters.

35. **`sessions_at_track` deduplicates to one row per `session_uid`.** Found while
    validating: a pause or a process restart writes a second `sessions` row holding the same
    laps and the same stops, and the first version of this counted Jeddah's race fuel twice
    and reported its one pit stop as two. It now picks the representative segment by the
    same rule `_SESSION_LIST_SQL` already uses.

36. **The strategy route is track-scoped and stays read-only.** `GET
    /api/analysis/strategy?track_id=&race_laps=` — a fifth GET, no mutating route, and
    `track_id = -1` (the "no Session packet" sentinel) is refused at the door rather than
    gathering every trackless fragment ever captured. `race_laps` omitted defaults to the
    newest race at the circuit that recorded its own distance; a circuit with no such race
    is a 422 naming the parameter, because guessing a race length silently decides the stop
    count for the driver.

37. **The frontend mock re-implements the engine rather than shipping a payload blob.**
    Same rule `mock.ts` and the rest of `analysis-mock.ts` already follow: the fixture caps
    stints at the wear cliff, enumerates the legal orderings and allocates laps with the
    same greedy step, so `npm run dev:mock` exercises a plan whose stint lengths and pit
    windows are internally consistent, and the untested-compound and no-legal-plan paths are
    reachable by changing inputs instead of by hand-writing a second payload.

38. **A wear rate alone earns plans; it just does not earn a time.** The weekend this
    recorder actually sees is two or three laps a set, which can never produce the four
    clean laps a degradation line needs — so the strategy page was permanently one race
    behind, unlocking only after a compound had already been raced. A wear rate needs two
    laps and is enough to know how long a set lasts, which is enough to enumerate the same
    plans, cap the same stints and compute the same pit windows. Those come back with
    `plans_ranking: "heuristic"` and **every projected-time field null** — never a fabricated
    number — ordered by a rule stated in the payload: fewest stops first, then the softer
    compound earlier. Laps inside such a plan are split so no stint ends closer to its wear
    ceiling than another, which is the only measured thing left to optimise. Time ranking is
    tried first and kept whenever it produces anything, so a third compound picking up a
    two-lap wear rate can never demote a set that was already ranked on pace.

39. **Degradation is derived by indexing lap time on wear, not on lap number.** One stint at
    a circuit with ≥ `MIN_FIT_LAPS` clean laps is regressed against *cumulative* worst-wheel
    wear (per-lap deltas, so a bad sample costs one step rather than the whole axis) instead
    of tyre age. The slope is a track-level `ms_per_wear_pct`, and any compound with a
    measured wear rate and no fit of its own gets `deg = ms_per_wear_pct × wear_pct_per_lap`,
    marked `source: "derived"` with both parents named. The assumption — that a percent of
    wear costs the same lap time on all three dry slicks here — is stated in the source, in
    the payload (`wear_calibration.assumption`) and on the page, because it is an assumption
    and the direct fit it substitutes for is not. A compound's own fit always wins; wets
    neither calibrate it nor borrow it; a stint that covered less than
    `MIN_CALIBRATION_WEAR_SPAN_PCT` of wear is refused, because dividing lap-to-lap scatter
    by a tiny span reports driver noise as seconds of degradation. Validated on real rows:
    Miami's six-lap soft race stint calibrates 195 ms/%, which hands the untested-in-anger
    medium 735 ms/lap and the hard 562 ms/lap and turns "no legal plan" into a ranked sheet.
    Base pace for a derived model is the median of that compound's own clean laps at the
    circuit — a stint average rather than an age-zero pace, and deliberately **not**
    fuel-normalised, which is stated in `base_source` rather than hidden.

40. **A negative coefficient is reported raw and clamped to zero for planning**, the same
    convention `deg_ms_per_lap_planned` already carries and for the same reason. Montreal's
    only long run got quicker as it wore (fuel burning off through a six-lap race), so its
    coefficient is −125 ms/% and every derived slope there is negative; the payload shows
    those numbers and the allocator uses zero. Worth recording that this makes every plan at
    such a circuit flat in time, so ranking falls back to base pace alone and the cheapest
    way to satisfy the two-compound rule becomes a one-lap token stint. It is honest — that
    *is* what the data says — but a minimum-stint-length rule is the obvious follow-up.

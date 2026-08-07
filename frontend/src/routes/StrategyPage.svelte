<script lang="ts">
  /**
   * What to put in the car before the lights go out.
   *
   * Two decisions, in the order they have to be made. **Fuel** first, because it is
   * irreversible — the load is set on the grid and no amount of strategy recovers a car
   * that runs dry — so it gets the largest type on the page and both units, kilograms and
   * the laps figure the in-game slider actually asks for. **Plans** second, as cards you
   * compare rather than a table you read: each one is a picture of the race (stint bars in
   * compound colour, at true lap width) plus the two numbers that separate it from the one
   * above — the projected loss and when you have to box.
   *
   * Underneath is the part that makes any of it trustworthy: the per-compound model table,
   * with the evidence tier beside every number and an explicit row for every compound
   * nobody ran. An untested compound is not omitted; a strategy page whose hard-tyre row is
   * simply absent reads as "no hard tyres exist", and the honest statement is "we have
   * never measured them". Same for the provenance footer: every session that contributed,
   * and what it contributed.
   *
   * Wear is telemetry percent throughout. The in-game display reads roughly double, so the
   * conversion is offered once, as an approximation, clearly labelled — never baked into a
   * number the model uses.
   *
   * Two claims on this page are weaker than the rest and are marked as such rather than
   * softened. A **heuristically ranked** plan set has no projected times at all — it was
   * ordered by a rule because nothing was driven long enough to model — so the badge says so
   * and the time columns hold an em-dash instead of a number. A **derived** degradation
   * slope was assembled from another stint's wear-to-time coefficient and this compound's
   * own wear rate, so it carries a marker of its own next to the figure. Both are real
   * answers; neither is a measurement of the thing it sits beside, and a page that let them
   * look identical to a fitted number would be the sort of quiet lie this app exists to
   * avoid.
   */
  import {
    fetchSessions,
    fetchStrategy,
    type SessionSummary,
    type StrategyCompound,
    type StrategyPlan,
    type StrategyResponse
  } from '../lib/analysis-api';
  import { LOADING, type Async } from '../lib/analysis.svelte';
  import { href, router } from '../lib/router.svelte';
  import { DASH, formatNumber } from '../lib/format';
  import {
    fitConfidence,
    formatDegradation,
    formatPercent,
    formatR2,
    inGameWear,
    formatMinutesSeconds,
    formatSecondsDelta
  } from '../lib/analysis-format';
  import { compoundOf } from '../lib/enums';
  import StatePanel from '../components/StatePanel.svelte';

  interface Props {
    /** Circuit to plan for. Null means "pick one from the sessions that exist". */
    trackId: number | null;
    /** Race distance override. Null lets the backend use the newest race at this track. */
    raceLaps: number | null;
  }

  let { trackId, raceLaps }: Props = $props();

  let sessions = $state<Async<SessionSummary[]>>(LOADING);
  let strategy = $state<Async<StrategyResponse> | null>(null);
  let attempt = $state(0);

  /* The track picker's options come from the session browser's own list, so a circuit
     appears here exactly when there is something recorded at it. */
  $effect(() => {
    const controller = new AbortController();
    sessions = LOADING;
    fetchSessions(200, controller.signal)
      .then((data) => {
        sessions = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        sessions = { status: 'error', error };
      });
    return () => controller.abort();
  });

  interface TrackOption {
    id: number;
    name: string;
    sessions: number;
  }

  let tracks = $derived.by((): TrackOption[] => {
    if (sessions.status !== 'ok') return [];
    const seen = new Map<number, TrackOption>();
    for (const session of sessions.data) {
      // -1 is the recorder's "the Session packet never landed" sentinel, not a circuit.
      if (session.track_id == null || session.track_id < 0) continue;
      const existing = seen.get(session.track_id);
      if (existing) existing.sessions += 1;
      else
        seen.set(session.track_id, {
          id: session.track_id,
          name: session.track_name ?? `Track ${session.track_id}`,
          sessions: 1
        });
    }
    return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
  });

  /** The circuit actually being shown: the URL's, or the newest one recorded. */
  let selected = $derived(trackId ?? tracks[0]?.id ?? null);

  $effect(() => {
    void attempt;
    const track = selected;
    const laps = raceLaps;
    if (track === null) {
      strategy = null;
      return;
    }
    const controller = new AbortController();
    strategy = LOADING;
    fetchStrategy(track, laps, controller.signal)
      .then((data) => {
        strategy = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        strategy = { status: 'error', error };
      });
    return () => controller.abort();
  });

  function pickTrack(event: Event): void {
    const value = Number((event.currentTarget as HTMLSelectElement).value);
    // The distance is dropped with the circuit: laps carried from another track's race
    // would silently plan the wrong length.
    router.go(href('/strategy', { track: value }));
  }

  let lapsDraft = $state('');
  $effect(() => {
    lapsDraft = raceLaps === null ? '' : String(raceLaps);
  });

  function applyLaps(event: Event): void {
    event.preventDefault();
    const value = Number(lapsDraft);
    const laps = Number.isSafeInteger(value) && value >= 1 && value <= 200 ? value : null;
    router.go(href('/strategy', { track: selected, laps }));
  }

  /** Stint bars are drawn at true lap width, so the picture is the plan. */
  function widthOf(stint: { laps: number }, raceDistance: number): string {
    return `${(100 * stint.laps) / Math.max(1, raceDistance)}%`;
  }

  /** Compounds in the order a strategist thinks about them: softest first, then wets. */
  function orderedCompounds(rows: StrategyCompound[]): StrategyCompound[] {
    return [...rows].sort(
      (a, b) => Number(b.dry) - Number(a.dry) || a.compound_visual - b.compound_visual
    );
  }

  function omission(data: StrategyResponse, key: string): string | null {
    return data.omitted[key] ?? null;
  }

  /** The plan's own headline: how much it costs against the best one. */
  function planDelta(plan: StrategyPlan): string {
    if (plan.delta_to_best_ms == null) return DASH;
    return plan.rank === 1 ? 'fastest' : `${formatSecondsDelta(plan.delta_to_best_ms)} s`;
  }

  /**
   * The badge over the plan list. Two words for what kind of ordering this is, because
   * "rank 1" means something completely different in the two cases: fastest, or merely
   * first under a rule.
   */
  const RANKING_BADGE: Record<string, string> = {
    time: 'ranked on pace models',
    heuristic: 'ranked by rule — no pace model yet; times not projected'
  };
</script>

<section class="analysis-page" data-page="strategy">
  <header class="page-head">
    <div>
      <h1 class="page-title">Strategy</h1>
      <p class="page-sub">
        Fuel load and pit strategy for this circuit, computed from what you drove here. Every
        number was measured this weekend; nothing is defaulted without saying so.
      </p>
    </div>

    <form class="controls" onsubmit={applyLaps}>
      <label class="field">
        <span class="label">Circuit</span>
        <select
          class="input"
          data-testid="track-picker"
          value={selected === null ? '' : String(selected)}
          onchange={pickTrack}
          disabled={tracks.length === 0}
        >
          {#each tracks as track (track.id)}
            <option value={String(track.id)}>{track.name} · {track.sessions} sessions</option>
          {/each}
        </select>
      </label>
      <label class="field">
        <span class="label">Race laps</span>
        <input
          class="input laps"
          data-testid="race-laps"
          type="number"
          min="1"
          max="200"
          inputmode="numeric"
          placeholder="auto"
          bind:value={lapsDraft}
        />
      </label>
      <button class="btn" type="submit">Plan</button>
    </form>
  </header>

  {#if strategy === null}
    <div class="panel idle">
      <p class="idle-title">Nothing recorded yet</p>
      <p class="idle-text">
        A strategy sheet is built out of your own running at a circuit. Drive a session with
        the recorder on and this page will have something to plan with.
      </p>
      <a class="btn" href="#/sessions">Browse sessions</a>
    </div>
  {:else}
    <StatePanel
      state={strategy}
      loadingLabel="the strategy sheet"
      isEmpty={() => false}
    >
      {#snippet actions()}
        <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      {/snippet}

      {#snippet children(data)}
        <p class="context label" data-testid="strategy-context">
          {data.track_name ?? `Track ${data.track_id}`} · {data.race_laps} laps ({data.race_laps_source})
          · wear ceiling {formatPercent(data.wear_cliff_pct)}
          <span class="approx">({inGameWear(data.wear_cliff_pct)} on the in-game display)</span>
        </p>

        <!--
          Fuel first and biggest. It is the one number on this page that cannot be
          corrected once the race starts, and it is asked for in two different units
          depending on where you type it, so both are given rather than one converted.
        -->
        <article class="panel fuel" data-testid="fuel-card">
          {#if data.fuel}
            <div class="fuel-figures">
              <div class="figure">
                <span class="label">Fuel to load</span>
                <span class="big gauge">{formatNumber(data.fuel.recommended_kg, 1)}</span>
                <span class="unit">kg</span>
              </div>
              <div class="figure">
                <span class="label">Slider</span>
                <span class="big gauge">{formatNumber(data.fuel.slider_laps, 2)}</span>
                <span class="unit">laps of fuel</span>
              </div>
              <div class="fuel-detail">
                <p>
                  {formatNumber(data.fuel.kg_per_lap, 3)} kg/lap measured over
                  {data.fuel.laps_measured} racing laps
                  <span class="tier tier-{data.fuel.evidence}">{data.fuel.evidence}</span>,
                  × {data.race_laps} laps + {data.fuel.margin_laps} laps of margin.
                </p>
                <p class="hint">
                  The margin covers the formation lap, the in-lap and a safety-car restart. It
                  is under half a kilo, which is worth almost nothing in lap time and is the
                  difference between finishing and not.
                </p>
              </div>
            </div>
          {:else}
            <div class="fuel-figures">
              <div class="figure">
                <span class="label">Fuel to load</span>
                <span class="big gauge">{DASH}</span>
              </div>
              <div class="fuel-detail">
                <p class="omitted" data-testid="fuel-omitted">{omission(data, 'fuel')}</p>
              </div>
            </div>
          {/if}
        </article>

        <!-- Plans. -->
        <h2 class="section-title">
          Plans
          <span class="label"
            >{data.plans.length} of {data.plans_considered} shown · pit loss
            {formatNumber(data.pit_loss_s, 1)} s
            <span class="tier tier-{data.pit_loss.source}">{data.pit_loss.source}</span>
          </span>
          {#if data.plans_ranking}
            <span
              class="tier tier-ranking-{data.plans_ranking}"
              data-testid="plans-ranking"
              data-ranking={data.plans_ranking}
            >
              {RANKING_BADGE[data.plans_ranking]}
            </span>
          {/if}
        </h2>

        {#if data.plans.length === 0}
          <div class="panel state empty" data-testid="no-plans">
            <p class="detail">{omission(data, 'plans') ?? 'No legal plan for this race length.'}</p>
          </div>
        {:else}
          {#if data.plans_ranking_note}
            <p class="hint ranking-note" data-testid="plans-ranking-note">
              {data.plans_ranking_note}
            </p>
          {/if}
          <div class="plans">
            {#each data.plans as plan (plan.rank)}
              <article class="panel plan" class:best={plan.rank === 1} data-plan={plan.rank}>
                <div class="plan-head">
                  <span class="rank">{plan.rank}</span>
                  <span class="plan-name">{plan.label}</span>
                  <span class="chip-plain">
                    <span class="k">Stops</span>{plan.stops}
                  </span>
                  <!--
                    A heuristically ranked plan has no time to show and does not borrow one:
                    both chips fall back to the em-dash the rest of the app uses for "not
                    known", so a feasible plan never reads as a timed one.
                  -->
                  <span
                    class="chip-plain delta"
                    class:is-best={plan.rank === 1 && plan.delta_to_best_ms != null}
                  >
                    <span class="k">vs best</span>
                    <span class="clock">{planDelta(plan)}</span>
                  </span>
                  <span class="chip-plain">
                    <span class="k">Race</span>
                    <span class="clock">{formatMinutesSeconds(plan.total_time_ms)}</span>
                  </span>
                </div>

                <!--
                  The plan as a picture. Bars are laid out at true lap width, so a stint
                  that is twice as long looks twice as long, and the compound colour is
                  always accompanied by its letter and its lap range.
                -->
                <div class="bars" role="list" aria-label="Stints">
                  {#each plan.stints as stint, i (i)}
                    {@const compound = compoundOf(stint.compound_visual)}
                    <div
                      class="bar"
                      role="listitem"
                      style="width: {widthOf(stint, data.race_laps)}; background: {compound.color}; color: {compound.ink}"
                      title="{stint.name}, laps {stint.lap_start}–{stint.lap_end}, {formatPercent(
                        stint.projected_end_wear_pct
                      )} projected wear"
                    >
                      <span class="bar-code">{compound.code}</span>
                      <span class="bar-laps">{stint.lap_start}–{stint.lap_end}</span>
                    </div>
                  {/each}
                </div>

                <ul class="stint-list">
                  {#each plan.stints as stint, i (i)}
                    <li>
                      <span class="stint-name">{stint.name}</span>
                      <span class="stint-laps">{stint.laps} laps</span>
                      <span class="stint-wear"
                        >{formatPercent(stint.projected_end_wear_pct)} at the end
                        <span class="approx">({inGameWear(stint.projected_end_wear_pct)} OSD)</span>
                      </span>
                    </li>
                  {/each}
                </ul>

                <div class="chip-row">
                  {#each plan.pit_windows as window (window.stop)}
                    <span class="chip-plain">
                      <span class="k">Box {window.stop}</span>
                      <span class="clock">lap {window.planned_lap}</span>
                      <span class="unit"
                        >window {window.earliest_lap}–{window.latest_lap}</span
                      >
                    </span>
                  {/each}
                </div>

                <p class="sc sc-{plan.safety_car.flexibility}">
                  <span class="label">Safety car · {plan.safety_car.flexibility}</span>
                  {plan.safety_car.note}
                </p>
              </article>
            {/each}
          </div>
        {/if}

        <!-- The models everything above rests on. -->
        <h2 class="section-title">Compound models</h2>
        <div class="panel">
          <div class="table-scroll">
            <table class="data-table" data-testid="compound-table">
              <caption class="label">
                Wear is telemetry percent of the worst wheel. The in-game display reads
                roughly double — the approximation is shown beside it and is never used in a
                calculation.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Compound</th>
                  <th scope="col">Evidence</th>
                  <th scope="col" class="num">Deg s/lap</th>
                  <th scope="col" class="num">r²</th>
                  <th scope="col" class="num">Wear %/lap</th>
                  <th scope="col" class="num">Max stint</th>
                  <th scope="col">Measured from</th>
                </tr>
              </thead>
              <tbody>
                {#each orderedCompounds(data.compounds) as row (row.compound_visual)}
                  {@const compound = compoundOf(row.compound_visual)}
                  {@const confidence = fitConfidence(row.pace?.r2)}
                  <tr
                    class:untested={row.untested}
                    data-compound={row.compound_visual}
                    data-untested={row.untested}
                  >
                    <td>
                      <span
                        class="pill"
                        style="background: {compound.color}; color: {compound.ink}"
                        aria-hidden="true">{compound.code}</span
                      >
                      {row.name}
                      {#if !row.dry}<span class="label wet">wet</span>{/if}
                    </td>
                    <td>
                      {#if row.untested}
                        <span class="tier tier-untested">untested</span>
                      {:else}
                        <span class="tier tier-{row.evidence}">{row.evidence}</span>
                      {/if}
                    </td>
                    <!--
                      Degradation, with its grade attached. A fitted slope is bare, because
                      it is the ordinary case. A derived one is marked, because it was never
                      measured on this tyre: it is this compound's wear rate times the
                      circuit's wear-to-time coefficient, and the title says which stint
                      supplied that. Nothing at all is the em-dash, with the reason in the
                      last column.
                    -->
                    <td class="num clock">
                      {formatDegradation(row.pace?.deg_ms_per_lap)}
                      {#if row.pace?.source === 'derived' && row.pace.derived_from}
                        <span
                          class="tier tier-derived"
                          data-testid="derived-{row.compound_visual}"
                          title="{formatNumber(row.pace.derived_from.ms_per_wear_pct, 0)} ms per % of wear, measured on the {row.pace.derived_from
                            .calibration.name} stint in {row.pace.derived_from.calibration
                            .session_label}, × {formatPercent(
                            row.pace.derived_from.wear_pct_per_lap
                          )}/lap of wear measured on this compound">derived</span
                        >
                      {/if}
                    </td>
                    <td class="num clock conf-{confidence}">{formatR2(row.pace?.r2)}</td>
                    <td class="num gauge">
                      {formatPercent(row.wear?.pct_per_lap)}
                      {#if row.wear?.pct_per_lap != null}
                        <span class="approx">≈{inGameWear(row.wear.pct_per_lap)}</span>
                      {/if}
                    </td>
                    <td class="num">{row.max_stint_laps ?? DASH}</td>
                    <td class="from">
                      {#if row.untested}
                        <span class="omitted">{row.not_plannable_reason}</span>
                      {:else}
                        {row.pace?.session_label ?? row.wear?.session_label ?? DASH}
                        {#if row.pace?.source === 'derived' && row.pace.derived_from}
                          <span class="label"
                            >calibration: {row.pace.derived_from.calibration.name} stint {row.pace
                              .derived_from.calibration.stint_no} · laps {row.pace.derived_from
                              .calibration.lap_range[0]}–{row.pace.derived_from.calibration
                              .lap_range[1]} · {row.pace.derived_from.calibration.laps_used} used</span
                          >
                          <span class="label"
                            >base: median of {row.pace.base_source?.laps} clean lap(s) on this
                            compound here</span
                          >
                        {:else if row.pace}
                          <span class="label"
                            >stint {row.pace.stint_no} · laps {row.pace.lap_range[0]}–{row.pace
                              .lap_range[1]} · {row.pace.laps_used} used</span
                          >
                        {/if}
                        {#if !row.plannable}
                          <span class="omitted">{row.not_plannable_reason}</span>
                        {/if}
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>

        <!-- Provenance. Everything above can be checked against these sessions. -->
        <footer class="panel provenance" data-testid="provenance">
          <p class="label">Built from</p>
          <ul>
            {#each data.sessions_used as session (session.id)}
              <li>
                <a class="src" href={href(`/sessions/${session.id}`)}>
                  {session.session_type_name ?? `Session ${session.id}`}
                </a>
                <span class="tier tier-{session.evidence}">{session.evidence}</span>
                <span class="gave">
                  {session.contributed.length > 0
                    ? session.contributed.join(', ')
                    : 'read, contributed nothing'}
                </span>
              </li>
            {/each}
          </ul>
          <p class="hint">{data.pit_loss.detail}</p>
          <!--
            The shared coefficient gets its own line, and its assumption with it. It is the
            one number here that is lent between compounds, so the sentence that says why
            that is allowed travels with it rather than living only in the source.
          -->
          {#if data.wear_calibration}
            <p class="hint" data-testid="wear-calibration">
              Wear-to-time: {formatNumber(data.wear_calibration.ms_per_wear_pct, 0)} ms per % of
              wear, from the {data.wear_calibration.name} stint in
              {data.wear_calibration.session_label}
              ({data.wear_calibration.laps_used} laps over
              {formatPercent(data.wear_calibration.wear_span_pct)} of wear, r² {formatR2(
                data.wear_calibration.r2
              )}).
              {#if data.wear_calibration.ms_per_wear_pct_planned != null}
                Measured negative, so planning uses zero.
              {/if}
              {data.wear_calibration.assumption}
            </p>
          {:else if omission(data, 'wear_calibration')}
            <p class="hint omitted" data-testid="wear-calibration-omitted">
              {omission(data, 'wear_calibration')}
            </p>
          {/if}
        </footer>
      {/snippet}
    </StatePanel>
  {/if}
</section>

<style>
  .controls {
    display: flex;
    align-items: flex-end;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .input {
    padding: 0.22rem 0.4rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    background: var(--surface-2);
    color: var(--ink);
    font: inherit;
    font-size: 0.82rem;
  }

  .laps {
    width: 6rem;
    font-variant-numeric: tabular-nums;
  }

  .context {
    margin: 0;
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-3);
  }

  .section-title {
    margin: 0.4rem 0 0;
    font-size: 1rem;
    font-weight: 700;
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  /*
   * The fuel card. It is the only place on this side of the app that spends display-sized
   * type, because it is the only number here that is unrecoverable if it is wrong.
   */
  .fuel {
    border-color: var(--line-strong);
  }

  .fuel-figures {
    display: flex;
    align-items: flex-end;
    gap: 1.4rem;
    flex-wrap: wrap;
  }

  .figure {
    display: flex;
    align-items: baseline;
    gap: 0.3rem;
    flex-wrap: wrap;
  }

  .figure .label {
    flex: 1 0 100%;
  }

  .big {
    font-size: 2.4rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.03em;
    color: var(--ink);
  }

  .unit {
    color: var(--ink-3);
    font-size: 0.72rem;
  }

  .fuel-detail {
    flex: 1 1 20rem;
    min-width: 0;
  }

  .fuel-detail p {
    margin: 0;
    font-size: 0.82rem;
    color: var(--ink-2);
  }

  .hint {
    margin: 0.2rem 0 0;
    font-size: 0.76rem;
    color: var(--ink-3);
  }

  .omitted {
    display: block;
    font-size: 0.78rem;
    color: var(--amber);
  }

  /*
   * Evidence tier as a word in a box, never as colour alone. Race trim is the trustworthy
   * one and gets the quiet treatment; practice and defaults get amber, which is the same
   * "degraded, still usable" meaning the pit wall's health dot already carries.
   */
  .tier {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .tier-race,
  .tier-measured {
    color: var(--green);
    border-color: color-mix(in srgb, var(--green) 45%, var(--line));
  }

  .tier-practice,
  .tier-default {
    color: var(--amber);
    border-color: color-mix(in srgb, var(--amber) 45%, var(--line));
  }

  .tier-untested {
    color: var(--ink-3);
    border-style: dashed;
  }

  /*
   * The two "this is weaker evidence" markers. Both are amber for the same reason the
   * practice tier is — usable, not measured here — and both carry their own word, so the
   * distinction survives being read in greyscale or by a screen reader.
   */
  .tier-derived,
  .tier-ranking-heuristic {
    color: var(--amber);
    border-color: color-mix(in srgb, var(--amber) 45%, var(--line));
  }

  .tier-derived {
    margin-inline-start: 0.3rem;
    letter-spacing: 0.04em;
  }

  .tier-ranking-time {
    color: var(--green);
    border-color: color-mix(in srgb, var(--green) 45%, var(--line));
  }

  /* Sentences, not labels: these two badges say a whole thing and read better in mixed case. */
  .tier-ranking-time,
  .tier-ranking-heuristic {
    text-transform: none;
    letter-spacing: 0.01em;
    font-weight: 600;
    font-size: 0.66rem;
    white-space: normal;
  }

  /* The ranking rule spelled out, once, under the badge that abbreviates it. */
  .ranking-note {
    max-width: 62ch;
    text-transform: none;
    letter-spacing: 0;
  }

  /* The in-game conversion. Always parenthesised, always smaller, never a bare number. */
  .approx {
    color: var(--ink-3);
    font-size: 0.68rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .plans {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 28rem), 1fr));
    gap: 0.75rem;
    align-items: start;
  }

  .plan {
    gap: 0.5rem;
  }

  .plan.best {
    border-color: color-mix(in srgb, var(--green) 35%, var(--line));
  }

  .plan-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .rank {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 50%;
    background: var(--surface-3);
    color: var(--ink-2);
    font-size: 0.72rem;
    font-weight: 800;
    flex: none;
  }

  .plan.best .rank {
    background: color-mix(in srgb, var(--green) 25%, var(--surface-3));
    color: var(--ink);
  }

  .plan-name {
    font-size: 0.95rem;
    font-weight: 700;
    margin-inline-end: auto;
  }

  .delta.is-best .clock {
    color: var(--green);
  }

  .bars {
    display: flex;
    gap: 2px;
    height: 1.6rem;
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    min-width: 0;
    font-size: 0.68rem;
    font-weight: 700;
    overflow: hidden;
    white-space: nowrap;
  }

  .bar-code {
    font-weight: 800;
  }

  .bar-laps {
    font-variant-numeric: tabular-nums;
    opacity: 0.85;
  }

  .stint-list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    font-size: 0.76rem;
  }

  .stint-list li {
    display: flex;
    gap: 0.5rem;
    color: var(--ink-3);
  }

  .stint-name {
    min-width: 4.5rem;
    color: var(--ink-2);
    font-weight: 600;
  }

  .stint-laps {
    min-width: 4rem;
    font-variant-numeric: tabular-nums;
  }

  .sc {
    margin: 0;
    font-size: 0.76rem;
    color: var(--ink-3);
    border-inline-start: 2px solid var(--line);
    padding-inline-start: 0.5rem;
  }

  .sc .label {
    display: block;
  }

  .sc-flexible {
    border-inline-start-color: color-mix(in srgb, var(--green) 55%, var(--line));
  }

  .sc-tight,
  .sc-none {
    border-inline-start-color: color-mix(in srgb, var(--amber) 55%, var(--line));
  }

  .pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.2rem;
    height: 1.2rem;
    border-radius: 50%;
    font-size: 0.66rem;
    font-weight: 800;
    margin-inline-end: 0.3rem;
  }

  tr.untested td {
    color: var(--ink-3);
  }

  .wet {
    margin-inline-start: 0.3rem;
  }

  .from {
    white-space: normal;
    max-width: 24rem;
    font-size: 0.78rem;
    color: var(--ink-2);
  }

  .from .label {
    display: block;
  }

  .conf-weak {
    color: var(--amber);
  }

  .provenance ul {
    margin: 0.25rem 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    font-size: 0.78rem;
  }

  .provenance li {
    display: flex;
    gap: 0.45rem;
    align-items: baseline;
    flex-wrap: wrap;
  }

  .src {
    color: var(--ink);
    font-weight: 600;
    text-decoration: none;
    min-width: 11rem;
  }

  .src:hover {
    text-decoration: underline;
  }

  .gave {
    color: var(--ink-3);
  }

  .idle {
    align-items: center;
    text-align: center;
    padding: 2rem 1rem;
    gap: 0.4rem;
  }

  .idle-title {
    margin: 0;
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--ink-2);
  }

  .idle-text {
    margin: 0;
    max-width: 34rem;
    font-size: 0.85rem;
    color: var(--ink-3);
  }
</style>

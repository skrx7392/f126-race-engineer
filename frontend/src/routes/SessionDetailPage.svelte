<script lang="ts">
  /**
   * One session, in enough detail to choose two laps out of it.
   *
   * That framing decides the layout. The lap table is the primary instrument
   * and everything else supports picking rows in it: the chart is the same laps
   * as a shape (so an outlier is visible before you read a single number), and
   * the stint strip says which tyres each lap was on (so you compare like with
   * like). Selecting is the point, so selection is visible in three places at
   * once — the row, the chart point, and the nav chip.
   */
  import {
    fetchDebrief,
    fetchLaps,
    fetchSession,
    type Debrief,
    type Lap,
    type SessionDetail
  } from '../lib/analysis-api';
  import { LOADING, analysis, type Async } from '../lib/analysis.svelte';
  import { href } from '../lib/router.svelte';
  import { formatLapTime, formatSectorTime, DASH } from '../lib/format';
  import { carLabel, displayStintRanges, formatSessionDate } from '../lib/analysis-format';
  import { compoundOf, actualCompoundName } from '../lib/enums';
  import { CHART_INK, LAP, xAxisDistance, yAxis } from '../lib/chart-theme';
  import StatePanel from '../components/StatePanel.svelte';
  import UPlotChart, { type SeriesSpec } from '../components/UPlotChart.svelte';

  interface Props {
    sessionId: number;
  }

  let { sessionId }: Props = $props();

  let session = $state<Async<SessionDetail>>(LOADING);
  let laps = $state<Async<Lap[]>>(LOADING);
  /**
   * The post-session debrief. `null` inside an `ok` state means the session has none —
   * `fetchDebrief` folds the 404 into that, because a session nobody has debriefed yet is
   * an ordinary session and must not render as a failed request.
   */
  let debrief = $state<Async<Debrief | null>>(LOADING);
  let attempt = $state(0);

  /** Which car's laps to list. Null means "whatever the session says is ours". */
  let carIndex = $state<number | null>(null);

  $effect(() => {
    void attempt;
    const id = sessionId;
    const controller = new AbortController();
    session = LOADING;
    fetchSession(id, controller.signal)
      .then((data) => {
        session = { status: 'ok', data };
        // The player's car is the default, resolved once per session load.
        if (carIndex === null) {
          carIndex =
            data.player_car_index ??
            data.participants.find((p) => p.is_player)?.car_index ??
            data.participants[0]?.car_index ??
            null;
        }
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        session = { status: 'error', error };
      });
    return () => controller.abort();
  });

  $effect(() => {
    void attempt;
    const id = sessionId;
    const controller = new AbortController();
    debrief = LOADING;
    fetchDebrief(id, controller.signal)
      .then((data) => {
        debrief = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        debrief = { status: 'error', error };
      });
    return () => controller.abort();
  });

  $effect(() => {
    void attempt;
    const id = sessionId;
    const car = carIndex;
    if (car === null) return;
    const controller = new AbortController();
    laps = LOADING;
    fetchLaps(id, car, controller.signal)
      .then((data) => {
        laps = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        laps = { status: 'error', error };
      });
    return () => controller.abort();
  });

  /** Reset the car choice when the route points at a different session. */
  let lastSession = $state<number | null>(null);
  $effect(() => {
    if (lastSession !== sessionId) {
      lastSession = sessionId;
      carIndex = null;
    }
  });

  let rows = $derived(laps.status === 'ok' ? laps.data : []);
  let detail = $derived(session.status === 'ok' ? session.data : null);

  /** The player's car, however the session identifies it. */
  let playerCarIndex = $derived(
    detail?.player_car_index ??
      detail?.participants.find((p) => p.is_player)?.car_index ??
      null
  );

  /**
   * How many laps the driver actually ran.
   *
   * `lap_count` is every car's laps added together — 267 for a 13-lap race on a
   * full grid. That is a true statement about the archive and a wrong answer to
   * "how long was this session", so it is the last resort. The list endpoint
   * computes `player_lap_count`; the detail endpoint does not, so when the laps
   * on screen are already the player's they are counted here instead.
   */
  let playerLapCount = $derived.by(() => {
    if (detail?.player_lap_count != null) return detail.player_lap_count;
    const car = playerCarIndex;
    if (car === null || carIndex !== car || rows.length === 0) return null;
    return new Set(rows.map((l) => l.lap_number)).size;
  });

  /** The quickest valid lap in the listed set — the natural reference. */
  let bestLapNumber = $derived.by(() => {
    let best: Lap | null = null;
    for (const l of rows) {
      if (l.valid === false || l.lap_time_ms == null || l.lap_time_ms <= 0) continue;
      if (best === null || (l.lap_time_ms ?? 0) < (best.lap_time_ms ?? 0)) best = l;
    }
    return best?.lap_number ?? null;
  });

  function select(lap: Lap): void {
    analysis.toggle({ sessionId, lap: lap.lap_number });
  }

  function slotFor(lap: Lap): 'a' | 'b' | null {
    return analysis.slotOf({ sessionId, lap: lap.lap_number });
  }

  // ── lap-time chart ─────────────────────────────────────────────────────────

  /**
   * The same laps as a shape.
   *
   * Split into four series purely so the marks can differ: a connecting line,
   * counted laps as filled dots, deleted laps as hollow ones, and the current
   * selection enlarged in its slot colour. They are one series of data wearing
   * four mark styles — which is why the pane has one y-scale and one subject,
   * and the legend explains marks rather than naming four things.
   */
  let chartData = $derived.by(() => {
    const xs = rows.map((l) => l.lap_number);
    const all = rows.map((l) => l.lap_time_ms ?? null);
    const valid = rows.map((l) => (l.valid === false ? null : (l.lap_time_ms ?? null)));
    const invalid = rows.map((l) => (l.valid === false ? (l.lap_time_ms ?? null) : null));
    const slotA = rows.map((l) => (slotFor(l) === 'a' ? (l.lap_time_ms ?? null) : null));
    const slotB = rows.map((l) => (slotFor(l) === 'b' ? (l.lap_time_ms ?? null) : null));
    return [xs, all, valid, invalid, slotA, slotB];
  });

  let chartSeries = $derived.by((): SeriesSpec[] => [
    {},
    {
      label: 'Trend',
      stroke: CHART_INK.gridStrong,
      width: 1,
      scale: 'y',
      points: { show: false },
      legendHidden: true
    },
    {
      label: 'Counted',
      stroke: 'transparent',
      width: 0,
      scale: 'y',
      points: { show: true, size: 7, fill: CHART_INK.axis, stroke: CHART_INK.axis },
      format: formatLapTime
    },
    {
      // Hollow: present, plotted, and not counted. Never hidden.
      label: 'Deleted',
      stroke: 'transparent',
      width: 0,
      scale: 'y',
      points: { show: true, size: 8, fill: '#131519', stroke: CHART_INK.axis, width: 1.5 },
      format: formatLapTime
    },
    {
      label: 'Lap A',
      stroke: 'transparent',
      width: 0,
      scale: 'y',
      points: { show: true, size: 11, fill: LAP.a, stroke: LAP.a },
      format: formatLapTime
    },
    {
      label: 'Lap B',
      stroke: 'transparent',
      width: 0,
      scale: 'y',
      points: { show: true, size: 11, fill: LAP.b, stroke: LAP.b },
      format: formatLapTime
    }
  ]);

  const lapTimeAxis = yAxis({
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => formatLapTime(t))
  });

  const lapNumberAxis = {
    ...xAxisDistance(true),
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => String(Math.round(t)))
  };

  // ── stint strip ────────────────────────────────────────────────────────────

  let stints = $derived.by(() => {
    const car = carIndex;
    if (!detail || car === null) return [];
    return detail.tyre_stints.filter((s) => s.car_index === car);
  });

  let lapSpan = $derived.by(() => {
    const last = rows.at(-1)?.lap_number ?? detail?.total_laps ?? 1;
    return Math.max(1, last);
  });

  /**
   * Ranges with the pit lap attributed once, so the strip's widths and labels
   * agree with each other and with the laps run. See `displayStintRanges`.
   */
  let stintRanges = $derived(displayStintRanges(stints, lapSpan));
</script>

<section class="analysis-page" data-page="session-detail">
  <StatePanel state={session} loadingLabel="session">
    {#snippet actions()}
      <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
    {/snippet}

    {#snippet children(s)}
      <header class="page-head">
        <div>
          <h1 class="page-title">{s.track_name ?? 'Unknown track'}</h1>
          <p class="page-sub">
            {s.session_type_name ?? 'Session'} · {formatSessionDate(s.started_at_wall)}
          </p>
        </div>

        <div class="chip-row">
          <span class="chip-plain" title="{s.lap_count} lap records across all cars">
            <span class="k">Laps</span>
            {playerLapCount ?? s.lap_count}
          </span>
          <span class="chip-plain">
            <span class="k">Best</span>
            <span class="clock">{formatLapTime(s.best_lap_ms)}</span>
          </span>
          {#if s.player_name}
            <span class="chip-plain"><span class="k">Driver</span> {s.player_name}</span>
          {/if}
        </div>
      </header>

      <!--
        The debrief sits directly under the header, above the instruments: it is the one
        thing on this page you read rather than operate, and it says what the numbers below
        it mean. Collapsed by default, because the lap table is why you came here.
      -->
      <!--
        No `.panel` wrapper here: StatePanel draws its own bordered box for the loading,
        error and empty states, and nesting one inside another gives the absent state a
        double border.
      -->
      <div data-panel="debrief">
        <StatePanel
          state={debrief}
          loadingLabel="the debrief"
          isEmpty={(d) => d === null}
          emptyMessage="No debrief yet — run: f126 debrief {sessionId}"
        >
          {#snippet children(d)}
            {#if d}
              <details class="panel debrief">
                <summary class="label">
                  Debrief
                  <span class="summary-meta"
                    >{d.model ?? 'unknown model'} · {formatSessionDate(d.created_at)}</span
                  >
                </summary>
                <p class="debrief-text" data-testid="debrief-text">{d.text}</p>
                <p class="debrief-note label">
                  Written from a fact sheet computed off this session's telemetry. Every
                  number above came from that sheet.
                </p>
              </details>
            {/if}
          {/snippet}
        </StatePanel>
      </div>

      <!-- Tyre life across the session, above the laps it explains. -->
      {#if stints.length > 0}
        <div class="panel" data-panel="stint-strip">
          <div class="panel-head">
            <span class="label">Stints</span>
            <span class="label">{lapSpan} laps</span>
          </div>
          <div class="strip">
            {#each stints as stint, i (stint.stint_no)}
              {@const range = stintRanges[i]}
              {@const compound = compoundOf(stint.compound_visual)}
              <div
                class="stint"
                style="flex-grow: {Math.max(1, range.laps)}"
                title="Stint {stint.stint_no}: laps {range.from}–{range.to}, {compound.label}"
              >
                <span
                  class="pill"
                  style="background: {compound.color}; color: {compound.ink}"
                  aria-hidden="true">{compound.code}</span
                >
                <span class="stint-text">
                  {compound.label}{#if actualCompoundName(stint.compound_actual)}
                    <span class="muted"> {actualCompoundName(stint.compound_actual)}</span>
                  {/if}
                </span>
                <span class="stint-laps clock">{range.from}–{range.to}</span>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <div class="grid">
        <div class="panel" data-panel="lap-chart">
          <div class="panel-head">
            <span class="label">Lap times</span>
            <span class="label">by lap number</span>
          </div>
          {#if rows.length > 0}
            <UPlotChart
              pane="lap-times"
              data={chartData}
              series={chartSeries}
              height={230}
              options={{ axes: [lapNumberAxis, lapTimeAxis] }}
            />
          {:else}
            <p class="label empty-note">No laps to plot.</p>
          {/if}
        </div>

        <div class="panel" data-panel="lap-table">
          <div class="panel-head">
            <span class="label">Laps</span>
            <div class="field">
              <label class="label" for="car-picker">Car</label>
              <select
                id="car-picker"
                bind:value={carIndex}
                onchange={() => analysis.clearSelection()}
              >
                {#each s.participants as p (p.car_index)}
                  <option value={p.car_index}>
                    {carLabel(p.name, p.car_index)}{p.is_player ? ' · you' : ''}
                  </option>
                {/each}
              </select>
            </div>
          </div>

          <StatePanel
            state={laps}
            loadingLabel="laps"
            isEmpty={(l) => l.length === 0}
            emptyMessage="No laps recorded for this car."
          >
            {#snippet children(lapRows)}
              <p class="hint label">Click a lap to select it. First pick is A, second is B.</p>
              <div class="table-scroll">
                <table class="data-table" data-testid="lap-table">
                  <thead>
                    <tr>
                      <th scope="col" class="num">Lap</th>
                      <th scope="col" class="num">Time</th>
                      <th scope="col" class="num">S1</th>
                      <th scope="col" class="num">S2</th>
                      <th scope="col" class="num">S3</th>
                      <th scope="col" class="num">Age</th>
                      <th scope="col">Tyre</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each lapRows as lap (lap.lap_number)}
                      {@const slot = slotFor(lap)}
                      {@const compound = compoundOf(lap.compound_visual)}
                      <tr
                        class="selectable"
                        class:is-selected={slot !== null}
                        class:is-excluded={lap.valid === false}
                        data-lap={lap.lap_number}
                        data-slot={slot}
                      >
                        <td class="num">
                          <button
                            class="pick"
                            type="button"
                            onclick={() => select(lap)}
                            aria-pressed={slot !== null}
                          >
                            {lap.lap_number}
                            {#if slot}<span class="slot slot-{slot}">{slot.toUpperCase()}</span>{/if}
                          </button>
                        </td>
                        <td class="num clock lap-time">
                          {formatLapTime(lap.lap_time_ms)}
                          {#if lap.lap_number === bestLapNumber}
                            <span class="best-flag label">best</span>
                          {/if}
                        </td>
                        <td class="num clock">{formatSectorTime(lap.s1_ms)}</td>
                        <td class="num clock">{formatSectorTime(lap.s2_ms)}</td>
                        <td class="num clock">{formatSectorTime(lap.s3_ms)}</td>
                        <td class="num">{lap.tyre_age_laps ?? DASH}</td>
                        <td>
                          <span
                            class="pill small"
                            style="background: {compound.color}; color: {compound.ink}"
                            >{compound.code}</span
                          >
                        </td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {/snippet}
          </StatePanel>

          <!--
            The exits. Disabled rather than hidden when the selection is not
            ready, with the requirement said out loud beneath them.
          -->
          <div class="actions">
            <a
              class="btn"
              class:disabled={!analysis.paired}
              href={analysis.paired ? analysis.compareHref() : '#/sessions'}
              aria-disabled={!analysis.paired}
            >
              Compare A vs B
            </a>
            <a
              class="btn"
              class:disabled={analysis.a === null}
              href={analysis.a ? analysis.cornersHref() : '#/sessions'}
              aria-disabled={analysis.a === null}
            >
              Corner analysis
            </a>
            <a class="btn" href={href('/stints', { session: sessionId })}>Stints</a>
            <!--
              Strategy is scoped to the circuit rather than to this session, so the link
              carries the track and nothing else — it is the whole weekend's answer, not
              this session's.
            -->
            {#if s.track_id != null && s.track_id >= 0}
              <a class="btn" href={href('/strategy', { track: s.track_id })}>
                Strategy for {s.track_name ?? 'this circuit'}
              </a>
            {/if}
            {#if analysis.paired}
              <button class="btn" type="button" onclick={() => analysis.swap()}>Swap A/B</button>
            {/if}
          </div>
          {#if !analysis.paired}
            <p class="hint label">
              {analysis.a === null
                ? 'Select a lap to enable corner analysis.'
                : 'Select a second lap to enable the comparison.'}
            </p>
          {/if}
        </div>
      </div>
    {/snippet}
  </StatePanel>
</section>

<style>
  /* Debrief. Graphite only — this is prose, and the accent colours on this page all
     carry meaning (compound, delta, fit confidence). Giving a paragraph a hue would
     make it look like a status. */
  .debrief summary {
    cursor: pointer;
    color: var(--ink-3);
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    justify-content: space-between;
  }

  .debrief summary:hover {
    color: var(--ink-2);
  }

  .summary-meta {
    text-transform: none;
    letter-spacing: 0;
    font-weight: 500;
    font-size: 0.62rem;
    color: var(--ink-3);
  }

  .debrief-text {
    /* The backend emits paragraphs separated by blank lines and nothing else — no
       markdown, no HTML — so pre-wrap is both the correct rendering and the safe one. */
    white-space: pre-wrap;
    margin: 0.5rem 0 0;
    padding-top: 0.5rem;
    border-top: 1px solid var(--line);
    max-width: 68ch;
    font-size: 0.82rem;
    line-height: 1.65;
    color: var(--ink-2);
  }

  .debrief-note {
    margin: 0.6rem 0 0;
    text-transform: none;
    letter-spacing: 0;
    font-weight: 500;
    max-width: 68ch;
    color: var(--ink-3);
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 0.75rem;
    align-items: start;
  }

  @media (max-width: 60rem) {
    .grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .strip {
    display: flex;
    gap: 0.25rem;
    min-width: 0;
  }

  .stint {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem 0.5rem;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    min-width: 0;
    overflow: hidden;
  }

  .stint-text {
    font-size: 0.78rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .muted {
    color: var(--ink-3);
    font-weight: 500;
    margin-inline-start: 0.3rem;
  }

  .stint-laps {
    margin-inline-start: auto;
    font-size: 0.72rem;
    color: var(--ink-3);
  }

  /* The compound pill always carries its letter; colour is the redundant part. */
  .pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.25rem;
    height: 1.25rem;
    border-radius: 50%;
    font-size: 0.7rem;
    font-weight: 800;
    flex: none;
  }

  .pill.small {
    width: 1.05rem;
    height: 1.05rem;
    font-size: 0.62rem;
  }

  .pick {
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    font-variant-numeric: tabular-nums;
    padding: 0.1rem 0.2rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: var(--radius-sm);
  }

  .pick:hover {
    background: var(--surface-3);
  }

  /* Stretch the pick target over the row: the whole row is the hit area. */
  .selectable {
    position: relative;
  }

  .pick::after {
    content: '';
    position: absolute;
    inset: 0;
  }

  .pick:focus-visible::after {
    outline: 2px solid var(--line-strong);
    outline-offset: -2px;
  }

  .slot {
    font-size: 0.6rem;
    font-weight: 800;
    padding: 0 0.22rem;
    border-radius: 2px;
    line-height: 1.35;
  }

  .slot-a {
    background: var(--chart-lap-a);
    color: #14150f;
  }

  .slot-b {
    background: var(--chart-lap-b);
    color: #f2f4f7;
  }

  .best-flag {
    margin-inline-start: 0.3rem;
    color: var(--ink-3);
    font-size: 0.55rem;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    padding-top: 0.5rem;
  }

  .btn.disabled {
    opacity: 0.45;
    pointer-events: none;
  }

  .hint {
    margin: 0.25rem 0 0;
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-3);
    font-weight: 500;
  }

  .empty-note {
    padding: 1.5rem;
    text-align: center;
  }
</style>

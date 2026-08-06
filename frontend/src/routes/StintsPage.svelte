<script lang="ts">
  /**
   * Tyre life, one stint at a time.
   *
   * Small multiples rather than one chart with a colour per stint. Stints are
   * not really categories to be compared point-for-point — they are separate
   * experiments on different compounds over different lap ranges — and the
   * question each one answers ("how fast is this going off, and do I believe
   * the number?") is answered inside a stint, not across them. One panel per
   * stint means no categorical palette is needed at all, no legend, and each
   * fit gets its own y-scale so a slow compound does not flatten a quick one.
   *
   * Excluded laps are drawn, not dropped. The contract is explicit that they are
   * "flagged, not hidden", and a scatter missing its in-lap is a scatter that
   * quietly lies about how the stint went — so they appear hollow, dimmed, and
   * carry their reason in the tooltip.
   */
  import { fetchStints, type Stint, type StintsResponse } from '../lib/analysis-api';
  import { LOADING, type Async } from '../lib/analysis.svelte';
  import { formatLapTime, DASH } from '../lib/format';
  import {
    fitConfidence,
    formatDegradation,
    formatPercent,
    formatR2
  } from '../lib/analysis-format';
  import { compoundOf } from '../lib/enums';
  import { WHEEL_ORDER } from '../lib/enums';
  import { CHART_INK, LAP, xAxisDistance, yAxis } from '../lib/chart-theme';
  import StatePanel from '../components/StatePanel.svelte';
  import UPlotChart, { type SeriesSpec } from '../components/UPlotChart.svelte';

  interface Props {
    sessionId: number | null;
  }

  let { sessionId }: Props = $props();

  let stints = $state<Async<StintsResponse> | null>(null);
  let attempt = $state(0);

  $effect(() => {
    void attempt;
    const id = sessionId;
    if (id === null) {
      stints = null;
      return;
    }
    const controller = new AbortController();
    stints = LOADING;
    fetchStints(id, controller.signal)
      .then((data) => {
        stints = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        stints = { status: 'error', error };
      });
    return () => controller.abort();
  });

  /**
   * Four series per stint, all describing the same laps with different marks:
   * the fitted line, counted laps, excluded laps hollow, and an invisible row
   * carrying every lap so the shared cursor can read one out at any x.
   */
  function chartFor(stint: Stint): {
    data: Array<Array<number | null>>;
    series: SeriesSpec[];
    range: [number, number];
    offScale: number;
  } {
    const xs = stint.laps.map((l) => l.lap_number);
    const counted = stint.laps.map((l) => (l.excluded ? null : (l.lap_time_ms ?? null)));
    const excluded = stint.laps.map((l) => (l.excluded ? (l.lap_time_ms ?? null) : null));

    // The fit, evaluated across the stint's own lap range.
    const fitted = stint.fit
      ? xs.map((lap) => {
          const f = stint.fit;
          if (!f) return null;
          return f.base_ms + f.deg_ms_per_lap * (lap - stint.lap_start);
        })
      : xs.map(() => null);

    /*
     * Scale to the laps that count.
     *
     * An in-lap is twenty seconds slower than a racing lap. Auto-ranging over
     * every lap therefore spends 90% of the pane on the gap between the pit
     * stop and the race, and squashes the degradation — the one thing this
     * chart exists to show — into a flat line two pixels tall. So the range
     * comes from the counted laps, and any excluded lap that falls outside it
     * is reported in words underneath instead. Nothing is hidden: the excluded
     * laps are still plotted, still listed with their reasons, and still
     * counted in "laps used".
     */
    const values = counted.filter((v): v is number => v != null);
    const lo = values.length > 0 ? Math.min(...values) : 0;
    const hi = values.length > 0 ? Math.max(...values) : 1;
    const pad = Math.max((hi - lo) * 0.25, 400);
    const range: [number, number] = [lo - pad, hi + pad];

    const offScale = stint.laps.filter(
      (l) =>
        l.excluded &&
        l.lap_time_ms != null &&
        (l.lap_time_ms < range[0] || l.lap_time_ms > range[1])
    ).length;

    return {
      range,
      offScale,
      data: [xs, fitted, counted, excluded],
      series: [
        {},
        {
          label: 'Fit',
          stroke: LAP.b,
          width: 2,
          dash: [5, 4],
          scale: 'y',
          points: { show: false },
          format: formatLapTime
        },
        {
          label: 'Counted',
          stroke: 'transparent',
          width: 0,
          scale: 'y',
          points: { show: true, size: 8, fill: CHART_INK.axis, stroke: CHART_INK.axis },
          format: formatLapTime
        },
        {
          label: 'Excluded',
          stroke: 'transparent',
          width: 0,
          scale: 'y',
          points: { show: true, size: 8, fill: '#131519', stroke: CHART_INK.axisFaint, width: 1.5 },
          format: formatLapTime
        }
      ]
    };
  }

  const lapTimeAxis = yAxis({
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => formatLapTime(t))
  });

  const lapAxis = {
    ...xAxisDistance(true),
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => String(Math.round(t)))
  };

  /** Excluded laps, for the reason list under each chart. */
  function exclusions(stint: Stint): Array<{ lap: number; reason: string }> {
    return stint.laps
      .filter((l) => l.excluded)
      .map((l) => ({
        lap: l.lap_number,
        reason: l.exclude_reason ?? (l.valid === false ? 'lap deleted' : 'excluded from the fit')
      }));
  }
</script>

<section class="analysis-page" data-page="stints">
  <header class="page-head">
    <div>
      <h1 class="page-title">Stints</h1>
      <p class="page-sub">
        Lap time against lap number, one panel per stint, with a least-squares fit over the laps
        that count.
      </p>
    </div>
  </header>

  {#if stints === null}
    <div class="panel idle">
      <p class="idle-title">No session selected</p>
      <p class="idle-text">Open a session to see how its tyres went off.</p>
      <a class="btn" href="#/sessions">Browse sessions</a>
    </div>
  {:else}
    <StatePanel
      state={stints}
      loadingLabel="stints"
      isEmpty={(s) => s.stints.length === 0}
      emptyMessage="No stints were recorded for this session."
    >
      {#snippet actions()}
        <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      {/snippet}

      {#snippet children(s)}
        <div class="stints">
          {#each s.stints as stint (stint.stint_no)}
            {@const chart = chartFor(stint)}
            {@const compound = compoundOf(stint.compound_visual)}
            {@const excluded = exclusions(stint)}
            {@const confidence = fitConfidence(stint.fit?.r2)}
            <article class="panel stint" data-stint={stint.stint_no}>
              <div class="panel-head">
                <div class="stint-id">
                  <span
                    class="pill"
                    style="background: {compound.color}; color: {compound.ink}"
                    aria-hidden="true">{compound.code}</span
                  >
                  <span class="stint-name">Stint {stint.stint_no}</span>
                  <span class="label">laps {stint.lap_start}–{stint.lap_end} · {compound.label}</span
                  >
                </div>
              </div>

              <!--
                The two numbers that matter, and a word saying whether the first
                one can be trusted. A degradation slope read off a weak fit is
                worse than no number, so the confidence is not optional chrome.
              -->
              <div class="chip-row">
                <span class="chip-plain big">
                  <span class="k">Deg</span>
                  <span class="clock">{formatDegradation(stint.fit?.deg_ms_per_lap)}</span>
                  <span class="unit">s/lap</span>
                </span>
                <span class="chip-plain conf-{confidence}">
                  <span class="k">r²</span>
                  <span class="clock">{formatR2(stint.fit?.r2)}</span>
                  <span class="unit">{confidence}</span>
                </span>
                <span class="chip-plain">
                  <span class="k">Laps used</span>
                  {stint.fit?.n_used ?? DASH} of {stint.laps.length}
                </span>
              </div>

              <UPlotChart
                pane="stint-{stint.stint_no}"
                data={chart.data}
                series={chart.series}
                height={190}
                note="lap time vs lap number"
                options={{
                  axes: [lapAxis, lapTimeAxis],
                  scales: { x: { time: false }, y: { range: chart.range } }
                }}
              />

              {#if chart.offScale > 0}
                <p class="offscale label">
                  {chart.offScale}
                  {chart.offScale === 1 ? 'excluded lap is' : 'excluded laps are'} off this scale —
                  listed below.
                </p>
              {/if}

              <!-- Wear at the end of the stint, per corner of the car. -->
              {#if stint.wear_end_pct && stint.wear_end_pct.length === 4}
                <div class="wear">
                  <span class="label">Wear at end</span>
                  <div class="chip-row">
                    {#each WHEEL_ORDER as wheel, i (wheel)}
                      <span class="chip-plain">
                        <span class="k">{wheel}</span>
                        <span class="gauge">{formatPercent(stint.wear_end_pct[i])}</span>
                      </span>
                    {/each}
                  </div>
                </div>
              {/if}

              {#if excluded.length > 0}
                <details class="exclusions">
                  <summary class="label">{excluded.length} laps excluded from the fit</summary>
                  <ul>
                    {#each excluded as e (e.lap)}
                      <li>
                        <span class="clock">Lap {e.lap}</span>
                        <span class="reason">{e.reason}</span>
                      </li>
                    {/each}
                  </ul>
                </details>
              {/if}
            </article>
          {/each}
        </div>
      {/snippet}
    </StatePanel>
  {/if}
</section>

<style>
  .stints {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 26rem), 1fr));
    gap: 0.75rem;
    align-items: start;
  }

  .stint {
    gap: 0.45rem;
  }

  .stint-id {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    min-width: 0;
  }

  .stint-name {
    font-size: 1rem;
    font-weight: 700;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.3rem;
    height: 1.3rem;
    border-radius: 50%;
    font-size: 0.72rem;
    font-weight: 800;
    flex: none;
  }

  .chip-plain.big .clock {
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--ink);
  }

  .unit {
    color: var(--ink-3);
    font-size: 0.62rem;
  }

  /*
   * Confidence in the fit is a status, so it borrows the status vocabulary the
   * pit wall already uses: green good, amber caution, red do-not-trust. The word
   * is always present beside the colour.
   */
  .conf-strong {
    border-color: color-mix(in srgb, var(--green) 45%, var(--line));
  }
  .conf-strong .unit {
    color: var(--green);
  }

  .conf-fair {
    border-color: color-mix(in srgb, var(--amber) 45%, var(--line));
  }
  .conf-fair .unit {
    color: var(--amber);
  }

  .conf-weak {
    border-color: color-mix(in srgb, var(--red) 45%, var(--line));
  }
  .conf-weak .unit {
    color: var(--red);
  }

  .wear {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .offscale {
    margin: 0;
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-3);
    font-weight: 500;
  }

  .exclusions summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .exclusions summary:hover {
    color: var(--ink-2);
  }

  .exclusions ul {
    margin: 0.35rem 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .exclusions li {
    display: flex;
    gap: 0.5rem;
    font-size: 0.76rem;
  }

  .exclusions .clock {
    color: var(--ink-2);
    min-width: 4.5rem;
  }

  .reason {
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

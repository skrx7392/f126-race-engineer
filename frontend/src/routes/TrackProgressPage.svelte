<script lang="ts">
  /**
   * One circuit, across the whole career.
   *
   * The question this page answers is "am I getting quicker here?", and the
   * chart is the answer: best lap and median clean lap, one point per visit,
   * labelled "S1 R2"-style. Both series are derived from the `visits` the
   * payload carries — the contract deliberately does not duplicate them as a
   * pre-built series, because the visits *are* the data and a second copy could
   * disagree with the table beside it.
   *
   * The PB card is the other half: the outright best, plus the three best
   * sectors and where each was set. Their sum — the theoretical best — is
   * captioned as exactly that, a derived time never driven as one lap; showing
   * it uncaptioned beside real laps would be the quiet lie this app avoids.
   *
   * A circuit visited once still renders: one point per series is a short
   * career, not an error.
   */
  import {
    fetchCareerTrack,
    type CareerTrackResponse,
    type CareerVisit
  } from '../lib/analysis-api';
  import { LOADING, type Async } from '../lib/analysis.svelte';
  import { href } from '../lib/router.svelte';
  import { DASH, formatLapTime, formatSectorTime } from '../lib/format';
  import {
    formatCvPercent,
    formatPosition,
    formatSeconds,
    formatSessionDate,
    formatSpeed,
    visitLabel
  } from '../lib/analysis-format';
  import { compoundOf } from '../lib/enums';
  import { LAP, xAxisDistance, yAxis } from '../lib/chart-theme';
  import StatePanel from '../components/StatePanel.svelte';
  import UPlotChart, { type SeriesSpec } from '../components/UPlotChart.svelte';

  interface Props {
    trackId: number;
  }

  let { trackId }: Props = $props();

  let track = $state<Async<CareerTrackResponse>>(LOADING);
  /** Bumped to re-run the load effect after a failure. */
  let attempt = $state(0);

  $effect(() => {
    void attempt;
    const id = trackId;
    const controller = new AbortController();
    track = LOADING;
    fetchCareerTrack(id, controller.signal)
      .then((data) => {
        track = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        track = { status: 'error', error };
      });
    return () => controller.abort();
  });

  /**
   * The evolution chart, derived from the visits. X is the visit index; the
   * axis prints "S1 R2"-style labels instead of the number. The y-range comes
   * from the values themselves with the same padding rule the stints page uses,
   * so a season of near-identical laps still shows its slope.
   */
  function chartFor(visits: CareerVisit[]): {
    data: Array<Array<number | null>>;
    series: SeriesSpec[];
    yRange: [number, number];
  } {
    const xs = visits.map((_, i) => i);
    const best = visits.map((v) => v.best_lap_ms);
    const median = visits.map((v) => v.consistency?.median_ms ?? null);

    const values = [...best, ...median].filter((v): v is number => v !== null);
    const lo = values.length > 0 ? Math.min(...values) : 0;
    const hi = values.length > 0 ? Math.max(...values) : 1;
    const pad = Math.max((hi - lo) * 0.25, 400);

    // Points are always drawn: a single-visit circuit has no line to show, and
    // even with several, the visit is the datum — the line only connects them.
    const dot = (label: string, color: string): SeriesSpec => ({
      label,
      stroke: color,
      width: 2,
      scale: 'y',
      points: { show: true, size: 7, fill: color, stroke: color },
      format: formatLapTime
    });

    return {
      data: [xs, best, median],
      yRange: [lo - pad, hi + pad],
      series: [{}, dot('Best lap', LAP.a), dot('Median clean lap', LAP.b)]
    };
  }

  /** X axis printing visit labels on the integer ticks and nothing between. */
  function visitAxis(visits: CareerVisit[]): Record<string, unknown> {
    return {
      ...xAxisDistance(true),
      splits: visits.map((_, i) => i),
      values: (_u: unknown, ticks: number[]) =>
        ticks.map((t) => {
          const v = visits[Math.round(t)];
          return v !== undefined && Math.abs(t - Math.round(t)) < 1e-6
            ? visitLabel(v.season, v.round)
            : '';
        })
    };
  }

  const lapTimeAxis = yAxis({
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => formatLapTime(t))
  });

  /** "S2 L4" for a sector's provenance, or nothing when it is unknown. */
  function setAt(sessionId: number | null, lap: number | null): string {
    if (sessionId === null || lap === null) return DASH;
    return `S${sessionId} L${lap}`;
  }
</script>

<section class="analysis-page" data-page="careertrack">
  <StatePanel
    state={track}
    loadingLabel="this circuit's career"
    isEmpty={() => false}
  >
    {#snippet actions()}
      <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      <a class="btn" href={href('/career')}>Back to career</a>
    {/snippet}

    {#snippet children(data)}
      {@const chart = chartFor(data.visits)}
      <header class="page-head">
        <div>
          <h1 class="page-title">{data.track_name ?? `Track ${data.track_id}`}</h1>
          <p class="page-sub">
            Career progress at this circuit: every visit, every session, and the personal
            best with the sectors that would beat it.
          </p>
        </div>
        <nav class="crumbs" aria-label="Circuit pages">
          <a class="btn" href={href('/career')}>Career</a>
          <a class="btn" href={href('/strategy', { track: data.track_id })}>Strategy</a>
        </nav>
      </header>

      <!-- The PB and its parts. -->
      <article class="panel pb" data-testid="pb-card">
        {#if data.pb}
          <div class="pb-head">
            <div class="figure">
              <span class="label">Personal best</span>
              <span class="big clock">{formatLapTime(data.pb.best_lap_ms)}</span>
            </div>
            <div class="pb-meta">
              {#if data.pb.compound_visual !== null}
                {@const compound = compoundOf(data.pb.compound_visual)}
                <span
                  class="pill"
                  style="background: {compound.color}; color: {compound.ink}"
                  aria-hidden="true">{compound.code}</span
                >
                <span>{data.pb.compound_name ?? compound.label}</span>
              {/if}
              <span class="sub">
                {data.pb.session_label ?? DASH}{data.pb.lap_number === null
                  ? ''
                  : ` · lap ${data.pb.lap_number}`} · {formatSessionDate(data.pb.set_at_wall)}
              </span>
            </div>
            <div class="figure">
              <span class="label">Top speed</span>
              <span class="big gauge">{formatSpeed(data.pb.top_speed_kmh)}</span>
              <span class="unit">km/h</span>
            </div>
          </div>

          <div class="chip-row sectors" data-testid="pb-sectors">
            {#each [
              {
                n: 1,
                ms: data.pb.sectors?.s1_ms ?? null,
                sid: data.pb.sectors?.s1_session_id ?? null,
                lap: data.pb.sectors?.s1_lap_number ?? null
              },
              {
                n: 2,
                ms: data.pb.sectors?.s2_ms ?? null,
                sid: data.pb.sectors?.s2_session_id ?? null,
                lap: data.pb.sectors?.s2_lap_number ?? null
              },
              {
                n: 3,
                ms: data.pb.sectors?.s3_ms ?? null,
                sid: data.pb.sectors?.s3_session_id ?? null,
                lap: data.pb.sectors?.s3_lap_number ?? null
              }
            ] as sector (sector.n)}
              <span class="chip-plain">
                <span class="k">S{sector.n}</span>
                <span class="clock">{formatSectorTime(sector.ms)}</span>
                <span class="unit">set {setAt(sector.sid, sector.lap)}</span>
              </span>
            {/each}
            <span class="chip-plain theoretical" data-testid="pb-theoretical">
              <span class="k">Theoretical best</span>
              <span class="clock">{formatLapTime(data.pb.theoretical_ms)}</span>
            </span>
          </div>
          <p class="hint">
            Theoretical best is the sum of the three best valid sectors above — a derived
            time, never driven as one lap.
          </p>
        {:else}
          <p class="empty-pb">No completed laps at this circuit yet.</p>
        {/if}
      </article>

      <!-- The evolution chart, one point per visit. -->
      {#if data.visits.length > 0}
        <div class="panel">
          <UPlotChart
            pane="career-evolution"
            data={chart.data}
            series={chart.series}
            height={220}
            title="Lap evolution"
            note="best and median clean lap per visit · m:ss.mmm"
            options={{
              axes: [visitAxis(data.visits), lapTimeAxis],
              scales: {
                x: { time: false, range: [-0.5, Math.max(0.5, data.visits.length - 0.5)] },
                y: { range: chart.yRange }
              }
            }}
          />
          {#if data.visits.length === 1}
            <p class="hint" data-testid="single-visit-note">
              One visit so far — the chart gains a line when you come back.
            </p>
          {/if}
        </div>
      {:else}
        <div class="panel state-note" data-testid="no-visits">
          <p class="hint">
            No career weekends at this circuit — everything recorded here is a time trial,
            which counts for the personal best but is not a round.
          </p>
        </div>
      {/if}

      <!-- The visits behind the chart. -->
      <h2 class="section-title">Visits</h2>
      <div class="panel">
        <div class="table-scroll">
          <table class="data-table" data-testid="visit-table">
            <caption class="label">
              Career weekends at this circuit. Consistency is the grand prix's
              representative laps: valid, non-in/out, within 107% of the median.
            </caption>
            <thead>
              <tr>
                <th scope="col">Visit</th>
                <th scope="col">Date</th>
                <th scope="col" class="num">Best lap</th>
                <th scope="col">Quali</th>
                <th scope="col">Sprint</th>
                <th scope="col">Race</th>
                <th scope="col">Consistency</th>
              </tr>
            </thead>
            <tbody>
              {#each data.visits as v (`${v.season}-${v.round}`)}
                <tr data-visit="{v.season}-{v.round}">
                  <td class="visit-id">{visitLabel(v.season, v.round)}</td>
                  <td>{formatSessionDate(v.started_at_wall)}</td>
                  <td class="num clock">
                    {formatLapTime(v.best_lap_ms)}
                    {#if v.best_lap_session_id !== null}
                      <span class="sub">session {v.best_lap_session_id}</span>
                    {/if}
                  </td>
                  <td>{formatPosition(v.quali?.position ?? null)}</td>
                  <td>{formatPosition(v.sprint?.position ?? null)}</td>
                  <td>
                    {#if v.race}
                      {formatPosition(v.race.position)}
                      {#if v.race.points !== null}
                        <span class="sub">{v.race.points} pts</span>
                      {/if}
                    {:else}
                      {DASH}
                    {/if}
                  </td>
                  <td>
                    {#if v.consistency && v.consistency.median_ms !== null}
                      <span class="clock">{formatLapTime(v.consistency.median_ms)}</span>
                      <span class="sub"
                        >±{formatSeconds(v.consistency.iqr_ms)} s ·
                        {formatCvPercent(v.consistency.cv_pct)}</span
                      >
                    {:else}
                      {DASH}
                    {/if}
                  </td>
                </tr>
              {/each}
              {#if data.visits.length === 0}
                <tr>
                  <td colspan="7" class="none">No career visits.</td>
                </tr>
              {/if}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Every session, time trials included, with per-session consistency. -->
      <h2 class="section-title">
        Sessions
        <span class="label">{data.sessions.length} at this circuit · time trials included</span>
      </h2>
      <div class="panel">
        <div class="table-scroll">
          <table class="data-table" data-testid="session-table">
            <caption class="label">
              Per-session consistency over the representative laps; an em dash means the
              session recorded nothing to measure.
            </caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Type</th>
                <th scope="col" class="num">Best lap</th>
                <th scope="col" class="num" title="Representative laps used, of laps driven">Used</th>
                <th scope="col" class="num">Median</th>
                <th scope="col" class="num">IQR s</th>
                <th scope="col" class="num">CV</th>
                <th scope="col" class="num">Top speed km/h</th>
              </tr>
            </thead>
            <tbody>
              {#each data.sessions as s (s.id)}
                <tr class="row" data-session-id={s.id}>
                  <td class="link-cell">
                    <a class="row-link" href={href(`/sessions/${s.id}`)}>
                      {formatSessionDate(s.started_at_wall)}
                    </a>
                  </td>
                  <td>{s.session_type_name ?? DASH}</td>
                  <td class="num clock">{formatLapTime(s.best_lap_ms)}</td>
                  <td class="num">
                    {s.laps_used ?? DASH}{s.laps_total === null ? '' : ` of ${s.laps_total}`}
                  </td>
                  <td class="num clock">{formatLapTime(s.median_ms)}</td>
                  <td class="num clock">{formatSeconds(s.iqr_ms)}</td>
                  <td class="num gauge">{formatCvPercent(s.cv_pct)}</td>
                  <td class="num gauge">{formatSpeed(s.top_speed_kmh)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/snippet}
  </StatePanel>
</section>

<style>
  .section-title {
    margin: 0.4rem 0 0;
    font-size: 1rem;
    font-weight: 700;
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  .crumbs {
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }

  .pb {
    border-color: var(--line-strong);
  }

  .pb-head {
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
    font-size: 2.1rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.03em;
    color: var(--ink);
  }

  .unit {
    color: var(--ink-3);
    font-size: 0.72rem;
  }

  .pb-meta {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
    font-size: 0.85rem;
    color: var(--ink-2);
    flex: 1 1 16rem;
    min-width: 0;
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

  .sectors {
    margin-top: 0.2rem;
  }

  /* The derived time is boxed differently from the measured ones beside it. */
  .theoretical {
    border-style: dashed;
  }

  .sub {
    display: block;
    font-size: 0.72rem;
    color: var(--ink-3);
    font-weight: 500;
  }

  .hint {
    margin: 0.2rem 0 0;
    font-size: 0.76rem;
    color: var(--ink-3);
  }

  .empty-pb {
    margin: 0;
    font-size: 0.85rem;
    color: var(--ink-3);
  }

  .state-note {
    align-items: flex-start;
  }

  .visit-id {
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .none {
    color: var(--ink-3);
  }

  /* Row-link pattern, as the session browser draws it. */
  .row {
    position: relative;
  }

  .row-link {
    color: var(--ink);
    text-decoration: none;
    font-weight: 600;
  }

  .row-link::after {
    content: '';
    position: absolute;
    inset: 0;
  }

  .row:focus-within td {
    background: var(--surface-2);
  }

  .row-link:focus-visible::after {
    outline: 2px solid var(--line-strong);
    outline-offset: -2px;
  }
</style>

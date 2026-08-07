<script lang="ts">
  /**
   * The career, season by season.
   *
   * Everything on this page is *derived*: the game never says "season 2, round
   * 1", so the backend works it out from the calendar shape of the recordings —
   * weekends are consecutive same-track runs, a season increments when a
   * circuit repeats — and this page's job is to show those numbers **and say
   * how they were made**. The derivation notes come out of the payload itself
   * and are printed, small but visible, under the table they explain. A pinned
   * tag (`f126 tag`, the one manual override that exists) is marked on its
   * weekend, and a weekend whose tags disagree carries a visible warning rather
   * than a silently-resolved number.
   *
   * The other rule is the app-wide one: a missing classification packet is a
   * missing fact. A race without one still appears — it happened — but its
   * position, points and status are em-dashes, and the season totals are the
   * sum of what the weekends actually show, never a guess over the gap.
   */
  import {
    fetchCareerOverview,
    type CareerOverview,
    type CareerTotals,
    type CareerWeekend
  } from '../lib/analysis-api';
  import { LOADING, type Async } from '../lib/analysis.svelte';
  import { href } from '../lib/router.svelte';
  import { DASH, formatLapTime } from '../lib/format';
  import {
    formatCvPercent,
    formatPosition,
    formatSeconds,
    formatSessionDate,
    formatSpeed
  } from '../lib/analysis-format';
  import { compoundOf } from '../lib/enums';
  import StatePanel from '../components/StatePanel.svelte';

  let overview = $state<Async<CareerOverview>>(LOADING);
  /** Bumped to re-run the load effect after a failure. */
  let attempt = $state(0);

  $effect(() => {
    void attempt;
    const controller = new AbortController();
    overview = LOADING;
    fetchCareerOverview(controller.signal)
      .then((data) => {
        overview = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        overview = { status: 'error', error };
      });
    return () => controller.abort();
  });

  /** The six counters, in the order a driver reads a season summary. */
  const TOTAL_KEYS: ReadonlyArray<{ key: keyof CareerTotals; label: string }> = [
    { key: 'points', label: 'Points' },
    { key: 'wins', label: 'Wins' },
    { key: 'podiums', label: 'Podiums' },
    { key: 'poles', label: 'Poles' },
    { key: 'fastest_laps', label: 'Fastest laps' },
    { key: 'sprint_wins', label: 'Sprint wins' }
  ];

  /** "P2 from P6", or as much of it as the classification actually recorded. */
  function finishAndGrid(position: number | null, grid: number | null): string {
    const p = formatPosition(position);
    return grid === null ? p : `${p} from ${formatPosition(grid)}`;
  }

  /** A weekend's non-finish status, when there is one worth flagging. */
  function statusOf(w: CareerWeekend): string | null {
    const status = w.race?.status ?? null;
    return status !== null && status !== 'finished' ? status : null;
  }
</script>

<section class="analysis-page" data-page="career">
  <header class="page-head">
    <div>
      <h1 class="page-title">Career</h1>
      <p class="page-sub">
        Seasons, weekends and personal bests, derived from the sessions you recorded. The
        season and round numbers are computed — the rules are printed under the table.
      </p>
    </div>
  </header>

  <StatePanel
    state={overview}
    loadingLabel="the career overview"
    isEmpty={(d) => d.seasons.length === 0 && d.pbs.length === 0}
    emptyMessage="No career sessions recorded yet. Drive a weekend with the recorder running and it will appear here."
  >
    {#snippet actions()}
      <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
    {/snippet}

    {#snippet children(data)}
      <!-- The whole career, before any one season. -->
      <article class="panel totals" data-testid="career-totals">
        <p class="label">Career to date · {data.career_totals.races} grands prix raced</p>
        <div class="figures">
          {#each TOTAL_KEYS as t (t.key)}
            <div class="figure">
              <span class="label">{t.label}</span>
              <span class="big gauge">{data.career_totals[t.key]}</span>
            </div>
          {/each}
        </div>
      </article>

      {#each data.seasons as season (season.season)}
        <section class="season" data-season={season.season}>
          <h2 class="section-title">
            Season {season.season}
            <span class="label">{season.rounds} rounds · {season.totals.races} raced</span>
          </h2>

          <div class="chip-row" data-testid="season-totals-{season.season}">
            {#each TOTAL_KEYS as t (t.key)}
              <span class="chip-plain">
                <span class="k">{t.label}</span>
                <span class="gauge">{season.totals[t.key]}</span>
              </span>
            {/each}
          </div>

          <div class="panel">
            <div class="table-scroll">
              <table class="data-table" data-testid="weekend-table-{season.season}">
                <caption class="label">
                  Derived season and round — computed from the weekend rule below, not
                  recorded by the game. A pinned tag overrides the derivation and is marked.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Rnd</th>
                    <th scope="col">Track</th>
                    <th scope="col">Format</th>
                    <th scope="col">Quali</th>
                    <th scope="col">Sprint</th>
                    <th scope="col">Race</th>
                    <th scope="col" class="num">Points</th>
                    <th scope="col">Race consistency</th>
                  </tr>
                </thead>
                <tbody>
                  {#each season.weekends as w (`${w.season}-${w.round}-${w.track_id}`)}
                    <tr class="row" data-weekend="{w.season}-{w.round}">
                      <td class="num">
                        {w.round}
                        {#if w.tags}
                          <span
                            class="tier tier-pinned"
                            data-testid="tag-pinned"
                            title={w.tags.note ?? 'pinned by f126 tag'}
                            >pinned S{w.tags.season}{w.tags.round === null
                              ? ''
                              : ` R${w.tags.round}`}</span
                          >
                        {/if}
                        {#if w.tag_conflict}
                          <span
                            class="tier tier-conflict"
                            data-testid="tag-conflict"
                            title="This weekend's sessions carry disagreeing tags; the newest one wins and the older rows are still in the table"
                            >tag conflict</span
                          >
                        {/if}
                      </td>
                      <td class="link-cell">
                        <a class="row-link" href={href(`/career/tracks/${w.track_id}`)}>
                          {w.track_name ?? `Track ${w.track_id}`}
                        </a>
                        <span class="sub">{formatSessionDate(w.started_at_wall)}</span>
                      </td>
                      <td>
                        <span class="tier tier-format-{w.format}">{w.format}</span>
                      </td>
                      <td class="quali">
                        {#if w.quali}
                          {formatPosition(w.quali.position)}
                          <span class="sub clock">{formatLapTime(w.quali.best_lap_ms)}</span>
                        {:else}
                          {DASH}
                        {/if}
                      </td>
                      <td class="sprint">
                        {#if w.sprint}
                          {finishAndGrid(w.sprint.position, w.sprint.grid_position)}
                          {#if w.sprint.points !== null}
                            <span class="sub">{w.sprint.points} pts</span>
                          {/if}
                        {:else}
                          {DASH}
                        {/if}
                      </td>
                      <td class="race">
                        {#if w.race}
                          {finishAndGrid(w.race.position, w.race.grid_position)}
                          {#if w.race.fastest_lap === true}
                            <span class="tier tier-fl" title="Fastest lap of the race">FL</span>
                          {/if}
                          {#if statusOf(w)}
                            <span class="sub status">{statusOf(w)}</span>
                          {/if}
                        {:else}
                          {DASH}
                        {/if}
                      </td>
                      <td class="num points">{w.points ?? DASH}</td>
                      <td class="consistency">
                        {#if w.consistency && w.consistency.median_ms !== null}
                          <span class="clock">{formatLapTime(w.consistency.median_ms)}</span>
                          <span class="sub"
                            >±{formatSeconds(w.consistency.iqr_ms)} s ·
                            {formatCvPercent(w.consistency.cv_pct)} over
                            {w.consistency.laps_used} laps</span
                          >
                        {:else}
                          {DASH}
                        {/if}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      {/each}

      <!-- How the season and round numbers above were arrived at. -->
      <footer class="panel notes" data-testid="career-notes">
        <p class="label">How these numbers were derived</p>
        <ul>
          {#each Object.entries(data.notes) as [key, text] (key)}
            <li>
              <span class="note-key">{key.replaceAll('_', ' ')}</span>
              <span class="note-text">{text}</span>
            </li>
          {/each}
        </ul>
        {#if data.untracked_sessions > 0}
          <p class="hint" data-testid="untracked">
            {data.untracked_sessions}
            {data.untracked_sessions === 1 ? 'session is' : 'sessions are'} not part of any
            round (time trials, unknown circuits). They still count for the personal bests
            below.
          </p>
        {/if}
      </footer>

      <!-- Personal bests, one row per circuit ever driven. -->
      <h2 class="section-title">
        Personal bests
        <span class="label">{data.pbs.length} circuits · first-visit order</span>
      </h2>
      <div class="panel">
        <div class="table-scroll">
          <table class="data-table" data-testid="pb-board">
            <caption class="label">
              Theoretical best is the sum of the circuit's best valid sectors — a derived
              time, never driven as one lap. Time trials count here.
            </caption>
            <thead>
              <tr>
                <th scope="col">Track</th>
                <th scope="col" class="num">Best lap</th>
                <th scope="col">Compound</th>
                <th scope="col" class="num">Theoretical</th>
                <th scope="col" class="num">Top speed km/h</th>
                <th scope="col">Set in</th>
              </tr>
            </thead>
            <tbody>
              {#each data.pbs as pb (pb.track_id)}
                {@const compound = compoundOf(pb.compound_visual)}
                <tr class="row" data-pb-track={pb.track_id}>
                  <td class="link-cell">
                    <a class="row-link" href={href(`/career/tracks/${pb.track_id}`)}>
                      {pb.track_name ?? `Track ${pb.track_id}`}
                    </a>
                  </td>
                  <td class="num clock">{formatLapTime(pb.best_lap_ms)}</td>
                  <td>
                    {#if pb.compound_visual !== null}
                      <span
                        class="pill"
                        style="background: {compound.color}; color: {compound.ink}"
                        aria-hidden="true">{compound.code}</span
                      >
                      {pb.compound_name ?? compound.label}
                    {:else}
                      {DASH}
                    {/if}
                  </td>
                  <td class="num clock">
                    {formatLapTime(pb.theoretical_ms)}
                    {#if pb.theoretical_ms !== null}
                      <span class="sub">theoretical</span>
                    {/if}
                  </td>
                  <td class="num gauge">{formatSpeed(pb.top_speed_kmh)}</td>
                  <td class="setin">
                    {#if pb.session_label}
                      {pb.session_label}
                      {#if pb.lap_number !== null}
                        <span class="sub">lap {pb.lap_number} · {formatSessionDate(pb.set_at_wall)}</span>
                      {/if}
                    {:else}
                      {DASH}
                    {/if}
                  </td>
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

  .season {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }

  .totals {
    border-color: var(--line-strong);
  }

  .figures {
    display: flex;
    gap: 1.4rem;
    flex-wrap: wrap;
  }

  .figure {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .big {
    font-size: 1.9rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.03em;
    color: var(--ink);
  }

  /* The row-link pattern from the session browser: a real link, stretched. */
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

  /* Secondary line inside a cell: a date, a lap time, a points count. */
  .sub {
    display: block;
    font-size: 0.72rem;
    color: var(--ink-3);
    font-weight: 500;
  }

  .status {
    color: var(--amber);
  }

  /* The same word-in-a-box vocabulary the strategy page uses. */
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

  .tier-format-sprint {
    color: var(--purple);
    border-color: color-mix(in srgb, var(--purple) 45%, var(--line));
  }

  /* A pin is information; a conflict is a warning. Amber = degraded, as ever. */
  .tier-pinned {
    color: var(--blue);
    border-color: color-mix(in srgb, var(--blue) 45%, var(--line));
    text-transform: none;
    letter-spacing: 0.03em;
  }

  .tier-conflict {
    color: var(--amber);
    border-color: color-mix(in srgb, var(--amber) 45%, var(--line));
  }

  .tier-fl {
    color: var(--purple);
    border-color: color-mix(in srgb, var(--purple) 45%, var(--line));
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

  .notes ul {
    margin: 0.25rem 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .notes li {
    font-size: 0.76rem;
    color: var(--ink-3);
  }

  .note-key {
    display: inline-block;
    min-width: 8.5rem;
    font-weight: 700;
    color: var(--ink-2);
    text-transform: capitalize;
  }

  .note-text {
    white-space: normal;
  }

  .hint {
    margin: 0.2rem 0 0;
    font-size: 0.76rem;
    color: var(--ink-3);
  }
</style>

<script lang="ts">
  /**
   * Where the lap time went, corner by corner.
   *
   * The table is the answer and the traces are the evidence, so they share a
   * page and a zoom: clicking a corner narrows every pane below to that corner's
   * window, through the same shared state the compare page reads. That is the
   * whole reason the window lives in `analysis.svelte.ts` rather than in a
   * component — two pages own the same view of the same lap.
   *
   * Time loss is a diverging quantity and gets a diverging encoding: a bar
   * growing from a centre datum, right for lost, left for gained. Colour is the
   * secondary channel; the side of the datum and the signed numeral both carry
   * the direction independently, which is what makes it readable without colour
   * vision. The rows are sorted by loss, biggest first, because the question
   * being asked is "where am I losing the most" and a table sorted by corner
   * number makes you find that yourself.
   */
  import {
    fetchCompare,
    fetchCorners,
    fetchLaps,
    fetchSessions,
    type CompareResponse,
    type Corner,
    type CornersResponse,
    type Lap,
    type SessionSummary
  } from '../lib/analysis-api';
  import { href, router } from '../lib/router.svelte';
  import { LOADING, analysis, type Async } from '../lib/analysis.svelte';
  import { formatDelta, DASH } from '../lib/format';
  import {
    CORNER_KIND_LABEL,
    formatMetres,
    formatMetresDelta,
    formatSpeed,
    formatSpeedDelta,
    lapLabel
  } from '../lib/analysis-format';
  import CompareStack from '../components/CompareStack.svelte';
  import StatePanel from '../components/StatePanel.svelte';

  interface Props {
    sessionId: number | null;
    lap: number | null;
    ref: string;
    refSession?: number | null;
  }

  let { sessionId, lap, ref, refSession = null }: Props = $props();

  let corners = $state<Async<CornersResponse> | null>(null);
  let traces = $state<Async<CompareResponse> | null>(null);
  let attempt = $state(0);
  /** Corner-number sort instead of loss sort, for reading the lap in order. */
  let sortByNumber = $state(false);

  /* The reference picker needs the session list (to offer same-circuit sessions)
     and, once one is chosen, that session's laps. Both are best-effort: the page
     is still fully usable on the default `ref=best` if either fetch fails. */
  let sessionList = $state<SessionSummary[]>([]);
  let refLaps = $state<Lap[]>([]);

  $effect(() => {
    const controller = new AbortController();
    fetchSessions(50, controller.signal)
      .then((data) => {
        sessionList = data;
      })
      .catch(() => {
        sessionList = [];
      });
    return () => controller.abort();
  });

  let subjectSession = $derived(sessionList.find((s) => s.id === sessionId) ?? null);

  /** Sessions at the same circuit — the only ones a reference lap can come from. */
  let sameTrackSessions = $derived.by(() => {
    if (!subjectSession) return sessionList;
    return sessionList.filter((s) => s.track_id === subjectSession.track_id);
  });

  /** 'best' | 'track_best' | 'lap' — which of the three modes the URL is asking for. */
  let refMode = $derived(ref === 'best' || ref === 'track_best' ? ref : 'lap');

  /** The session an explicit lap reference is read from; defaults to the subject's. */
  let pickedRefSession = $derived(refSession ?? sessionId);

  $effect(() => {
    const id = pickedRefSession;
    if (refMode !== 'lap' || id === null) {
      refLaps = [];
      return;
    }
    const controller = new AbortController();
    fetchLaps(id, null, controller.signal)
      .then((data) => {
        refLaps = data;
      })
      .catch(() => {
        refLaps = [];
      });
    return () => controller.abort();
  });

  /** Narrow an all-cars lap list to the player's, the way the compare page does. */
  let refLapOptions = $derived.by(() => {
    const session = sessionList.find((s) => s.id === pickedRefSession) ?? null;
    const car = session?.player_car_index ?? refLaps[0]?.car_index ?? null;
    return refLaps.filter((l) => car === null || l.car_index === car);
  });

  function goRef(nextRef: string, nextRefSession: number | null = null): void {
    router.go(
      href('/corners', {
        session: sessionId,
        lap,
        ref: nextRef,
        ref_session: nextRefSession
      })
    );
  }

  function onModeChange(mode: string): void {
    if (mode === 'best' || mode === 'track_best') {
      goRef(mode);
      return;
    }
    // Switching to an explicit lap: start from the subject's own session, lap 1.
    goRef('1', sessionId);
  }


  $effect(() => {
    void attempt;
    const id = sessionId;
    const l = lap;
    if (id === null || l === null) {
      corners = null;
      return;
    }
    const controller = new AbortController();
    corners = LOADING;
    fetchCorners(id, l, ref, controller.signal, refSession)
      .then((data) => {
        corners = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        corners = { status: 'error', error };
      });
    return () => controller.abort();
  });

  /* The traces beneath, always lap-versus-its-reference so the panes and the
     table are describing the same pair of laps. */
  $effect(() => {
    void attempt;
    const c = corners;
    if (c?.status !== 'ok') {
      traces = null;
      return;
    }
    const { session_id, lap_number, ref_lap_number } = c.data;
    if (ref_lap_number === null) {
      // Nothing to compare against — the table renders its subject-only columns and
      // there is no second trace to draw.
      traces = null;
      return;
    }
    const refSession = c.data.ref_session_id ?? session_id;
    const controller = new AbortController();
    traces = LOADING;
    fetchCompare(session_id, lap_number, refSession, ref_lap_number, controller.signal)
      .then((data) => {
        traces = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        traces = { status: 'error', error };
      });
    return () => controller.abort();
  });

  let data = $derived(corners?.status === 'ok' ? corners.data : null);

  /** How the active reference reads in the header, e.g. "One-Shot Qualifying · Aug 07 L1". */
  let refCaption = $derived.by(() => {
    const d = data;
    if (!d || d.self_reference || d.ref_lap_number === null) return null;
    const crossSession =
      d.ref_session_id !== undefined &&
      d.ref_session_id !== null &&
      d.ref_session_id !== d.session_id;
    if (crossSession) {
      return `${d.ref_session_label ?? `S${d.ref_session_id}`} L${d.ref_lap_number}`;
    }
    return `lap ${d.ref_lap_number}`;
  });

  /** Widest absolute loss, so every bar is drawn on one common scale. */
  let scaleMs = $derived.by(() => {
    const list = data?.corners ?? [];
    const worst = list.reduce((m, c) => Math.max(m, Math.abs(c.time_loss_ms)), 0);
    // A floor keeps a tidy lap from rendering fifteen full-width bars.
    return Math.max(worst, 120);
  });

  let sorted = $derived.by(() => {
    const list = [...(data?.corners ?? [])];
    return sortByNumber
      ? list.sort((a, b) => a.n - b.n)
      : list.sort((a, b) => b.time_loss_ms - a.time_loss_ms);
  });

  function focus(corner: Corner): void {
    if (analysis.focusedCorner === corner.n) {
      analysis.clearWindow();
      return;
    }
    analysis.focusCorner(corner.n, corner.entry_m, corner.exit_m);
  }

  /** Bar geometry: fraction of the half-width, signed. */
  function barWidth(ms: number): number {
    return Math.min(100, (Math.abs(ms) / scaleMs) * 100);
  }
</script>

<section class="analysis-page" data-page="corners">
  <header class="page-head">
    <div>
      <h1 class="page-title">Corners</h1>
      {#if data && refCaption}
        <p class="page-sub">
          {lapLabel(data.session_id, data.lap_number)} versus {refCaption} · positive means slower
          than the reference through that corner.
        </p>
      {:else if data}
        <p class="page-sub">
          {lapLabel(data.session_id, data.lap_number)} · corner map only, with no reference to
          measure against.
        </p>
      {:else}
        <p class="page-sub">Select a lap to break its time down corner by corner.</p>
      {/if}
    </div>

    {#if data}
      <div class="chip-row">
        <span class="chip-plain">
          <span class="k">Total</span>
          <span class="clock">{formatDelta(data.total_delta_ms)}</span>
        </span>
        <span class="chip-plain">
          <span class="k">Straights</span>
          <span class="clock">{formatDelta(data.straights_time_loss_ms)}</span>
        </span>
        <button class="btn" type="button" onclick={() => (sortByNumber = !sortByNumber)}>
          Sort: {sortByNumber ? 'corner number' : 'biggest loss'}
        </button>
      </div>
    {/if}
  </header>

  {#if sessionId !== null && lap !== null}
    <div class="panel ref-picker" data-panel="reference">
      <label class="picker">
        <span class="label">Reference</span>
        <select
          aria-label="Reference lap source"
          value={refMode}
          onchange={(e) => onModeChange(e.currentTarget.value)}
        >
          <option value="best">Session best</option>
          <option value="track_best">Track best · any session here</option>
          <option value="lap">Specific lap…</option>
        </select>
      </label>

      {#if refMode === 'lap'}
        <label class="picker">
          <span class="label">From session</span>
          <select
            aria-label="Reference session"
            value={pickedRefSession ?? ''}
            onchange={(e) => goRef('1', Number(e.currentTarget.value))}
          >
            {#each sameTrackSessions as s (s.id)}
              <option value={s.id}>
                {s.session_type_name ?? 'Session'} · #{s.id}
              </option>
            {/each}
          </select>
        </label>

        <label class="picker">
          <span class="label">Lap</span>
          <select
            aria-label="Reference lap"
            value={ref}
            onchange={(e) => goRef(e.currentTarget.value, pickedRefSession)}
          >
            {#each refLapOptions as l (l.lap_number)}
              <option value={String(l.lap_number)}>L{l.lap_number}</option>
            {/each}
          </select>
        </label>
      {/if}

      {#if data?.self_reference}
        <p class="ref-note" data-testid="self-reference-note">
          Only one lap has telemetry — showing the corner map without reference deltas.
        </p>
      {/if}
    </div>
  {/if}

  {#if corners === null}
    <div class="panel idle">
      <p class="idle-title">No lap selected</p>
      <p class="idle-text">
        Pick a lap on a session page and come back, or open a session to choose one.
      </p>
      <a class="btn" href="#/sessions">Browse sessions</a>
    </div>
  {:else}
    <StatePanel
      state={corners}
      loadingLabel="corner analysis"
      isEmpty={(c) => c.corners.length === 0}
      emptyMessage="No corners were detected on this lap."
    >
      {#snippet actions()}
        <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      {/snippet}

      {#snippet children(c)}
        <div class="panel">
          <div class="table-scroll">
            <table class="data-table" data-testid="corners-table">
              <caption class="label">
                {c.corners.length} corners · click a row to zoom the traces to it
              </caption>
              <thead>
                <tr>
                  <th scope="col" class="num">#</th>
                  <th scope="col">Kind</th>
                  <th scope="col" class="num">Min speed</th>
                  <th scope="col" class="num">vs ref</th>
                  <th scope="col" class="num">Brake point</th>
                  <th scope="col" class="num">vs ref</th>
                  <th scope="col" class="loss-col">Time loss</th>
                </tr>
              </thead>
              <tbody>
                {#each sorted as corner (corner.n)}
                  <!-- A self-reference compares a lap with itself, so every delta is
                       exactly zero. Showing a column of ±0.000 reads like a real,
                       perfectly-matched result; a dash says "no answer" honestly. -->
                  {@const speedDelta =
                    !c.self_reference &&
                    corner.min_speed_kmh !== null &&
                    corner.ref_min_speed_kmh !== null
                      ? corner.min_speed_kmh - corner.ref_min_speed_kmh
                      : null}
                  {@const brakeDelta =
                    !c.self_reference &&
                    corner.brake_point_m !== null &&
                    corner.ref_brake_point_m !== null
                      ? corner.brake_point_m - corner.ref_brake_point_m
                      : null}
                  <tr
                    class="selectable"
                    class:is-selected={analysis.focusedCorner === corner.n}
                    data-corner={corner.n}
                  >
                    <td class="num">
                      <button
                        class="pick"
                        type="button"
                        onclick={() => focus(corner)}
                        aria-pressed={analysis.focusedCorner === corner.n}
                      >
                        T{corner.n}
                      </button>
                    </td>
                    <td>
                      <span class="chip-plain kind-{corner.kind}">
                        {CORNER_KIND_LABEL[corner.kind] ?? corner.kind}
                      </span>
                    </td>
                    <td class="num gauge-cell">{formatSpeed(corner.min_speed_kmh)}</td>
                    <td
                      class="num gauge-cell"
                      class:down={speedDelta !== null && speedDelta < -0.05}
                      class:up={speedDelta !== null && speedDelta > 0.05}
                    >
                      {formatSpeedDelta(speedDelta)}
                    </td>
                    <td class="num">{formatMetres(corner.brake_point_m)}</td>
                    <!--
                      Positive = braked further down the road than the reference.
                      Deliberately uncoloured: braking later is not better or
                      worse on its own — it is quicker into the corner and
                      slower out of it, and which one won is what the time-loss
                      column already says. Colouring it would assert a verdict
                      the number does not support.
                    -->
                    <td class="num" title="Positive means braking later than the reference">
                      {formatMetresDelta(brakeDelta)}
                    </td>
                    <td class="loss-col">
                      <div class="loss">
                        <div class="bar-track">
                          <span class="datum"></span>
                          {#if corner.time_loss_ms >= 0}
                            <span
                              class="bar behind"
                              style="width: calc({barWidth(corner.time_loss_ms)}% / 2)"
                            ></span>
                          {:else}
                            <span
                              class="bar ahead"
                              style="width: calc({barWidth(corner.time_loss_ms)}% / 2)"
                            ></span>
                          {/if}
                        </div>
                        <span class="loss-value clock">{formatDelta(corner.time_loss_ms)}</span>
                      </div>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>

          <p class="scale-note label">
            Bars share one scale: full half-width = {formatDelta(scaleMs)} s. Left of the datum is
            time gained, right is time lost.
          </p>
        </div>

        <!-- The evidence for the table above, zoomed by whatever row you click. -->
        {#if traces}
          <StatePanel state={traces} loadingLabel="the traces">
            {#snippet children(t)}
              <div class="traces">
                <div class="traces-head">
                  <span class="label">Traces</span>
                  {#if analysis.window}
                    <span class="label zoomed">
                      Turn {analysis.focusedCorner ?? DASH} · {Math.round(analysis.window.min)}–{Math.round(
                        analysis.window.max
                      )} m
                    </span>
                    <button class="btn" type="button" onclick={() => analysis.clearWindow()}>
                      Whole lap
                    </button>
                  {:else}
                    <span class="label zoomed">Whole lap</span>
                  {/if}
                </div>
                <CompareStack
                  compare={t}
                  corners={c.corners}
                  focusedCorner={analysis.focusedCorner}
                  window={analysis.window}
                  onWindow={(w) => (w ? analysis.setWindow(w.min, w.max) : analysis.clearWindow())}
                  paneHeight={130}
                />
              </div>
            {/snippet}
          </StatePanel>
        {/if}
      {/snippet}
    </StatePanel>
  {/if}
</section>

<style>
  .ref-picker {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.75rem 1rem;
  }

  .picker {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }

  .ref-note {
    flex-basis: 100%;
    margin: 0;
    color: var(--ink-2);
    font-size: 0.85rem;
  }

  .loss-col {
    width: 40%;
    min-width: 14rem;
  }

  .loss {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .bar-track {
    position: relative;
    flex: 1 1 auto;
    height: 0.85rem;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    overflow: hidden;
    min-width: 6rem;
  }

  /* The datum. Everything about this encoding hangs off it being visible. */
  .datum {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 50%;
    width: 1px;
    background: var(--line-strong);
  }

  .bar {
    position: absolute;
    top: 2px;
    bottom: 2px;
    border-radius: 2px;
  }

  .bar.behind {
    left: 50%;
    background: var(--chart-behind);
  }

  .bar.ahead {
    right: 50%;
    background: var(--chart-ahead);
  }

  .loss-value {
    min-width: 4.2rem;
    text-align: right;
    font-size: 0.8rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .gauge-cell {
    font-family: var(--font-display);
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }

  /* Faster / earlier than the reference reads green; slower / later reads red.
     Both also carry an explicit sign, so the colour is never load-bearing. */
  .up {
    color: var(--chart-ahead);
  }

  .down {
    color: var(--chart-behind);
  }

  .pick {
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
    padding: 0.1rem 0.2rem;
    border-radius: var(--radius-sm);
  }

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

  .scale-note {
    margin: 0.5rem 0 0;
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-3);
    font-weight: 500;
  }

  .traces {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .traces-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .zoomed {
    color: var(--ink-2);
    text-transform: none;
    letter-spacing: 0;
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

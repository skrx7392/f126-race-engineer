<script lang="ts">
  /**
   * Two laps, on one distance axis.
   *
   * The page is mostly plumbing around `CompareStack`: pick two laps, fetch the
   * resampled comparison, hand it over. Two decisions are worth stating.
   *
   * First, a single lap is a valid state, not a broken one. Until a reference is
   * chosen the page shows lap A's own telemetry with the channels coloured — so
   * you can read a trace immediately instead of staring at an empty frame that
   * demands two choices before it shows anything.
   *
   * Second, the B picker only offers sessions at the same track. The API answers
   * 422 for a cross-track comparison, and offering a choice that is guaranteed
   * to fail is worse than not offering it — so the constraint is enforced in the
   * picker, and the 422 is still handled in case the backend disagrees.
   */
  import {
    fetchCompare,
    fetchLaps,
    fetchSessions,
    fetchTelemetry,
    type CompareResponse,
    type Lap,
    type LapTelemetry,
    type SessionSummary
  } from '../lib/analysis-api';
  import { LOADING, analysis, type Async, type LapRef } from '../lib/analysis.svelte';
  import { formatLapTime, formatDelta, DASH } from '../lib/format';
  import CompareStack from '../components/CompareStack.svelte';
  import StatePanel from '../components/StatePanel.svelte';

  let sessions = $state<Async<SessionSummary[]>>(LOADING);
  let lapsA = $state<Lap[]>([]);
  let lapsB = $state<Lap[]>([]);
  let comparison = $state<Async<CompareResponse> | null>(null);
  let single = $state<Async<LapTelemetry> | null>(null);
  let attempt = $state(0);

  $effect(() => {
    void attempt;
    const controller = new AbortController();
    sessions = LOADING;
    fetchSessions(50, controller.signal)
      .then((data) => {
        sessions = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        sessions = { status: 'error', error };
      });
    return () => controller.abort();
  });

  let sessionList = $derived(sessions.status === 'ok' ? sessions.data : []);

  let sessionA = $derived(sessionList.find((s) => s.id === analysis.a?.sessionId) ?? null);

  /** Only same-track sessions can be compared; the API returns 422 otherwise. */
  let sessionsForB = $derived.by(() => {
    if (!sessionA) return sessionList;
    return sessionList.filter((s) => s.track_id === sessionA.track_id);
  });

  /* Lap lists for whichever sessions are currently chosen. */
  $effect(() => {
    const id = analysis.a?.sessionId;
    if (id === undefined) {
      lapsA = [];
      return;
    }
    const controller = new AbortController();
    fetchLaps(id, null, controller.signal)
      .then((data) => {
        lapsA = data;
      })
      .catch(() => {
        lapsA = [];
      });
    return () => controller.abort();
  });

  $effect(() => {
    const id = analysis.b?.sessionId;
    if (id === undefined) {
      lapsB = [];
      return;
    }
    const controller = new AbortController();
    fetchLaps(id, null, controller.signal)
      .then((data) => {
        lapsB = data;
      })
      .catch(() => {
        lapsB = [];
      });
    return () => controller.abort();
  });

  /** The comparison itself, or a single lap's trace when there is no B. */
  $effect(() => {
    void attempt;
    const a = analysis.a;
    const b = analysis.b;
    const controller = new AbortController();

    if (a && b) {
      single = null;
      comparison = LOADING;
      fetchCompare(a.sessionId, a.lap, b.sessionId, b.lap, controller.signal)
        .then((data) => {
          comparison = { status: 'ok', data };
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') return;
          comparison = { status: 'error', error };
        });
    } else if (a) {
      comparison = null;
      single = LOADING;
      fetchTelemetry(a.sessionId, a.lap, null, controller.signal)
        .then((data) => {
          single = { status: 'ok', data };
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') return;
          single = { status: 'error', error };
        });
    } else {
      comparison = null;
      single = null;
    }
    return () => controller.abort();
  });

  /** Lap options for a picker, newest sessions first, best lap flagged. */
  function lapOptions(list: Lap[], playerCar: number | null): Lap[] {
    const car = playerCar ?? list[0]?.car_index ?? null;
    return list.filter((l) => car === null || l.car_index === car);
  }

  function setSlot(slot: 'a' | 'b', ref: LapRef | null): void {
    if (slot === 'a') analysis.setA(ref);
    else analysis.setB(ref);
  }

  function onSessionChange(slot: 'a' | 'b', value: string): void {
    const id = Number(value);
    if (!Number.isSafeInteger(id)) return;
    // Changing session invalidates the lap number; start at lap 1 and let the
    // lap picker re-populate rather than keeping a lap that may not exist.
    setSlot(slot, { sessionId: id, lap: 1 });
  }

  function onLapChange(slot: 'a' | 'b', value: string): void {
    const lap = Number(value);
    const current = slot === 'a' ? analysis.a : analysis.b;
    if (!Number.isSafeInteger(lap) || !current) return;
    setSlot(slot, { sessionId: current.sessionId, lap });
  }

  let finalDelta = $derived(
    comparison?.status === 'ok' ? (comparison.data.delta_ms.at(-1) ?? null) : null
  );
</script>

<section class="analysis-page" data-page="compare">
  <header class="page-head">
    <div>
      <h1 class="page-title">Compare</h1>
      <p class="page-sub">
        Two laps on one distance axis. Drag any pane to zoom; double-click to reset.
      </p>
    </div>
    {#if comparison?.status === 'ok'}
      <div class="chip-row">
        <span class="chip-plain">
          <span class="k">A</span>
          <span class="clock">{formatLapTime(comparison.data.a.lap_time_ms)}</span>
        </span>
        <span class="chip-plain">
          <span class="k">B</span>
          <span class="clock">{formatLapTime(comparison.data.b.lap_time_ms)}</span>
        </span>
        <span class="chip-plain" data-testid="final-delta">
          <span class="k">Delta</span>
          <span class="clock">{formatDelta(finalDelta)}</span>
        </span>
      </div>
    {/if}
  </header>

  <!-- ── pickers ─────────────────────────────────────────────────────────── -->
  <div class="panel" data-panel="pickers">
    <div class="pickers">
      {#each [{ slot: 'a' as const, label: 'Lap A · subject', ref: analysis.a, laps: lapsA, list: sessionList }, { slot: 'b' as const, label: 'Lap B · reference', ref: analysis.b, laps: lapsB, list: sessionsForB }] as picker (picker.slot)}
        <div class="picker slot-{picker.slot}">
          <span class="label">{picker.label}</span>
          <div class="field">
            <select
              aria-label="Session for lap {picker.slot.toUpperCase()}"
              value={picker.ref?.sessionId ?? ''}
              onchange={(e) => onSessionChange(picker.slot, e.currentTarget.value)}
            >
              <option value="" disabled>Choose a session</option>
              {#each picker.list as s (s.id)}
                <option value={s.id}>
                  {s.track_name ?? 'Unknown'} · {s.session_type_name ?? 'Session'} · #{s.id}
                </option>
              {/each}
            </select>

            <select
              aria-label="Lap for lap {picker.slot.toUpperCase()}"
              value={picker.ref?.lap ?? ''}
              disabled={picker.ref === null}
              onchange={(e) => onLapChange(picker.slot, e.currentTarget.value)}
            >
              <option value="" disabled>Lap</option>
              {#each lapOptions(picker.laps, picker.list.find((s) => s.id === picker.ref?.sessionId)?.player_car_index ?? null) as lap (lap.lap_number)}
                <option value={lap.lap_number}>
                  L{lap.lap_number} · {formatLapTime(lap.lap_time_ms)}{lap.valid === false
                    ? ' (deleted)'
                    : ''}
                </option>
              {/each}
            </select>

            {#if picker.ref}
              <button
                class="btn"
                type="button"
                onclick={() => setSlot(picker.slot, null)}
                aria-label="Clear lap {picker.slot.toUpperCase()}">×</button
              >
            {/if}
          </div>
        </div>
      {/each}

      <button
        class="btn swap"
        type="button"
        disabled={!analysis.paired}
        onclick={() => analysis.swap()}
      >
        Swap A / B
      </button>
    </div>

    {#if sessionA && sessionsForB.length === 1 && analysis.a}
      <p class="hint label">
        Only one session recorded at {sessionA.track_name ?? 'this track'}, so lap B must come from
        the same one.
      </p>
    {/if}
  </div>

  <!-- ── the traces ──────────────────────────────────────────────────────── -->
  {#if comparison}
    <StatePanel
      state={comparison}
      loadingLabel="the comparison"
      isEmpty={(c) => c.grid_m.length === 0}
      emptyMessage="No overlapping distance data for these two laps."
    >
      {#snippet actions()}
        <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      {/snippet}
      {#snippet children(c)}
        <CompareStack
          compare={c}
          window={analysis.window}
          onWindow={(w) => (w ? analysis.setWindow(w.min, w.max) : analysis.clearWindow())}
          focusedCorner={analysis.focusedCorner}
        />
        {#if analysis.window}
          <div class="zoom-note">
            <span class="label">
              Zoomed to {Math.round(analysis.window.min)}–{Math.round(analysis.window.max)} m
            </span>
            <button class="btn" type="button" onclick={() => analysis.clearWindow()}>
              Show whole lap
            </button>
          </div>
        {/if}
      {/snippet}
    </StatePanel>
  {:else if single}
    <StatePanel state={single} loadingLabel="the lap trace">
      {#snippet actions()}
        <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
      {/snippet}
      {#snippet children(t)}
        <p class="hint label single-note">
          One lap selected — channels are coloured. Pick a reference lap to overlay the two and get
          the delta.
        </p>
        <CompareStack
          telemetry={t}
          window={analysis.window}
          onWindow={(w) => (w ? analysis.setWindow(w.min, w.max) : analysis.clearWindow())}
        />
      {/snippet}
    </StatePanel>
  {:else}
    <div class="panel idle">
      <p class="idle-title">Nothing selected</p>
      <p class="idle-text">
        Pick a lap above, or choose one from a session. {DASH} The first lap you pick is the subject;
        the second is the reference it is measured against.
      </p>
      <a class="btn" href="#/sessions">Browse sessions</a>
    </div>
  {/if}
</section>

<style>
  .pickers {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: flex-end;
  }

  .picker {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
    padding-inline-start: 0.45rem;
    border-inline-start: 3px solid var(--line);
  }

  /* The picker wears the colour its trace will be. */
  .picker.slot-a {
    border-inline-start-color: var(--chart-lap-a);
  }

  .picker.slot-b {
    border-inline-start-color: var(--chart-lap-b);
  }

  .swap {
    margin-inline-start: auto;
  }

  .hint {
    margin: 0.4rem 0 0;
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-3);
    font-weight: 500;
  }

  .single-note {
    margin: 0 0 0.4rem;
  }

  .zoom-note {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding-top: 0.4rem;
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

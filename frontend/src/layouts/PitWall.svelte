<script lang="ts">
  /**
   * The live pit wall — moved here verbatim when analysis routes arrived.
   *
   * It used to be `App.svelte` itself. Now `App.svelte` is a two-line router
   * switch and this is the `#/` branch, unchanged: same shell, same layout
   * switch, same styles. Nothing about the kiosk was renegotiated to make room
   * for a second half of the product.
   *
   * ── The shell: the persistent chrome, and the layout switch ──────────────
   * Reading top to bottom, the screen is arranged by how far from the centre of
   * vision each thing can still be understood:
   *
   *   strip   — peripheral. Shift lights, or the flag colour.
   *   header  — where, what, how long, and whether to trust the numbers.
   *   banner  — the flag, in words, only when there is one.
   *   body    — the session-specific instrument panel.
   *
   * The layout follows `slow.session.session_kind`, so switching sessions in
   * game switches the dashboard with no interaction. Practice borrows the
   * qualifying layout: both are single-lap sessions where the delta is the
   * whole story.
   */
  import { store } from '../lib/ws.svelte';
  import { flagState, safetyCarState } from '../lib/enums';
  import ShiftStrip from '../components/ShiftStrip.svelte';
  import Header from '../components/Header.svelte';
  import FlagBanner from '../components/FlagBanner.svelte';
  import Toasts from '../components/Toasts.svelte';
  import RaceLayout from './RaceLayout.svelte';
  import QualiLayout from './QualiLayout.svelte';
  import TimeTrialLayout from './TimeTrialLayout.svelte';

  let session = $derived(store.slow?.session ?? store.session);
  let kind = $derived(session?.session_kind ?? null);

  let flag = $derived(flagState(session?.fia_flag));
  let safetyCar = $derived(safetyCarState(session?.safety_car));

  let lapNumber = $derived(store.fast?.lap_number ?? store.playerRow?.lap_number ?? null);

  /** No session at all yet — say so plainly rather than showing empty gauges. */
  let waiting = $derived(session === null);
</script>

<div class="app">
  <ShiftStrip revPercent={store.fast?.rev_lights_percent ?? 0} {flag} {safetyCar} />

  <Header
    {session}
    {lapNumber}
    connection={store.connection}
    health={store.health}
    statusNote={store.statusNote}
  />

  <FlagBanner {flag} {safetyCar} />

  {#if waiting}
    <main class="waiting" data-layout="waiting">
      <p class="headline">Waiting for telemetry</p>
      <p class="hint">
        Start a session in game. The dashboard picks its layout from the session type.
      </p>
    </main>
  {:else if kind === 'race'}
    <RaceLayout fast={store.fast} slow={store.slow} />
  {:else if kind === 'time_trial'}
    <TimeTrialLayout fast={store.fast} slow={store.slow} />
  {:else if kind === 'quali' || kind === 'practice'}
    <QualiLayout fast={store.fast} slow={store.slow} />
  {:else}
    <RaceLayout fast={store.fast} slow={store.slow} />
  {/if}

  <Toasts events={store.events} />

  <!--
    The one way out of the kiosk.

    Deliberately the quietest element on the screen: this is a display that gets
    left running for an hour at a time, and a navigation control competing with
    the delta bar would be a design failure. It sits in the corner at low
    contrast and comes up to full only on hover or keyboard focus, so it is
    reachable without ever being noticeable.
  -->
  <a class="to-analysis" href="#/sessions" title="Session analysis">Analysis</a>
</div>

<style>
  .app {
    position: relative;
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    grid-template-areas:
      'strip'
      'header'
      'banner'
      'body';
    height: 100dvh;
    max-height: 100dvh;
    overflow: hidden;
    padding-bottom: env(safe-area-inset-bottom);
  }

  .waiting {
    grid-area: body;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    text-align: center;
    padding: 2rem;
  }

  .headline {
    margin: 0;
    font-size: 1.8rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--ink-2);
  }

  .hint {
    margin: 0;
    font-size: 0.95rem;
    color: var(--ink-3);
    max-width: 32rem;
  }

  .to-analysis {
    position: absolute;
    right: 0.45rem;
    bottom: calc(0.35rem + env(safe-area-inset-bottom));
    padding: 0.1rem 0.4rem;
    border-radius: var(--radius-sm);
    font-size: 0.62rem;
    font-weight: 650;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    text-decoration: none;
    opacity: 0.35;
    transition: opacity 120ms ease;
  }

  .to-analysis:hover,
  .to-analysis:focus-visible {
    opacity: 1;
    color: var(--ink);
    background: var(--surface-2);
  }
</style>

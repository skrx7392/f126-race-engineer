<script lang="ts">
  /**
   * Event toasts, top-right, auto-dismissed after 6 s.
   *
   * Two events get their own treatment because they mean something categorically
   * different from the rest:
   *
   *   FLASHBACK — the timeline itself moved. The delta reference reset, so every
   *   number on screen just changed meaning. Rendered as an inverted, full-bleed
   *   violet card so it cannot be mistaken for a sector time.
   *
   *   STALLED — the feed stopped. This is the only toast that reports on the
   *   dashboard rather than the race, so it is outlined rather than filled and
   *   pairs with the header dot going red.
   *
   * Backfilled events from a reconnect snapshot arrive with `at === 0` and are
   * skipped: a reconnect should not replay twenty stale toasts at the driver.
   */
  import { untrack } from 'svelte';
  import type { StoredEvent } from '../lib/ws.svelte';
  import { formatLapTime, formatSectorTime } from '../lib/format';

  interface Props {
    events: StoredEvent[];
  }

  let { events }: Props = $props();

  const TOAST_MS = 6_000;
  const MAX_VISIBLE = 4;

  type Tone = 'purple' | 'green' | 'yellow' | 'red' | 'amber' | 'blue' | 'flashback' | 'stalled' | 'neutral';

  interface Card {
    seq: number;
    tone: Tone;
    title: string;
    detail: string;
  }

  /** Map an event to its card, or null for events not worth interrupting for. */
  function describe(e: StoredEvent): Card | null {
    const base = { seq: e.seq };
    switch (e.code) {
      case 'SECTOR':
        return { ...base, tone: e.data.color, title: `Sector ${e.data.sector}`, detail: formatSectorTime(e.data.time_ms) };
      case 'LAP':
        return {
          ...base,
          tone: e.data.valid ? e.data.color : 'red',
          title: `Lap ${e.data.lap_number}`,
          detail: e.data.valid ? formatLapTime(e.data.time_ms) : `${formatLapTime(e.data.time_ms)} · deleted`
        };
      case 'LAP_INVALID':
        return { ...base, tone: 'red', title: 'Lap invalidated', detail: `Lap ${e.data.lap_number}` };
      case 'PIT_IN':
        return { ...base, tone: 'blue', title: 'Pit entry', detail: `Lap ${e.data.lap_number}` };
      case 'PIT_OUT':
        return { ...base, tone: 'blue', title: 'Pit exit', detail: `Lap ${e.data.lap_number}` };
      case 'PENALTY':
        return { ...base, tone: 'red', title: 'Penalty', detail: `+${e.data.time_s}s` };
      case 'FLAG':
        return e.data.flag === 'clear'
          ? { ...base, tone: 'green', title: 'Track clear', detail: 'Flag withdrawn' }
          : { ...base, tone: e.data.flag === 'red' ? 'red' : e.data.flag === 'blue' ? 'blue' : 'yellow', title: `${e.data.flag} flag`, detail: 'Sector affected' };
      case 'SC': {
        const text: Record<string, string> = {
          deployed: 'Safety car deployed',
          virtual: 'Virtual safety car',
          ending: 'Safety car ending',
          in: 'Safety car in this lap'
        };
        return { ...base, tone: 'amber', title: text[e.data.status] ?? 'Safety car', detail: 'Hold position' };
      }
      case 'FLASHBACK':
        return { ...base, tone: 'flashback', title: 'Flashback', detail: 'Delta reference reset' };
      case 'STALLED':
        return e.data.stalled
          ? { ...base, tone: 'stalled', title: 'Telemetry stalled', detail: 'No packets from the game' }
          : { ...base, tone: 'green', title: 'Telemetry restored', detail: 'Packets flowing' };
      case 'FASTEST_LAP':
        return { ...base, tone: 'purple', title: 'Fastest lap', detail: `${e.data.name} · ${formatLapTime(e.data.time_ms)}` };
      case 'CHEQUERED':
        return { ...base, tone: 'neutral', title: 'Chequered flag', detail: 'Session complete' };
      case 'SESSION_START':
        return { ...base, tone: 'neutral', title: 'Session started', detail: 'Good luck' };
      case 'SESSION_END':
        return { ...base, tone: 'neutral', title: 'Session ended', detail: '' };
      case 'DRS':
        return e.data.enabled ? { ...base, tone: 'green', title: 'DRS enabled', detail: '' } : null;
      default:
        return null;
    }
  }

  let visible = $state<Card[]>([]);
  const seen = new Set<number>();
  const timers = new Set<ReturnType<typeof setTimeout>>();

  $effect(() => {
    const now = Date.now();
    // `visible` is read untracked so appending to it does not re-trigger this
    // effect — the only dependency here is the incoming event ring.
    let next = untrack(() => visible);
    let changed = false;

    for (const e of events) {
      if (seen.has(e.seq)) continue;
      seen.add(e.seq);
      if (e.at === 0 || now - e.at > TOAST_MS) continue;

      const card = describe(e);
      if (!card) continue;

      next = [...next, card].slice(-MAX_VISIBLE);
      changed = true;

      const timer = setTimeout(() => {
        timers.delete(timer);
        visible = visible.filter((c) => c.seq !== card.seq);
      }, TOAST_MS);
      timers.add(timer);
    }

    if (changed) visible = next;
  });

  $effect(() => {
    return () => {
      for (const t of timers) clearTimeout(t);
      timers.clear();
    };
  });
</script>

<div class="toasts" aria-live="polite">
  {#each visible as card (card.seq)}
    <article class="toast {card.tone}">
      <span class="title">{card.title}</span>
      {#if card.detail}<span class="detail clock">{card.detail}</span>{/if}
    </article>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    top: 3.4rem;
    right: 0.7rem;
    z-index: 40;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    align-items: flex-end;
    pointer-events: none;
  }

  .toast {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    min-width: 9.5rem;
    padding: 0.32rem 0.6rem;
    border-radius: var(--radius);
    background: var(--surface-2);
    border-inline-start: 3px solid var(--line-strong);
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.55);
    animation: toast-in 0.16s ease-out;
  }

  .title {
    font-size: 0.9rem;
    font-weight: 700;
    text-transform: capitalize;
    white-space: nowrap;
  }

  .detail {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--ink-2);
    margin-inline-start: auto;
    white-space: nowrap;
  }

  .toast.purple {
    border-inline-start-color: var(--purple);
  }
  .toast.purple .title {
    color: var(--purple);
  }

  .toast.green {
    border-inline-start-color: var(--green);
  }
  .toast.green .title {
    color: var(--green);
  }

  .toast.yellow {
    border-inline-start-color: var(--yellow);
  }
  .toast.yellow .title {
    color: var(--yellow);
  }

  .toast.red {
    border-inline-start-color: var(--red);
  }
  .toast.red .title {
    color: var(--red);
  }

  .toast.amber {
    border-inline-start-color: var(--amber);
  }
  .toast.amber .title {
    color: var(--amber);
  }

  .toast.blue {
    border-inline-start-color: var(--blue);
  }
  .toast.blue .title {
    color: var(--blue);
  }

  .toast.neutral .title {
    color: var(--ink);
  }

  /* The timeline moved: inverted, so it cannot be read as a lap result. */
  .toast.flashback {
    background: var(--purple);
    border-inline-start-color: #ffffff;
    box-shadow: 0 4px 24px color-mix(in srgb, var(--purple) 45%, transparent);
  }
  .toast.flashback .title,
  .toast.flashback .detail {
    color: #ffffff;
  }

  /* About the dashboard, not the race: outlined rather than filled. */
  .toast.stalled {
    background: transparent;
    border: 1px dashed var(--red);
    border-inline-start: 3px solid var(--red);
    animation:
      toast-in 0.16s ease-out,
      flag-pulse 1.4s ease-in-out infinite;
  }
  .toast.stalled .title {
    color: var(--red);
  }
</style>

<script lang="ts">
  /**
   * Always-on context bar: where we are, what session, how much is left, and
   * whether the numbers below can be trusted.
   */
  import type { SessionInfo } from '../lib/protocol';
  import type { ConnectionState, HealthLevel } from '../lib/ws.svelte';
  import { formatClock, sessionKindLabel, DASH } from '../lib/format';

  interface Props {
    session: SessionInfo | null;
    lapNumber: number | null;
    connection: ConnectionState;
    health: HealthLevel;
    statusNote: string | null;
  }

  let { session, lapNumber, connection, health, statusNote }: Props = $props();

  /**
   * Socket vocabulary is not driver vocabulary. "Live" is what the green dot
   * actually means; "open" is an implementation detail leaking onto a windscreen.
   */
  const CONNECTION_WORD: Record<ConnectionState, string> = {
    connecting: 'Connecting',
    open: 'Live',
    stalled: 'Stalled',
    closed: 'Offline'
  };

  let trackName = $derived(session?.track_name ?? 'No session');
  let kindLabel = $derived(sessionKindLabel(session?.session_kind));

  /** Races count laps; everything else counts down a clock. */
  let isRace = $derived(session?.session_kind === 'race');
  let lapText = $derived(
    lapNumber != null && session?.total_laps ? `${lapNumber} / ${session.total_laps}` : DASH
  );
</script>

<header>
  <div class="place">
    <span class="track">{trackName}</span>
    <span class="kind label">{kindLabel}</span>
  </div>

  <div class="remaining">
    {#if isRace}
      <span class="label">Lap</span>
      <span class="value gauge">{lapText}</span>
    {:else}
      <span class="label">Remaining</span>
      <span class="value clock">{formatClock(session?.time_left_s)}</span>
    {/if}
  </div>

  <div class="health" title={statusNote ?? `Connection ${connection}`}>
    <span class="dot {health}"></span>
    <span class="label state">{statusNote ?? CONNECTION_WORD[connection]}</span>
  </div>
</header>

<style>
  header {
    grid-area: header;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.45rem 0.75rem;
    background: var(--surface-1);
    border-bottom: 1px solid var(--line);
    min-width: 0;
  }

  .place {
    display: flex;
    align-items: baseline;
    gap: 0.7rem;
    min-width: 0;
  }

  .track {
    font-size: 1.5rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    /* SF exposes a width axis; condensing buys room for long circuit names. */
    font-stretch: 92%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .kind {
    color: var(--ink-2);
  }

  .remaining {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin-inline-start: auto;
  }

  .remaining .value {
    font-size: 1.55rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }

  .health {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
  }

  .state {
    max-width: 14rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .dot {
    width: 0.62rem;
    height: 0.62rem;
    min-width: 7px;
    min-height: 7px;
    border-radius: 50%;
    flex: none;
  }

  .dot.good {
    background: var(--green);
    box-shadow: 0 0 7px color-mix(in srgb, var(--green) 60%, transparent);
  }
  .dot.warn {
    background: var(--amber);
    box-shadow: 0 0 7px color-mix(in srgb, var(--amber) 60%, transparent);
  }
  .dot.bad {
    background: var(--red);
    box-shadow: 0 0 8px color-mix(in srgb, var(--red) 70%, transparent);
    animation: flag-pulse 1.1s ease-in-out infinite;
  }
</style>

<script lang="ts">
  /**
   * Rolling three-lap pace against the cars directly ahead and behind.
   *
   * The absolute lap times matter less than the sign of the difference, so each
   * neighbour shows its delta to the player, coloured from the player's point
   * of view: green means you are quicker than them.
   */
  import type { PaceInfo } from '../lib/protocol';
  import { formatLapTime, formatDelta, DASH } from '../lib/format';

  interface Props {
    pace: PaceInfo | null;
  }

  let { pace }: Props = $props();

  let mine = $derived(pace?.last_3_avg_ms ?? null);

  function diff(other: number | null | undefined): number | null {
    if (mine == null || other == null) return null;
    // Positive means the neighbour is slower than you.
    return other - mine;
  }

  let aheadDiff = $derived(diff(pace?.ahead_last_3_avg_ms));
  let behindDiff = $derived(diff(pace?.behind_last_3_avg_ms));

  function tone(d: number | null): string {
    if (d === null) return 'none';
    return d > 0 ? 'good' : d < 0 ? 'bad' : 'none';
  }
</script>

<section class="pace panel" data-panel="pace">
  <div class="panel-head">
    <span class="label">Pace</span>
    <span class="label">last 3 laps</span>
  </div>

  <div class="rows">
    <div class="line">
      <span class="who label">Ahead</span>
      <span class="time clock">{formatLapTime(pace?.ahead_last_3_avg_ms)}</span>
      <span class="d clock {tone(aheadDiff)}">{aheadDiff === null ? DASH : formatDelta(aheadDiff, 2)}</span>
    </div>

    <div class="line you">
      <span class="who label">You</span>
      <span class="time clock">{formatLapTime(mine)}</span>
      <span class="d clock"></span>
    </div>

    <div class="line">
      <span class="who label">Behind</span>
      <span class="time clock">{formatLapTime(pace?.behind_last_3_avg_ms)}</span>
      <span class="d clock {tone(behindDiff)}"
        >{behindDiff === null ? DASH : formatDelta(behindDiff, 2)}</span
      >
    </div>
  </div>
</section>

<style>
  .pace {
    grid-area: pace;
  }

  .rows {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.18rem;
    flex: 1;
    min-height: 0;
  }

  .line {
    display: grid;
    grid-template-columns: 3.4rem 1fr auto;
    align-items: baseline;
    gap: 0.4rem;
    padding: 0.1rem 0.25rem;
    border-radius: var(--radius-sm);
  }

  .line.you {
    background: var(--surface-3);
    box-shadow: inset 2px 0 0 var(--player);
  }

  .who {
    color: var(--ink-3);
  }

  .line.you .who {
    color: var(--player);
  }

  .time {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--ink-2);
  }

  .line.you .time {
    color: var(--ink);
  }

  .d {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--ink-3);
  }

  .d.good {
    color: var(--green);
  }
  .d.bad {
    color: var(--red);
  }
  .d.none {
    color: var(--ink-3);
  }
</style>

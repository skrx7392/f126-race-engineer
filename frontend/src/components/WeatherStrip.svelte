<script lang="ts">
  /**
   * Conditions now, and what is coming.
   *
   * Rain probability is the only number here that changes a decision, so the
   * forecast chips are ordered by time and the percentage is the largest glyph
   * in each. A chip turns blue once its sample is actually wet, not merely
   * threatening.
   */
  import type { SessionInfo } from '../lib/protocol';
  import { weatherOf, isWet } from '../lib/enums';
  import { formatNumber } from '../lib/format';

  interface Props {
    session: SessionInfo | null;
  }

  let { session }: Props = $props();

  let now = $derived(weatherOf(session?.weather));
  let forecast = $derived((session?.forecast ?? []).slice(0, 4));
</script>

<section class="weather panel" data-panel="weather">
  <div class="panel-head">
    <span class="label">Conditions</span>
    <span class="temps label">
      Track {formatNumber(session?.track_temp_c)}° · Air {formatNumber(session?.air_temp_c)}°
    </span>
  </div>

  <div class="body">
    <div class="now" class:wet={isWet(session?.weather)}>
      <span class="glyph">{now.glyph}</span>
      <span class="what">{now.label}</span>
    </div>

    <div class="forecast">
      <!-- Keyed by index: offset_min is raw game data and the forecast can carry two
           samples at the same offset (one per session type), which is a duplicate key. -->
      {#each forecast as sample, i (i)}
        {@const w = weatherOf(sample.weather)}
        <div class="chip" class:wet={isWet(sample.weather)}>
          <span class="when label">+{sample.offset_min}m</span>
          <span class="rain">{sample.rain_pct}<span class="pct">%</span></span>
          <span class="icon">{w.glyph}</span>
        </div>
      {/each}
    </div>
  </div>
</section>

<style>
  .weather {
    grid-area: weather;
  }

  .body {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    flex: 1;
    min-height: 0;
    min-width: 0;
  }

  .now {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex: none;
    padding-inline-end: 0.55rem;
    border-inline-end: 1px solid var(--line);
    align-self: stretch;
  }

  .glyph {
    font-size: 1.5rem;
    color: var(--ink-2);
    line-height: 1;
  }

  .what {
    font-size: 0.85rem;
    font-weight: 650;
    color: var(--ink-2);
    white-space: nowrap;
  }

  .now.wet .glyph,
  .now.wet .what {
    color: var(--blue);
  }

  .forecast {
    display: flex;
    gap: 0.3rem;
    flex: 1;
    min-width: 0;
    overflow: hidden;
  }

  .chip {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.02rem;
    padding: 0.2rem 0.35rem;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    flex: 1;
    min-width: 0;
  }

  .chip.wet {
    background: var(--blue-dim);
  }

  .when {
    color: var(--ink-3);
    font-size: 0.58rem;
  }

  .rain {
    font-size: 1rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    color: var(--ink-2);
    white-space: nowrap;
  }

  .chip.wet .rain {
    color: var(--blue);
  }

  .pct {
    font-size: 0.62em;
    color: var(--ink-3);
  }

  .icon {
    font-size: 0.75rem;
    color: var(--ink-3);
    line-height: 1;
  }

  .temps {
    color: var(--ink-2);
  }
</style>

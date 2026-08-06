<script lang="ts">
  /**
   * Energy store and deployment mode. The bar is a magnitude, so it uses a
   * single hue at varying fill rather than a colour that changes with level —
   * a bar that turns red at 20% encodes the same number twice and steals a
   * semantic colour to do it.
   */
  import type { EnergyInfo } from '../lib/protocol';
  import { tweenVar } from '../lib/tween';
  import { deployModeLabel } from '../lib/enums';
  import { formatNumber, clamp01 } from '../lib/format';

  interface Props {
    energy: EnergyInfo | null;
  }

  let { energy }: Props = $props();

  let pct = $derived(clamp01((energy?.store_pct ?? 0) / 100));
</script>

<section class="energy panel" data-panel="energy">
  <div class="panel-head">
    <span class="label">Energy</span>
    <span class="mode">{deployModeLabel(energy?.deploy_mode)}</span>
  </div>

  <div class="value gauge">{formatNumber(energy?.store_pct, 0)}<span class="unit">%</span></div>

  <div class="track" use:tweenVar={{ name: '--v', value: pct }}>
    <span class="fill"></span>
  </div>
</section>

<style>
  .energy {
    grid-area: energy;
    justify-content: center;
  }

  .value {
    font-size: 1.7rem;
  }

  .unit {
    font-size: 0.55em;
    font-weight: 600;
    color: var(--ink-3);
    margin-inline-start: 0.1em;
  }

  .track {
    position: relative;
    height: 0.45rem;
    min-height: 5px;
    background: var(--surface-2);
    border-radius: 3px;
    overflow: hidden;
  }

  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    width: calc(var(--v, 0) * 100%);
    background: var(--blue);
    border-radius: 3px;
  }

  .mode {
    font-size: 0.78rem;
    font-weight: 700;
    color: var(--ink-2);
    white-space: nowrap;
  }
</style>

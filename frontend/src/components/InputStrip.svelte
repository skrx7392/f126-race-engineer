<script lang="ts">
  /**
   * Driver inputs: throttle and brake as vertical bars, plus the gear, speed
   * and rev readout.
   *
   * Throttle green / brake red is the universal convention in every telemetry
   * tool a sim racer has ever used, and the two bars are physically separated
   * and permanently labelled, so the colour is never load-bearing.
   *
   * Bars tween; the numbers do not.
   */
  import { tweenVar } from '../lib/tween';
  import { formatGear, formatNumber, clamp01 } from '../lib/format';
  import type { FastFrame } from '../lib/protocol';

  interface Props {
    fast: FastFrame | null;
  }

  let { fast }: Props = $props();

  let throttle = $derived(clamp01(fast?.throttle ?? 0));
  let brake = $derived(clamp01(fast?.brake ?? 0));
  let revs = $derived(clamp01((fast?.rev_lights_percent ?? 0) / 100));

  /** Steering is already -1..1; clamp defensively and keep the sign. */
  let steer = $derived(Math.max(-1, Math.min(1, fast?.steer ?? 0)));

  const REV_SEGMENTS = 10;
  const segments = Array.from({ length: REV_SEGMENTS }, (_, i) => i / REV_SEGMENTS);
</script>

<section class="inputs panel" data-panel="inputs">
  <div class="panel-head">
    <span class="label">Inputs</span>
    {#if fast?.drs_open}
      <span class="drs">DRS</span>
    {/if}
  </div>

  <div class="body">
    <div class="pedals">
      <div class="pedal">
        <span class="label">Thr</span>
        <span class="pedal-val gauge thr-ink">{Math.round(throttle * 100)}</span>
        <div class="bar-track" use:tweenVar={{ name: '--v', value: throttle }}>
          <span class="bar thr"></span>
        </div>
      </div>
      <div class="pedal">
        <span class="label">Brk</span>
        <span class="pedal-val gauge brk-ink">{Math.round(brake * 100)}</span>
        <div class="bar-track" use:tweenVar={{ name: '--v', value: brake }}>
          <span class="bar brk"></span>
        </div>
      </div>
    </div>

    <div class="readouts">
      <div class="top-row">
        <div class="gear-block">
          <span class="gear gauge">{formatGear(fast?.gear)}</span>
          <span class="label">Gear</span>
        </div>

        <div class="speed-block">
          <span class="speed gauge"
            >{formatNumber(fast?.speed_kmh)}<span class="unit">km/h</span></span
          >

          <div class="revs" use:tweenVar={{ name: '--lit', value: revs }}>
            {#each segments as seg (seg)}
              <span class="seg" class:top={seg >= 0.8} style="--i: {seg}"></span>
            {/each}
          </div>
          <span class="rpm label">{formatNumber(fast?.rpm)} rpm</span>
        </div>
      </div>

      <!-- Steering is an input too; the panel was incomplete without it. -->
      <div class="steer-block">
        <span class="label">Steer</span>
        <div class="steer-track" use:tweenVar={{ name: '--s', value: steer }}>
          <span class="steer-datum"></span>
          <span class="steer-needle"></span>
        </div>
      </div>
    </div>
  </div>
</section>

<style>
  .inputs {
    grid-area: inputs;
  }

  .body {
    display: flex;
    gap: 0.6rem;
    flex: 1;
    min-height: 0;
  }

  .pedals {
    display: flex;
    gap: 0.5rem;
    flex: none;
  }

  .pedal {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.15rem;
    min-height: 0;
  }

  .pedal-val {
    font-size: 1.1rem;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }

  .thr-ink {
    color: var(--green);
  }
  .brk-ink {
    color: var(--red);
  }

  .bar-track {
    position: relative;
    width: 1.5rem;
    min-width: 14px;
    flex: 1;
    min-height: 0;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  /* Bars grow from the baseline with a rounded data-end. */
  .bar {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: calc(var(--v, 0) * 100%);
    border-radius: 3px 3px 0 0;
  }

  .bar.thr {
    background: var(--green);
  }
  .bar.brk {
    background: var(--red);
  }

  .readouts {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.5rem;
    flex: 1;
    min-width: 0;
  }

  .top-row {
    display: flex;
    gap: 0.6rem;
    min-width: 0;
    align-items: center;
  }

  .steer-block {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    min-width: 0;
  }

  .steer-track {
    position: relative;
    flex: 1;
    height: 0.4rem;
    min-height: 4px;
    min-width: 0;
    background: var(--surface-2);
    border-radius: 2px;
  }

  .steer-datum {
    position: absolute;
    top: -2px;
    bottom: -2px;
    left: 50%;
    width: 1px;
    background: var(--line-strong);
  }

  .steer-needle {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 3px;
    margin-left: -1.5px;
    left: calc(50% + var(--s, 0) * 50%);
    background: var(--ink-2);
    border-radius: 2px;
  }

  .gear-block {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: none;
  }

  .gear {
    font-size: 3.4rem;
    line-height: 0.9;
    font-weight: 800;
  }

  .speed-block {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.25rem;
    flex: 1;
    min-width: 0;
  }

  .speed {
    font-size: 2rem;
    white-space: nowrap;
  }

  .unit {
    font-size: 0.5em;
    font-weight: 600;
    color: var(--ink-3);
    margin-inline-start: 0.2em;
    letter-spacing: 0;
  }

  /*
   * A local rev readout. Deliberately quieter than the full-bleed strip at the
   * top of the screen: flat segments, no glow. That one is peripheral vision;
   * this one belongs to the number beside it.
   */
  .revs {
    display: flex;
    gap: 2px;
    height: 0.4rem;
    min-height: 4px;
  }

  .seg {
    flex: 1;
    border-radius: 1px;
    background: var(--ink-2);
    opacity: clamp(0.12, calc((var(--lit, 0) - var(--i)) * 60), 1);
  }

  .seg.top {
    background: var(--red);
  }

  .rpm {
    color: var(--ink-3);
  }

  .drs {
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    padding: 0.1rem 0.3rem;
    border-radius: var(--radius-sm);
    background: var(--green-dim);
    color: var(--green);
  }
</style>

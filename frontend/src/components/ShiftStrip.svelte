<script lang="ts">
  /**
   * The top edge of the screen, doing the job a steering wheel's LED bar does.
   *
   * This is the one place the design spends its boldness. The user of this
   * dashboard is *driving* — an iPad propped beside the TV — so the most
   * valuable thing the screen can offer is something readable without looking
   * at it. A full-bleed light bar in peripheral vision costs zero glances.
   *
   * It is also the interface's single alarm channel. When a flag or safety car
   * is out, the strip stops being a tachometer and becomes the flag: under a
   * yellow you lift, so engine revs have nothing left to tell you. Priority is
   * red > safety car > yellow > revs.
   *
   * Rendering note: the lit fraction arrives as one tweened CSS variable and
   * each LED derives its own opacity from it in pure CSS. That is one style
   * write per frame for the whole bar, rather than 15 reactive updates.
   */
  import { tweenVar } from '../lib/tween';
  import type { FlagState, SafetyCarState } from '../lib/enums';
  import { clamp01 } from '../lib/format';

  interface Props {
    revPercent: number;
    flag: FlagState;
    safetyCar: SafetyCarState;
  }

  let { revPercent, flag, safetyCar }: Props = $props();

  const LED_COUNT = 15;
  const leds = Array.from({ length: LED_COUNT }, (_, i) => ({
    // Threshold at which this LED starts to light.
    threshold: i / LED_COUNT,
    // Classic shift-light ramp: green, then red, then violet for "shift now".
    tone: i < 5 ? 'green' : i < 10 ? 'red' : 'shift'
  }));

  let lit = $derived(clamp01(revPercent / 100));

  type Alarm = 'red' | 'sc' | 'yellow' | null;
  let alarm: Alarm = $derived(
    flag === 'red'
      ? 'red'
      : safetyCar === 'full' || safetyCar === 'virtual'
        ? 'sc'
        : flag === 'yellow'
          ? 'yellow'
          : null
  );
</script>

{#if alarm}
  <div class="strip alarm {alarm}" role="presentation"></div>
{:else}
  <div class="strip" use:tweenVar={{ name: '--lit', value: lit }} role="presentation">
    {#each leds as led (led.threshold)}
      <span class="led {led.tone}" style="--i: {led.threshold}"></span>
    {/each}
  </div>
{/if}

<style>
  .strip {
    grid-area: strip;
    display: flex;
    gap: 2px;
    height: 0.72rem;
    min-height: 7px;
    padding: 0 2px;
    background: #050608;
    align-items: stretch;
  }

  .led {
    flex: 1;
    border-radius: 1px;
    /*
     * Each LED compares the tweened lit fraction against its own threshold.
     * The ×60 makes the edge crisp while leaving one LED mid-fade, which is
     * what reads as a real light bar rather than a progress meter.
     */
    opacity: clamp(0, calc((var(--lit, 0) - var(--i)) * 60), 1);
  }

  .led.green {
    background: var(--green);
    box-shadow: 0 0 6px color-mix(in srgb, var(--green) 55%, transparent);
  }
  .led.red {
    background: var(--red);
    box-shadow: 0 0 6px color-mix(in srgb, var(--red) 55%, transparent);
  }
  .led.shift {
    background: var(--purple);
    box-shadow: 0 0 8px color-mix(in srgb, var(--purple) 70%, transparent);
  }

  /* Flag mode: the whole bar becomes the flag, with moving chevrons. */
  .alarm {
    background-image: repeating-linear-gradient(
      115deg,
      var(--flag-c) 0 1rem,
      color-mix(in srgb, var(--flag-c) 45%, #000) 1rem 2rem
    );
    background-size: 3rem 100%;
    animation: flag-sweep 0.55s linear infinite;
  }

  .alarm.yellow {
    --flag-c: var(--yellow);
  }
  .alarm.sc {
    --flag-c: var(--amber);
  }
  .alarm.red {
    --flag-c: var(--red);
    animation:
      flag-sweep 0.4s linear infinite,
      flag-pulse 0.9s ease-in-out infinite;
  }
</style>

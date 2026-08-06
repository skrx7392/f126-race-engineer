<script lang="ts">
  /**
   * Qualifying and practice.
   *
   * One lap is the entire session, so the delta bar takes the full width of the
   * screen at a ±2 s scale and everything else arranges beneath it. The tower
   * is still present but ranks on best lap rather than track position, so its
   * interval column changes meaning and says so.
   *
   * The invalid-lap banner overlays the delta rather than displacing it: the
   * lap is dead, but you still want to see what it was doing.
   */
  import type { FastFrame, SlowFrame } from '../lib/protocol';
  import DeltaBar from '../components/DeltaBar.svelte';
  import SectorBoard from '../components/SectorBoard.svelte';
  import TyreCluster from '../components/TyreCluster.svelte';
  import InputStrip from '../components/InputStrip.svelte';
  import TimingTower from '../components/TimingTower.svelte';
  import WeatherStrip from '../components/WeatherStrip.svelte';
  import ValidityBanner from '../components/ValidityBanner.svelte';

  interface Props {
    fast: FastFrame | null;
    slow: SlowFrame | null;
  }

  let { fast, slow }: Props = $props();

  /**
   * The banner shows while the lap in progress is dead. `last_lap_valid` is the
   * protocol's flag for it; a LAP_INVALID event sets it and the next clean lap
   * clears it.
   */
  let invalid = $derived(slow?.sectors?.last_lap_valid === false);
</script>

<main class="quali" data-layout="quali">
  <DeltaBar
    deltaMs={fast?.delta_best_ms ?? null}
    deltaKind={fast?.delta_kind ?? null}
    scale={2}
    size="dominant"
  />
  <ValidityBanner {invalid} />

  <SectorBoard sectors={slow?.sectors ?? null} />
  <TyreCluster tyres={slow?.tyres ?? null} emphasis="temp" />
  <TimingTower tower={slow?.tower ?? []} intervalLabel="Gap" />
  <InputStrip {fast} />
  <WeatherStrip session={slow?.session ?? null} />
</main>

<style>
  .quali {
    grid-area: body;
    display: grid;
    grid-template-columns: minmax(0, 34fr) minmax(0, 30fr) minmax(0, 36fr);
    grid-template-rows: auto minmax(0, 1fr) minmax(0, 1fr) auto;
    grid-template-areas:
      'delta delta delta'
      'sectors tyres tower'
      'inputs tyres tower'
      'weather weather tower';
    gap: var(--gap);
    padding: var(--gap);
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  @media (max-height: 460px) {
    .quali {
      grid-template-columns: minmax(0, 40fr) minmax(0, 30fr) minmax(0, 30fr);
      grid-template-rows: auto minmax(0, 1fr);
      grid-template-areas:
        'delta delta delta'
        'sectors tyres tower';
    }

    .quali :global([data-panel='inputs']),
    .quali :global([data-panel='weather']) {
      display: none;
    }
  }
</style>

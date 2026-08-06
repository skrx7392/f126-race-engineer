<script lang="ts">
  /**
   * Race.
   *
   * The tower owns the left half because in a race, position is the result and
   * everything else is the means. The right column is ordered strictly by
   * glance value from the top: delta, then what you are doing with the car
   * (inputs) and what the car is doing to its tyres, then the two resources
   * that decide strategy (fuel, energy), then the slower context — pace,
   * damage, weather.
   */
  import type { FastFrame, SlowFrame } from '../lib/protocol';
  import DeltaBar from '../components/DeltaBar.svelte';
  import TimingTower from '../components/TimingTower.svelte';
  import InputStrip from '../components/InputStrip.svelte';
  import TyreCluster from '../components/TyreCluster.svelte';
  import FuelPanel from '../components/FuelPanel.svelte';
  import EnergyBar from '../components/EnergyBar.svelte';
  import PaceTrio from '../components/PaceTrio.svelte';
  import DamagePanel from '../components/DamagePanel.svelte';
  import WeatherStrip from '../components/WeatherStrip.svelte';

  interface Props {
    fast: FastFrame | null;
    slow: SlowFrame | null;
  }

  let { fast, slow }: Props = $props();
</script>

<main class="race" data-layout="race">
  <TimingTower tower={slow?.tower ?? []} />

  <DeltaBar deltaMs={fast?.delta_best_ms ?? null} deltaKind={fast?.delta_kind ?? null} scale={1} />

  <InputStrip {fast} />
  <TyreCluster tyres={slow?.tyres ?? null} emphasis="wear" />
  <FuelPanel fuel={slow?.fuel ?? null} />
  <EnergyBar energy={slow?.energy ?? null} />
  <PaceTrio pace={slow?.pace ?? null} />
  <DamagePanel damage={slow?.damage ?? null} />
  <WeatherStrip session={slow?.session ?? null} />
</main>

<style>
  .race {
    grid-area: body;
    display: grid;
    grid-template-columns: minmax(0, 43fr) minmax(0, 30fr) minmax(0, 27fr);
    grid-template-rows: auto minmax(0, 1.15fr) minmax(0, 1.15fr) minmax(0, 0.8fr) minmax(0, 1fr) auto;
    grid-template-areas:
      'tower delta delta'
      'tower inputs tyres'
      'tower inputs tyres'
      'tower fuel energy'
      'tower pace damage'
      'tower weather weather';
    gap: var(--gap);
    padding: var(--gap);
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  /*
   * Phone landscape: roughly 390px of height. Everything still fits without
   * scrolling only if the lowest-value panels give up their rows — damage and
   * the forecast are the two a driver can afford to lose first.
   */
  @media (max-height: 460px) {
    .race {
      grid-template-rows: auto minmax(0, 1.2fr) minmax(0, 1.2fr) minmax(0, 0.9fr);
      grid-template-areas:
        'tower delta delta'
        'tower inputs tyres'
        'tower inputs tyres'
        'tower fuel energy';
    }

    .race :global([data-panel='pace']),
    .race :global([data-panel='damage']),
    .race :global([data-panel='weather']) {
      display: none;
    }
  }
</style>

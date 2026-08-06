<script lang="ts">
  /**
   * Fuel. The number that decides a race is `delta_laps` — laps in hand against
   * laps remaining — so it gets the semantic colour and the others stay grey.
   */
  import type { FuelInfo } from '../lib/protocol';
  import { formatNumber, formatSigned, DASH } from '../lib/format';
  import Stat from './Stat.svelte';

  interface Props {
    fuel: FuelInfo | null;
  }

  let { fuel }: Props = $props();

  let delta = $derived(fuel?.delta_laps ?? null);

  /** Under a tenth of a lap in hand is a real problem; under half is a warning. */
  let deltaTone = $derived(
    delta === null ? 'muted' : delta < 0 ? 'red' : delta < 0.5 ? 'amber' : 'green'
  );
</script>

<section class="fuel panel" data-panel="fuel">
  <div class="panel-head">
    <span class="label">Fuel</span>
    <span class="label">{formatNumber(fuel?.burn_last_lap_kg, 2)} kg/lap</span>
  </div>

  <div class="row">
    <Stat label="In tank" value={formatNumber(fuel?.in_tank_kg, 1)} unit="kg" size="md" />
    <Stat
      label="Laps in hand"
      value={formatSigned(delta, 1)}
      tone={deltaTone as 'green' | 'red' | 'amber' | 'muted'}
      size="md"
    />
  </div>

  <div class="foot label">
    Fuel for {formatNumber(fuel?.remaining_laps, 1)} laps · {fuel?.laps_left_in_session ?? DASH} to run
  </div>
</section>

<style>
  .fuel {
    grid-area: fuel;
  }

  .row {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    flex: 1;
    align-items: center;
    min-width: 0;
  }

  .foot {
    color: var(--ink-3);
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>

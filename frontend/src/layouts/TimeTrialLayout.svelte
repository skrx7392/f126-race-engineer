<script lang="ts">
  /**
   * Time trial.
   *
   * Two references, two bars, side by side: your own PB on the left, the rival
   * ghost on the right. Reading them as a pair is the whole activity — you are
   * simultaneously trying to beat yourself and someone else, and those two
   * answers routinely disagree.
   *
   * The protocol carries only one live delta, measured against the PB. The
   * rival bar is derived by offsetting it with the constant difference between
   * the two reference laps, and is labelled "derived" so it is never mistaken
   * for a measured value.
   */
  import type { FastFrame, SlowFrame } from '../lib/protocol';
  import { formatLapTime } from '../lib/format';
  import DeltaBar from '../components/DeltaBar.svelte';
  import TimeTrialBoard from '../components/TimeTrialBoard.svelte';
  import InputStrip from '../components/InputStrip.svelte';
  import SectorBoard from '../components/SectorBoard.svelte';
  import ValidityBanner from '../components/ValidityBanner.svelte';

  interface Props {
    fast: FastFrame | null;
    slow: SlowFrame | null;
  }

  let { fast, slow }: Props = $props();

  let pbDelta = $derived(fast?.delta_best_ms ?? null);

  let rivalDelta = $derived.by(() => {
    const tt = slow?.timetrial;
    if (pbDelta === null || tt?.pb_ms == null || tt?.rival_ms == null) return null;
    // If you are level with your PB, you are (pb - rival) off the rival.
    return pbDelta + (tt.pb_ms - tt.rival_ms);
  });

  let invalid = $derived(slow?.sectors?.last_lap_valid === false);
</script>

<main class="tt" data-layout="time_trial">
  <DeltaBar
    deltaMs={pbDelta}
    deltaKind={fast?.delta_kind ?? null}
    scale={2}
    size="dominant"
    label="vs personal best"
    refLabel={formatLapTime(slow?.timetrial?.pb_ms)}
    area="delta"
  />
  <DeltaBar
    deltaMs={rivalDelta}
    deltaKind={null}
    scale={2}
    size="dominant"
    label="vs rival (derived)"
    refLabel={formatLapTime(slow?.timetrial?.rival_ms)}
    area="rival"
  />
  <ValidityBanner {invalid} />

  <TimeTrialBoard timetrial={slow?.timetrial ?? null} />
  <SectorBoard sectors={slow?.sectors ?? null} />
  <InputStrip {fast} />
</main>

<style>
  /*
   * Three things, and only three: the two references, and what you are doing
   * with the car. A time trial has no field, no fuel and no strategy — adding
   * panels here only stretches them into empty space.
   */
  .tt {
    grid-area: body;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr) minmax(0, 0.85fr);
    grid-template-areas:
      'delta rival'
      'board sectors'
      'inputs inputs';
    gap: var(--gap);
    padding: var(--gap);
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  @media (max-height: 460px) {
    .tt {
      grid-template-rows: auto minmax(0, 1fr);
      grid-template-areas:
        'delta rival'
        'board sectors';
    }

    .tt :global([data-panel='inputs']) {
      display: none;
    }
  }
</style>

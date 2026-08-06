<script lang="ts">
  /**
   * Time-trial reference board: your personal best and the rival's ghost,
   * sector by sector.
   *
   * The protocol carries one live delta (`fast.delta_best_ms`, against your PB
   * in this mode). The rival delta is derived from it by the constant offset
   * between the two reference laps, which is exact at the line and a good
   * approximation everywhere else. It is labelled as derived so nobody reads it
   * as a measured value.
   */
  import type { TimeTrialInfo } from '../lib/protocol';
  import { formatLapTime, formatSectorTime, formatDelta, DASH } from '../lib/format';

  interface Props {
    timetrial: TimeTrialInfo | null;
  }

  let { timetrial }: Props = $props();

  let gapToRival = $derived(
    timetrial?.pb_ms != null && timetrial?.rival_ms != null
      ? timetrial.pb_ms - timetrial.rival_ms
      : null
  );

  let rows = $derived(
    [0, 1, 2].map((i) => ({
      n: i + 1,
      pb: timetrial?.pb_sectors?.[i] ?? null,
      rival: timetrial?.rival_sectors?.[i] ?? null
    }))
  );
</script>

<section class="board panel" data-panel="board">
  <div class="panel-head">
    <span class="label">Reference laps</span>
    <span class="label">you vs rival</span>
  </div>

  <div class="totals">
    <div class="total">
      <span class="label">Personal best</span>
      <span class="t clock">{formatLapTime(timetrial?.pb_ms)}</span>
    </div>
    <div class="total">
      <span class="label">Rival</span>
      <span class="t clock rival-ink">{formatLapTime(timetrial?.rival_ms)}</span>
    </div>
    <div class="total">
      <span class="label">Gap</span>
      <span class="t clock" class:ahead={gapToRival !== null && gapToRival < 0}>
        {gapToRival === null ? DASH : formatDelta(gapToRival, 3)}
      </span>
    </div>
  </div>

  <div class="sector-rows">
    {#each rows as row (row.n)}
      {@const d = row.pb !== null && row.rival !== null ? row.pb - row.rival : null}
      <div class="srow">
        <span class="s label">S{row.n}</span>
        <span class="v clock">{formatSectorTime(row.pb)}</span>
        <span class="v clock rival-ink">{formatSectorTime(row.rival)}</span>
        <span class="v clock d" class:good={d !== null && d < 0} class:bad={d !== null && d > 0}>
          {d === null ? DASH : formatDelta(d, 3)}
        </span>
      </div>
    {/each}
  </div>
</section>

<style>
  .board {
    grid-area: board;
  }

  .totals {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.4rem;
  }

  .total {
    display: flex;
    flex-direction: column;
    gap: 0.05rem;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    padding: 0.28rem 0.4rem;
    min-width: 0;
  }

  .t {
    font-size: 1.25rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .t.ahead {
    color: var(--green);
  }

  .rival-ink {
    color: var(--ink-2);
  }

  .sector-rows {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1;
    justify-content: center;
    min-height: 0;
  }

  .srow {
    display: grid;
    grid-template-columns: 2.2rem repeat(3, 1fr);
    gap: 0.4rem;
    align-items: baseline;
    padding: 0.12rem 0.3rem;
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }

  .v {
    font-size: 0.98rem;
    font-weight: 600;
    text-align: right;
    white-space: nowrap;
  }

  .d {
    color: var(--ink-3);
  }
  .d.good {
    color: var(--green);
  }
  .d.bad {
    color: var(--red);
  }
</style>

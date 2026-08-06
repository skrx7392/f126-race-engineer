<script lang="ts">
  /**
   * Sector board: the lap in progress against the reference.
   *
   * Purple / green / yellow is the one F1 convention worth keeping unchanged in
   * meaning — but not, it turns out, unchanged in value. The literal broadcast
   * colours fail a colourblind-separation check outright (their green and
   * yellow sit at ΔE 5.6 for a protan reader, well under the floor of 6). The
   * values here were re-stepped to clear it at 15.6 while staying immediately
   * recognisable.
   *
   * Each cell also carries a text tag — SB, PB, or a dash — so the ranking is
   * never colour-alone regardless.
   */
  import type { SectorsInfo } from '../lib/protocol';
  import { formatSectorTime, DASH } from '../lib/format';

  interface Props {
    sectors: SectorsInfo | null;
  }

  let { sectors }: Props = $props();

  type Rank = 'purple' | 'green' | 'yellow' | 'pending';

  interface Cell {
    n: number;
    current: number | null;
    best: number | null;
    rank: Rank;
    tag: string;
  }

  const TAGS: Record<Rank, string> = {
    purple: 'SB',
    green: 'PB',
    yellow: '—',
    pending: ''
  };

  let cells: Cell[] = $derived(
    [0, 1, 2].map((i) => {
      const current = sectors?.current_lap?.[i] ?? null;
      const best = sectors?.best_lap?.[i] ?? null;
      const sessionBest = sectors?.session_best?.[i] ?? null;

      let rank: Rank = 'pending';
      if (current !== null) {
        if (sessionBest === null || current <= sessionBest) rank = 'purple';
        else if (best === null || current <= best) rank = 'green';
        else rank = 'yellow';
      }
      return { n: i + 1, current, best, rank, tag: TAGS[rank] };
    })
  );
</script>

<section class="sectors panel" data-panel="sectors">
  <div class="panel-head">
    <span class="label">Sectors</span>
    <span class="label">current · best</span>
  </div>

  <div class="grid">
    {#each cells as cell (cell.n)}
      <div class="cell {cell.rank}">
        <div class="top">
          <span class="s label">S{cell.n}</span>
          {#if cell.tag}<span class="tag">{cell.tag}</span>{/if}
        </div>
        <span class="current clock">{formatSectorTime(cell.current)}</span>
        <span class="best clock">{cell.best === null ? DASH : formatSectorTime(cell.best)}</span>
      </div>
    {/each}
  </div>
</section>

<style>
  .sectors {
    grid-area: sectors;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.35rem;
    flex: 1;
    min-height: 0;
  }

  .cell {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.1rem;
    padding: 0.3rem 0.45rem;
    border-radius: var(--radius-sm);
    background: var(--surface-2);
    border-inline-start: 3px solid var(--line-strong);
    min-width: 0;
  }

  .top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.3rem;
  }

  .tag {
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    color: var(--ink-3);
  }

  .current {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--ink);
    white-space: nowrap;
  }

  .best {
    font-size: 0.82rem;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .cell.purple {
    background: var(--purple-dim);
    border-inline-start-color: var(--purple);
  }
  .cell.purple .current,
  .cell.purple .tag {
    color: var(--purple);
  }

  .cell.green {
    background: var(--green-dim);
    border-inline-start-color: var(--green);
  }
  .cell.green .current,
  .cell.green .tag {
    color: var(--green);
  }

  .cell.yellow {
    background: var(--yellow-dim);
    border-inline-start-color: var(--yellow);
  }
  .cell.yellow .current,
  .cell.yellow .tag {
    color: var(--yellow);
  }

  .cell.pending .current {
    color: var(--ink-3);
  }
</style>

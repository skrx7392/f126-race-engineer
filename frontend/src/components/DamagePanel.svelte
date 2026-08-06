<script lang="ts">
  /**
   * Damage, as numbers.
   *
   * A car silhouette would be prettier and slower to read. The question this
   * panel answers is "is anything broken, and how badly" — undamaged components
   * recede to almost nothing so a single amber or red entry is the only thing
   * with contrast in the panel. When the car is healthy this reads as empty,
   * which is exactly the right amount of attention for it to take.
   */
  import type { DamageInfo } from '../lib/protocol';

  interface Props {
    damage: DamageInfo | null;
  }

  let { damage }: Props = $props();

  const PARTS: ReadonlyArray<{ key: keyof DamageInfo; short: string }> = [
    { key: 'front_left_wing_pct', short: 'FLW' },
    { key: 'front_right_wing_pct', short: 'FRW' },
    { key: 'rear_wing_pct', short: 'RW' },
    { key: 'floor_pct', short: 'FLR' },
    { key: 'diffuser_pct', short: 'DIF' },
    { key: 'sidepod_pct', short: 'SPD' },
    { key: 'gearbox_pct', short: 'GBX' },
    { key: 'engine_pct', short: 'ENG' }
  ];

  interface Part {
    short: string;
    value: number;
    level: 'ok' | 'warn' | 'bad';
  }

  let parts: Part[] = $derived(
    PARTS.map(({ key, short }) => {
      const value = damage?.[key] ?? 0;
      return { short, value, level: value === 0 ? 'ok' : value < 25 ? 'warn' : 'bad' };
    })
  );

  let worst = $derived(parts.reduce((m, p) => Math.max(m, p.value), 0));
</script>

<section class="damage panel" data-panel="damage">
  <div class="panel-head">
    <span class="label">Damage</span>
    <span class="label" class:clean={worst === 0}>{worst === 0 ? 'Clean' : `Max ${worst}%`}</span>
  </div>

  <div class="grid">
    {#each parts as part (part.short)}
      <div class="part {part.level}">
        <span class="short">{part.short}</span>
        <span class="pct">{part.value}</span>
      </div>
    {/each}
  </div>
</section>

<style>
  .damage {
    grid-area: damage;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.2rem;
    flex: 1;
    min-height: 0;
    align-content: center;
  }

  .part {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.05rem;
    padding: 0.18rem 0.1rem;
    border-radius: var(--radius-sm);
    background: var(--surface-2);
    min-width: 0;
  }

  .short {
    font-size: 0.58rem;
    font-weight: 650;
    letter-spacing: 0.08em;
    color: var(--ink-3);
  }

  .pct {
    font-size: 1rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    color: var(--ink-2);
  }

  /* Healthy components sink into the panel. */
  .part.ok {
    background: transparent;
  }

  .part.ok .pct {
    color: var(--ink-3);
    opacity: 0.55;
  }

  .part.warn {
    background: var(--amber-dim);
  }
  .part.warn .pct {
    color: var(--amber);
  }

  .part.bad {
    background: var(--red-dim);
  }
  .part.bad .pct {
    color: var(--red);
  }

  .clean {
    color: var(--green);
  }
</style>

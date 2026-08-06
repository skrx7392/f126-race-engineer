<script lang="ts">
  /**
   * One labelled readout. Used everywhere a panel shows a number, so the
   * label/value/unit rhythm stays identical across the whole dashboard.
   */
  interface Props {
    label: string;
    value: string;
    unit?: string;
    /** Semantic ink for the value; structure stays grey. */
    tone?: 'default' | 'green' | 'red' | 'amber' | 'blue' | 'purple' | 'yellow' | 'muted';
    /** Clock readings use the mono face; gauge readings use the display face. */
    kind?: 'gauge' | 'clock';
    size?: 'sm' | 'md' | 'lg';
  }

  let { label, value, unit, tone = 'default', kind = 'gauge', size = 'md' }: Props = $props();
</script>

<div class="stat {size}">
  <span class="label">{label}</span>
  <span class="value {kind} tone-{tone}">
    {value}{#if unit}<span class="unit">{unit}</span>{/if}
  </span>
</div>

<style>
  .stat {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
  }

  .value {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    color: var(--ink);
  }

  .sm .value {
    font-size: 1.05rem;
    font-weight: 700;
  }
  .md .value {
    font-size: 1.5rem;
    font-weight: 800;
  }
  .lg .value {
    font-size: 2.2rem;
    font-weight: 800;
  }

  .unit {
    font-size: 0.6em;
    font-weight: 600;
    color: var(--ink-3);
    margin-inline-start: 0.15em;
    letter-spacing: 0;
  }

  .tone-green {
    color: var(--green);
  }
  .tone-red {
    color: var(--red);
  }
  .tone-amber {
    color: var(--amber);
  }
  .tone-blue {
    color: var(--blue);
  }
  .tone-purple {
    color: var(--purple);
  }
  .tone-yellow {
    color: var(--yellow);
  }
  .tone-muted {
    color: var(--ink-3);
  }
</style>

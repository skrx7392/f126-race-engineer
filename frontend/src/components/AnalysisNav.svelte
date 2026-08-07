<script lang="ts">
  /**
   * The analysis chrome — and the only chrome on this side of the app.
   *
   * The pit wall has no navigation at all, on purpose: it is a kiosk with one
   * screen and nothing to navigate to. Analysis is the opposite, so it gets a
   * bar. It stays deliberately thin and quiet — one row, small caps, no colour
   * — because everything below it is the actual subject, and a heavy header
   * would be the loudest thing on a page whose job is to show fine differences
   * in grey traces.
   *
   * Compare, corners and stints only mean something once laps are selected, so
   * they carry the current selection in their links rather than dead-ending on
   * an empty state.
   */
  import { analysis } from '../lib/analysis.svelte';
  import { href, type RouteName } from '../lib/router.svelte';
  import { lapLabel } from '../lib/analysis-format';

  interface Props {
    current: RouteName;
  }

  let { current }: Props = $props();

  let sessionId = $derived(analysis.a?.sessionId ?? null);

  let links = $derived([
    { name: 'sessions' as RouteName, label: 'Sessions', to: href('/sessions') },
    { name: 'compare' as RouteName, label: 'Compare', to: analysis.compareHref() },
    { name: 'corners' as RouteName, label: 'Corners', to: analysis.cornersHref() },
    {
      name: 'stints' as RouteName,
      label: 'Stints',
      to: href('/stints', { session: sessionId })
    },
    // Strategy is track-scoped, so it carries no lap selection — it is reached with no
    // parameters and picks a circuit for itself.
    { name: 'strategy' as RouteName, label: 'Strategy', to: href('/strategy') }
  ]);
</script>

<nav class="analysis-nav" aria-label="Analysis">
  <a class="wordmark" href={href('/sessions')}>
    Race Engineer <span class="label sub">Analysis</span>
  </a>

  <ul class="links">
    {#each links as link (link.name)}
      <li>
        <a
          href={link.to}
          class="link"
          class:active={current === link.name}
          aria-current={current === link.name ? 'page' : undefined}
        >
          {link.label}
        </a>
      </li>
    {/each}
  </ul>

  <!--
    The selection, always visible. It is the state every page on this side
    shares, and without it "Compare" is a link whose destination you cannot
    predict.
  -->
  <div class="selection" data-testid="selection">
    {#if analysis.a}
      <span class="chip slot-a">A {lapLabel(analysis.a.sessionId, analysis.a.lap)}</span>
    {/if}
    {#if analysis.b}
      <span class="chip slot-b">B {lapLabel(analysis.b.sessionId, analysis.b.lap)}</span>
    {/if}
    {#if analysis.a || analysis.b}
      <button class="clear" type="button" onclick={() => analysis.clearSelection()}>Clear</button>
    {/if}
  </div>

  <a class="live" href="#/" title="Back to the live pit wall">Live</a>
</nav>

<style>
  .analysis-nav {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.4rem 0.9rem;
    background: var(--surface-1);
    border-bottom: 1px solid var(--line);
    min-height: 2.4rem;
    flex-wrap: wrap;
  }

  .wordmark {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    font-size: 0.95rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--ink);
    text-decoration: none;
    white-space: nowrap;
  }

  .sub {
    color: var(--ink-3);
  }

  .links {
    display: flex;
    gap: 0.15rem;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .link {
    display: block;
    padding: 0.2rem 0.55rem;
    border-radius: var(--radius-sm);
    font-size: 0.82rem;
    font-weight: 600;
    color: var(--ink-2);
    text-decoration: none;
    white-space: nowrap;
  }

  .link:hover {
    color: var(--ink);
    background: var(--surface-2);
  }

  /*
   * The active route is marked by a filled surface rather than a colour. There
   * is no spare hue in this interface, and the one place a hue would be free is
   * exactly where the charts need it.
   */
  .link.active {
    color: var(--ink);
    background: var(--surface-3);
  }

  .selection {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    margin-inline-start: auto;
    min-width: 0;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.12rem 0.4rem;
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: 0.72rem;
    font-weight: 600;
    white-space: nowrap;
    border: 1px solid;
  }

  /* The two lap colours, matching the traces they stand for. */
  .slot-a {
    color: var(--chart-lap-a);
    border-color: color-mix(in srgb, var(--chart-lap-a) 45%, transparent);
    background: color-mix(in srgb, var(--chart-lap-a) 14%, transparent);
  }

  .slot-b {
    color: var(--chart-lap-b);
    border-color: color-mix(in srgb, var(--chart-lap-b) 45%, transparent);
    background: color-mix(in srgb, var(--chart-lap-b) 14%, transparent);
  }

  .clear,
  .live {
    padding: 0.15rem 0.45rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line);
    background: transparent;
    color: var(--ink-3);
    font: inherit;
    font-size: 0.72rem;
    font-weight: 600;
    text-decoration: none;
    cursor: pointer;
    white-space: nowrap;
  }

  .clear:hover,
  .live:hover {
    color: var(--ink);
    border-color: var(--line-strong);
  }
</style>

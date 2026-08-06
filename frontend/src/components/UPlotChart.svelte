<script lang="ts">
  /**
   * The one place uPlot is constructed.
   *
   * Everything else in the app describes charts as data plus a series list and
   * never touches the library, which is what keeps four different pages drawing
   * panes that line up with each other. The wrapper owns four things that are
   * easy to get subtly wrong per-call-site and are therefore centralised:
   *
   *   sizing   — uPlot needs explicit pixel dimensions and does not observe its
   *              container. A ResizeObserver drives `setSize`, so a pane reflows
   *              with the page instead of being measured once at mount.
   *   sync     — panes sharing a `syncKey` share a cursor through uPlot's own
   *              sync API. Zoom is *not* part of that API, so it is lifted into
   *              the caller's state and pushed back down through `window`.
   *   legend   — uPlot's built-in legend is replaced by HTML, so the readout
   *              uses the app's type rules (clocks mono, gauges sans) and the
   *              same ink tokens as every other panel.
   *   fallback — when there is no 2D canvas (jsdom under test, or a locked-down
   *              browser) the chart degrades to its data table rather than
   *              throwing. The table is worth having regardless: it is the
   *              non-visual reading of the same numbers.
   */
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { untrack, type Snippet } from 'svelte';
  import { CHART_INK, CURSOR_STYLE, Y_AXIS_SIZE } from '../lib/chart-theme';
  import { DASH } from '../lib/format';

  /**
   * A series as the pages describe it: uPlot's own options, plus two keys this
   * component consumes and strips (`stepped`, `format`).
   */
  export interface SeriesSpec extends Record<string, unknown> {
    label?: string;
    /**
     * A colour, or a uPlot callback returning one (a canvas gradient, for the
     * diverging delta fill). Typed loosely on purpose — uPlot accepts both, and
     * pinning it to `string` would forbid the one series that needs the other.
     */
    stroke?: unknown;
    /** Draw as a step function — correct for gears and any held integer. */
    stepped?: boolean;
    /** Formats the hovered value for the HTML legend. */
    format?: (v: number | null | undefined) => string;
    /** Suffix shown after the legend value, e.g. `km/h`. */
    unit?: string;
    /** Hide from the legend without hiding from the plot. */
    legendHidden?: boolean;
  }

  interface Props {
    /** uPlot aligned data: `[xs, ...ys]`. */
    data: Array<Array<number | null>>;
    /** One entry per data row. Index 0 is the x series. */
    series: SeriesSpec[];
    height?: number;
    /** Panes sharing a key share a cursor. */
    syncKey?: string;
    /** Externally controlled x window. Null means "the whole domain". */
    window?: { min: number; max: number } | null;
    /** Fired when the user drags to zoom, or double-clicks to reset (null). */
    onWindow?: (w: { min: number; max: number } | null) => void;
    /** Extra uPlot options merged last — scales, bands, axes. */
    options?: Record<string, unknown>;
    /**
     * Extra hooks, merged *into* the wrapper's own rather than replacing them.
     * Kept separate from `options` for exactly that reason: a caller that wants
     * a `draw` hook for a zero line must not silently take out the cursor and
     * scale hooks this component depends on.
     */
    hooks?: Record<string, Array<(...args: never[]) => void>>;
    /** Pane title. A single-series pane needs no legend; the title names it. */
    title?: string;
    /** Right-aligned note in the pane head — units, conventions, counts. */
    note?: string;
    /** Show the HTML legend. Defaults to on when there are ≥2 visible series. */
    legend?: boolean;
    /** Rendered inside the collapsible data view, beneath the plot. */
    dataView?: Snippet;
    /** Test hook / DOM handle. */
    pane?: string;
  }

  let {
    data,
    series,
    height = 160,
    syncKey,
    window: xWindow = null,
    onWindow,
    options,
    hooks,
    title,
    note,
    legend,
    dataView,
    pane
  }: Props = $props();

  let host = $state<HTMLDivElement | null>(null);
  let plot: uPlot | null = null;
  /** Index of the sample under the cursor, or null when the cursor is away. */
  let cursorIdx = $state<number | null>(null);
  /** Set once at mount; a missing 2D context is a permanent property of the env. */
  let canvasOk = $state(true);

  const visibleSeries = $derived(series.slice(1).filter((s) => !s.legendHidden));
  const showLegend = $derived(legend ?? visibleSeries.length >= 2);

  /**
   * uPlot mutates the options object it is given, so the series it receives are
   * fresh objects with this component's own keys removed — leaving them in
   * makes uPlot warn about unknown series properties on some versions and, more
   * practically, makes it unclear which keys are ours.
   */
  function toUplotSeries(spec: SeriesSpec): uPlot.Series {
    const { stepped, format, unit, legendHidden, ...rest } = spec;
    void format;
    void unit;
    void legendHidden;
    const out = { ...rest } as Record<string, unknown>;
    if (stepped) out['paths'] = uPlot.paths.stepped?.({ align: 1 });
    return out as uPlot.Series;
  }

  function buildOptions(width: number): uPlot.Options {
    const base: Record<string, unknown> = {
      width,
      height,
      // The HTML legend below replaces it; uPlot's own would add a second type
      // system to every pane.
      legend: { show: false },
      cursor: {
        show: true,
        x: true,
        y: false,
        points: { show: false },
        drag: { x: true, y: false, setScale: true },
        sync: syncKey ? { key: syncKey, setSeries: false, scales: ['x', null] } : undefined
      },
      scales: { x: { time: false } },
      axes: [],
      series: series.map(toUplotSeries),
      hooks: mergeHooks({
        setCursor: [
          (u: uPlot) => {
            cursorIdx = u.cursor.idx ?? null;
          }
        ],
        setScale: [
          (u: uPlot, key: string) => {
            if (key !== 'x' || !onWindow) return;
            const { min, max } = u.scales['x'] ?? {};
            if (min == null || max == null) return;
            const full = u.data[0];
            const lo = full?.[0];
            const hi = full?.[full.length - 1];
            // A scale spanning the whole domain is "no window", not a zoom of
            // exactly the full width — otherwise a reset would read as a zoom
            // and the shared window would never clear.
            const isFull =
              lo != null && hi != null && min <= lo + 1e-6 && max >= hi - 1e-6;
            onWindow(isFull ? null : { min, max });
          }
        ]
      })
    };
    return { ...base, ...options } as unknown as uPlot.Options;
  }

  /** Concatenate the caller's hook arrays onto ours, per hook name. */
  function mergeHooks(
    own: Record<string, Array<(...args: never[]) => void>>
  ): Record<string, Array<(...args: never[]) => void>> {
    if (!hooks) return own;
    const out: Record<string, Array<(...args: never[]) => void>> = { ...own };
    for (const [name, fns] of Object.entries(hooks)) {
      out[name] = [...(out[name] ?? []), ...fns];
    }
    return out;
  }

  /* Create and destroy. Data is read untracked so a data change updates the
     existing plot rather than tearing it down and rebuilding it 20 times a
     second while a cursor drags. */
  $effect(() => {
    const el = host;
    if (!el) return;
    // Re-create when the structural inputs change.
    void series;
    void height;
    void syncKey;
    void options;
    void hooks;

    let ctxOk = false;
    try {
      ctxOk = document.createElement('canvas').getContext('2d') !== null;
    } catch {
      ctxOk = false;
    }
    canvasOk = ctxOk;
    if (!ctxOk) return;

    const initial = untrack(() => data);
    const width = el.clientWidth || 640;
    const u = new uPlot(buildOptions(width), initial as uPlot.AlignedData, el);
    plot = u;

    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0);
      if (w > 0) u.setSize({ width: w, height });
    });
    ro.observe(el);

    // Double-click is uPlot's own reset gesture; it fires `setScale`, which the
    // hook above turns into `onWindow(null)`.
    return () => {
      ro.disconnect();
      u.destroy();
      plot = null;
    };
  });

  /* Push new data into the live plot. */
  $effect(() => {
    const d = data;
    const u = plot;
    if (!u) return;
    u.setData(d as uPlot.AlignedData, /* resetScales */ false);
  });

  /* Push the externally-owned window down. Guarded against re-applying a scale
     the plot already has, which is what would otherwise ping-pong between this
     effect and the `setScale` hook. */
  $effect(() => {
    const w = xWindow;
    const u = plot;
    if (!u) return;
    const cur = u.scales['x'];
    if (w) {
      if (cur?.min === w.min && cur?.max === w.max) return;
      u.setScale('x', { min: w.min, max: w.max });
    } else {
      const xs = u.data[0];
      const lo = xs?.[0];
      const hi = xs?.[xs.length - 1];
      if (lo == null || hi == null) return;
      if (cur?.min === lo && cur?.max === hi) return;
      u.setScale('x', { min: lo, max: hi });
    }
  });

  /** The hovered value for one series, already formatted. */
  function readout(spec: SeriesSpec, rowIndex: number): string {
    const idx = cursorIdx;
    if (idx === null) return DASH;
    const row = data[rowIndex];
    const v = row?.[idx];
    if (spec.format) return spec.format(v);
    if (v == null || !Number.isFinite(v)) return DASH;
    return String(Math.round(v));
  }
</script>

<!--
  `--y-gutter` indents the head and legend to the plot's left edge, so a pane
  title sits above its own axis rather than above the axis labels. It is the
  same number uPlot reserves for the y-axis, which is why that number is a
  shared constant and not a literal in two places.
-->
<figure
  class="chart"
  data-pane={pane}
  style="--y-gutter: {Y_AXIS_SIZE}px; --cursor-stroke: {CURSOR_STYLE.stroke}"
>
  {#if title || note}
    <figcaption class="chart-head">
      {#if title}<span class="label">{title}</span>{/if}
      {#if note}<span class="label note">{note}</span>{/if}
    </figcaption>
  {/if}

  <div class="plot" bind:this={host} style="height: {height}px" data-testid="uplot-host"></div>

  {#if !canvasOk}
    <p class="fallback label">Chart unavailable — the data view below carries the same numbers.</p>
  {/if}

  {#if showLegend}
    <ul class="legend">
      {#each series as spec, i (spec.label ?? i)}
        {#if i > 0 && !spec.legendHidden}
          <li>
            <span
              class="swatch"
              style="background: {typeof spec.stroke === 'string' ? spec.stroke : CHART_INK.axis}"
            ></span>
            <span class="name">{spec.label}</span>
            <span class="value clock">{readout(spec, i)}</span>
            {#if spec.unit}<span class="unit">{spec.unit}</span>{/if}
          </li>
        {/if}
      {/each}
    </ul>
  {/if}

  {#if dataView}
    <details class="data-view">
      <summary class="label">Data view</summary>
      {@render dataView()}
    </details>
  {/if}
</figure>

<style>
  .chart {
    margin: 0;
    min-width: 0;
  }

  .chart-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0 0 0.25rem var(--y-gutter, 0);
    min-width: 0;
  }

  .note {
    color: var(--ink-3);
    letter-spacing: 0.08em;
    text-transform: none;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .plot {
    width: 100%;
    min-width: 0;
  }

  .fallback {
    margin: 0.4rem 0;
    color: var(--ink-3);
    text-transform: none;
    letter-spacing: 0;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.15rem 1rem;
    margin: 0.25rem 0 0;
    padding: 0 0 0 var(--y-gutter, 0);
    list-style: none;
  }

  .legend li {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    min-width: 0;
  }

  /* A 2px mark, matching the line weight it stands for. */
  .swatch {
    width: 0.85rem;
    height: 2px;
    border-radius: 1px;
    flex: none;
  }

  .name {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--ink-2);
    white-space: nowrap;
  }

  .value {
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }

  .unit {
    font-size: 0.62rem;
    color: var(--ink-3);
  }

  .data-view {
    margin-top: 0.4rem;
    border-top: 1px solid var(--line);
    padding-top: 0.3rem;
  }

  .data-view summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .data-view summary:hover {
    color: var(--ink-2);
  }

  /* uPlot draws its own DOM; these reach into it deliberately. */
  .chart :global(.u-over) {
    cursor: crosshair;
  }

  .chart :global(.u-select) {
    background: color-mix(in srgb, var(--ink) 10%, transparent);
    border-left: 1px solid var(--line-strong);
    border-right: 1px solid var(--line-strong);
  }

  /* The shared crosshair. Dashed and thin: it locates a sample without
     competing with the traces it is measuring. */
  .chart :global(.u-cursor-x) {
    border-right: 1px dashed var(--cursor-stroke);
  }

  .chart :global(.u-cursor-y) {
    display: none;
  }
</style>

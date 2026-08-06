<script lang="ts">
  /**
   * The stacked trace panes — the thing the whole analysis side exists to draw.
   *
   * ── Why a stack and not an overlay ───────────────────────────────────────
   * Speed, pedals, gear and time delta share one axis (distance around the lap)
   * and nothing else. Putting them on one pair of axes would mean two y-scales,
   * which is the single worst thing you can do to a chart: it invites the eye to
   * compare two quantities whose relative heights are an artefact of the scale
   * choice. Stacking them keeps one y-scale per pane and lets distance do the
   * aligning, which is also how the eye actually reads telemetry — vertically,
   * down through the channels at one point on the road.
   *
   * ── Colour follows what varies ────────────────────────────────────────────
   * With two laps loaded the lap is the thing that varies, so colour is lap
   * identity (gold A, blue B) and the channel is carried by which pane you are
   * in. With one lap loaded the channel is the thing that varies, so colour is
   * the channel. The two schemes never appear at once. See `chart-theme.ts`.
   *
   * ── The pedal pane ────────────────────────────────────────────────────────
   * Throttle grows up from zero, brake grows down. Two channels in one pane,
   * separated by which half-plane they occupy rather than by colour — which
   * leaves colour free to carry lap identity, and means the pane is still
   * readable with no colour vision at all.
   *
   * ── The delta pane ────────────────────────────────────────────────────────
   * A diverging encoding: a hard datum at zero, green where A is ahead, red
   * where A is behind, the fill anchored to the datum rather than to the floor.
   * The sign convention is stated on the pane itself, every time, because
   * "delta" without a stated direction is a coin flip.
   */
  import UPlotChart, { type SeriesSpec } from './UPlotChart.svelte';
  import type { CompareResponse, Corner, LapTelemetry } from '../lib/analysis-api';
  import {
    CHANNEL,
    DELTA,
    FILL_ALPHA,
    LAP,
    lineSeries,
    stepSeries,
    withAlpha,
    xAxisDistance,
    yAxis
  } from '../lib/chart-theme';
  import { formatDelta, formatGear, DASH } from '../lib/format';
  import { lapLabel } from '../lib/analysis-format';
  import type { Window } from '../lib/analysis.svelte';

  interface Props {
    /** Two-lap mode. Takes precedence when present. */
    compare?: CompareResponse | null;
    /** One-lap mode, used when there is no second lap to compare against. */
    telemetry?: LapTelemetry | null;
    /** Shared distance window; null is the whole lap. */
    window?: Window | null;
    onWindow?: (w: Window | null) => void;
    /** Corner bounds, shaded behind the traces so the panes have landmarks. */
    corners?: Corner[] | null;
    focusedCorner?: number | null;
    /** Height of each trace pane, in pixels. */
    paneHeight?: number;
  }

  let {
    compare = null,
    telemetry = null,
    window: xWindow = null,
    onWindow,
    corners = null,
    focusedCorner = null,
    paneHeight = 150
  }: Props = $props();

  /** One sync key per mounted stack, so two stacks on a page do not cross-talk. */
  const syncKey = `stack-${Math.random().toString(36).slice(2, 9)}`;

  let twoLap = $derived(compare !== null);

  let labelA = $derived(
    compare ? lapLabel(compare.a.session_id, compare.a.lap_number) : DASH
  );
  let labelB = $derived(
    compare ? lapLabel(compare.b.session_id, compare.b.lap_number) : DASH
  );

  const pct = (v: number | null | undefined): string =>
    v == null || !Number.isFinite(v) ? DASH : `${Math.round(Math.abs(v) * 100)}`;
  const kmh = (v: number | null | undefined): string =>
    v == null || !Number.isFinite(v) ? DASH : String(Math.round(v));

  // ── data ───────────────────────────────────────────────────────────────────

  let xs = $derived(compare ? compare.grid_m : (telemetry?.distance_m ?? []));

  let speedData = $derived.by(() => {
    if (compare) return [xs, compare.a.speed_kmh, compare.b.speed_kmh];
    return [xs, telemetry?.speed_kmh ?? []];
  });

  /**
   * Pedals, mirrored about zero. Brake is negated so it grows downward; the
   * axis relabels the negative half as a positive percentage, because the sign
   * is a layout device, not a quantity anyone should read.
   */
  let pedalData = $derived.by(() => {
    if (compare) {
      return [
        xs,
        compare.a.throttle,
        compare.b.throttle,
        compare.a.brake.map((v) => -v),
        compare.b.brake.map((v) => -v)
      ];
    }
    return [xs, telemetry?.throttle ?? [], (telemetry?.brake ?? []).map((v) => -v)];
  });

  let gearData = $derived.by(() => {
    if (compare) return [xs, compare.a.gear, compare.b.gear];
    return [xs, telemetry?.gear ?? []];
  });

  let deltaData = $derived.by(() => (compare ? [xs, compare.delta_ms] : [xs, []]));

  // ── series ─────────────────────────────────────────────────────────────────

  let speedSeries = $derived.by((): SeriesSpec[] => {
    const x: SeriesSpec = {};
    if (compare) {
      return [
        x,
        { ...lineSeries(labelA, LAP.a), format: kmh, unit: 'km/h' },
        { ...lineSeries(labelB, LAP.b), format: kmh, unit: 'km/h' }
      ];
    }
    return [x, { ...lineSeries('Speed', CHANNEL.speed), format: kmh, unit: 'km/h' }];
  });

  let pedalSeries = $derived.by((): SeriesSpec[] => {
    const x: SeriesSpec = {};
    if (compare) {
      return [
        x,
        { ...lineSeries(`${labelA} throttle`, LAP.a), format: pct, unit: '%' },
        { ...lineSeries(`${labelB} throttle`, LAP.b), format: pct, unit: '%' },
        {
          ...lineSeries(`${labelA} brake`, LAP.a),
          dash: [4, 3],
          format: pct,
          unit: '%'
        },
        {
          ...lineSeries(`${labelB} brake`, LAP.b),
          dash: [4, 3],
          format: pct,
          unit: '%'
        }
      ];
    }
    return [
      x,
      {
        ...lineSeries('Throttle', CHANNEL.throttle),
        fill: withAlpha(CHANNEL.throttle, FILL_ALPHA),
        fillTo: () => 0,
        format: pct,
        unit: '%'
      },
      {
        ...lineSeries('Brake', CHANNEL.brake),
        fill: withAlpha(CHANNEL.brake, FILL_ALPHA),
        fillTo: () => 0,
        format: pct,
        unit: '%'
      }
    ];
  });

  let gearSeries = $derived.by((): SeriesSpec[] => {
    const x: SeriesSpec = {};
    if (compare) {
      return [
        x,
        { ...stepSeries(labelA, LAP.a), format: formatGear },
        { ...stepSeries(labelB, LAP.b), format: formatGear }
      ];
    }
    return [x, { ...stepSeries('Gear', CHANNEL.gear), format: formatGear }];
  });

  let deltaSeries = $derived.by((): SeriesSpec[] => [
    {},
    {
      label: 'Cumulative delta',
      width: 2,
      scale: 'y',
      points: { show: false },
      /*
       * Both the stroke and the fill are vertical gradients with a hard stop at
       * the zero pixel, so a trace that crosses the datum changes colour
       * exactly where it crosses. Two clipped series would need a synthetic
       * point at every crossing to avoid a notch; one gradient cannot get that
       * wrong.
       */
      stroke: (u: {
        ctx: CanvasRenderingContext2D;
        valToPos: (v: number, scale: string, canvasPx?: boolean) => number;
        bbox: { top: number; height: number };
      }) => divergingGradient(u, 1),
      fill: (u: {
        ctx: CanvasRenderingContext2D;
        valToPos: (v: number, scale: string, canvasPx?: boolean) => number;
        bbox: { top: number; height: number };
      }) => divergingGradient(u, FILL_ALPHA),
      // Fill to the datum, not to the floor: the area being read is the area
      // between the curve and zero.
      fillTo: () => 0,
      format: (v: number | null | undefined) => formatDelta(v),
      unit: 's'
    }
  ]);

  /** Red above the datum (A behind), green below it (A ahead). */
  function divergingGradient(
    u: {
      ctx: CanvasRenderingContext2D;
      valToPos: (v: number, scale: string, canvasPx?: boolean) => number;
      bbox: { top: number; height: number };
    },
    alpha: number
  ): CanvasGradient | string {
    const top = u.bbox.top;
    const bottom = top + u.bbox.height;
    let zero: number;
    try {
      zero = u.valToPos(0, 'y', true);
    } catch {
      return withAlpha(DELTA.behind, alpha);
    }
    if (!Number.isFinite(zero)) return withAlpha(DELTA.behind, alpha);

    const g = u.ctx.createLinearGradient(0, top, 0, bottom);
    const stop = Math.max(0, Math.min(1, (zero - top) / Math.max(1, bottom - top)));
    const behind = withAlpha(DELTA.behind, alpha);
    const ahead = withAlpha(DELTA.ahead, alpha);
    g.addColorStop(0, behind);
    g.addColorStop(stop, behind);
    g.addColorStop(stop, ahead);
    g.addColorStop(1, ahead);
    return g;
  }

  // ── axes and scales ────────────────────────────────────────────────────────

  const pedalAxis = yAxis({
    label: undefined,
    splits: [-1, -0.5, 0, 0.5, 1],
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => `${Math.round(Math.abs(t) * 100)}`)
  });

  const gearAxis = yAxis({
    splits: [1, 2, 3, 4, 5, 6, 7, 8],
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => (t % 2 === 0 ? String(t) : ''))
  });

  const deltaAxis = yAxis({
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => (t / 1000).toFixed(1))
  });

  const pedalScale = { y: { range: [-1.05, 1.05] as [number, number] } };
  const gearScale = { y: { range: [0.5, 8.5] as [number, number] } };

  /**
   * The delta scale always contains zero.
   *
   * Left to auto-range, a lap that is behind everywhere gets a scale running
   * from +0.4 s to +1.6 s — and then the fill, which is anchored to zero, runs
   * off the bottom of the pane and reads as a solid block measured from an
   * invisible baseline. Zooming into a corner makes it worse, because the
   * visible range narrows around whatever the delta happens to be there. A
   * diverging encoding whose datum is off-screen is not a diverging encoding,
   * so zero is pinned into the range whatever the data does.
   */
  const deltaScale = {
    y: {
      range: (_u: unknown, min: number, max: number): [number, number] => {
        const lo = Math.min(0, Number.isFinite(min) ? min : 0);
        const hi = Math.max(0, Number.isFinite(max) ? max : 0);
        const pad = Math.max((hi - lo) * 0.12, 25);
        return [lo - pad, hi + pad];
      }
    }
  };

  // ── corner shading ─────────────────────────────────────────────────────────

  /**
   * Corner windows drawn behind the traces, as a faint band each.
   *
   * These are landmarks, not data — they answer "which corner am I looking at?"
   * without a legend. Drawn in the surface tint rather than a hue, so they sit
   * behind the traces the way a gridline does.
   */
  let drawHooks = $derived.by(() => {
    const list = corners;
    if (!list || list.length === 0) return undefined;
    const focus = focusedCorner;
    return {
      drawClear: [
        (u: {
          ctx: CanvasRenderingContext2D;
          valToPos: (v: number, scale: string, canvasPx?: boolean) => number;
          bbox: { top: number; height: number; left: number; width: number };
        }) => {
          const { ctx } = u;
          ctx.save();
          ctx.beginPath();
          ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
          ctx.clip();
          for (const c of list) {
            const x0 = u.valToPos(c.entry_m, 'x', true);
            const x1 = u.valToPos(c.exit_m, 'x', true);
            if (!Number.isFinite(x0) || !Number.isFinite(x1)) continue;
            ctx.fillStyle = c.n === focus ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.035)';
            ctx.fillRect(x0, u.bbox.top, Math.max(1, x1 - x0), u.bbox.height);
          }
          ctx.restore();
        }
      ]
    };
  });

  /** A zero datum for the delta pane, drawn over the fill and under the cursor. */
  const deltaHooks = {
    draw: [
      (u: {
        ctx: CanvasRenderingContext2D;
        valToPos: (v: number, scale: string, canvasPx?: boolean) => number;
        bbox: { top: number; height: number; left: number; width: number };
      }) => {
        const y = u.valToPos(0, 'y', true);
        if (!Number.isFinite(y)) return;
        const { ctx } = u;
        ctx.save();
        ctx.strokeStyle = DELTA.zero;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(u.bbox.left, y);
        ctx.lineTo(u.bbox.left + u.bbox.width, y);
        ctx.stroke();
        ctx.restore();
      }
    ]
  };

  /** Corner shading plus, on the delta pane, the datum. */
  let deltaAllHooks = $derived({ ...(drawHooks ?? {}), ...deltaHooks });

  let finalDelta = $derived(compare?.delta_ms.at(-1) ?? null);
  let hasData = $derived(xs.length > 0);
</script>

{#if hasData}
  <div class="stack" data-testid="compare-stack">
    <UPlotChart
      pane="speed"
      title="Speed"
      note="km/h vs distance (m)"
      data={speedData}
      series={speedSeries}
      height={paneHeight + 30}
      {syncKey}
      window={xWindow}
      {onWindow}
      hooks={drawHooks}
      options={{ axes: [xAxisDistance(false), yAxis()] }}
    />

    <UPlotChart
      pane="pedals"
      title="Throttle ▲ / Brake ▼"
      note={twoLap ? 'dashed = brake · % applied' : '% applied'}
      data={pedalData}
      series={pedalSeries}
      height={paneHeight}
      {syncKey}
      window={xWindow}
      {onWindow}
      hooks={drawHooks}
      options={{ axes: [xAxisDistance(false), pedalAxis], scales: { x: { time: false }, ...pedalScale } }}
    />

    <UPlotChart
      pane="gear"
      title="Gear"
      note="held between shifts"
      data={gearData}
      series={gearSeries}
      height={paneHeight - 40}
      {syncKey}
      window={xWindow}
      {onWindow}
      hooks={drawHooks}
      options={{
        axes: [xAxisDistance(!twoLap), gearAxis],
        scales: { x: { time: false }, ...gearScale }
      }}
    />

    {#if twoLap}
      <!--
        The convention is spelled out on the pane and repeated in the note.
        A delta chart whose direction you have to infer is worse than no chart.
      -->
      <UPlotChart
        pane="delta"
        title="Cumulative delta"
        note={`below zero = ${labelA} ahead · seconds`}
        data={deltaData}
        series={deltaSeries}
        height={paneHeight + 10}
        {syncKey}
        window={xWindow}
        {onWindow}
        hooks={deltaAllHooks}
        options={{
          axes: [xAxisDistance(true), deltaAxis],
          scales: { x: { time: false }, ...deltaScale }
        }}
        legend={false}
      >
        {#snippet dataView()}
          <table class="data-table mini">
            <caption class="label">Sector times</caption>
            <thead>
              <tr>
                <th scope="col">Lap</th>
                <th scope="col" class="num">S1</th>
                <th scope="col" class="num">S2</th>
                <th scope="col" class="num">S3</th>
                <th scope="col" class="num">Lap</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{labelA}</td>
                {#each compare?.sectors_a ?? [] as s, i (i)}
                  <td class="num clock">{formatDelta(s === null ? null : s, 3).slice(1)}</td>
                {/each}
                <td class="num clock">{formatDelta(compare?.a.lap_time_ms, 3).slice(1)}</td>
              </tr>
              <tr>
                <td>{labelB}</td>
                {#each compare?.sectors_b ?? [] as s, i (i)}
                  <td class="num clock">{formatDelta(s === null ? null : s, 3).slice(1)}</td>
                {/each}
                <td class="num clock">{formatDelta(compare?.b.lap_time_ms, 3).slice(1)}</td>
              </tr>
            </tbody>
          </table>
        {/snippet}
      </UPlotChart>

      <p class="convention">
        <span class="key ahead"></span> {labelA} ahead
        <span class="key behind"></span> {labelA} behind
        <span class="total clock">
          Final {formatDelta(finalDelta)} s
        </span>
      </p>
    {/if}
  </div>
{:else}
  <p class="idle label">No trace loaded.</p>
{/if}

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    background: var(--surface-1);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 0.6rem 0.7rem 0.7rem;
    min-width: 0;
  }

  .convention {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
    padding-left: 52px; /* the shared y-axis gutter */
    font-size: 0.72rem;
    color: var(--ink-2);
  }

  .key {
    width: 0.8rem;
    height: 0.55rem;
    border-radius: 2px;
    display: inline-block;
  }

  .key.ahead {
    background: color-mix(in srgb, var(--chart-ahead) 55%, transparent);
    border: 1px solid var(--chart-ahead);
  }

  .key.behind {
    background: color-mix(in srgb, var(--chart-behind) 55%, transparent);
    border: 1px solid var(--chart-behind);
    margin-inline-start: 0.6rem;
  }

  .total {
    margin-inline-start: auto;
    color: var(--ink);
    font-weight: 600;
  }

  .idle {
    padding: 1.5rem;
    text-align: center;
    color: var(--ink-3);
  }

  .mini {
    font-size: 0.75rem;
  }
</style>

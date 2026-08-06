/**
 * The chart palette, and the uPlot furniture every pane shares.
 *
 * ── Why these are not the app.css tokens ─────────────────────────────────────
 * They are the same hues, re-stepped. The pit-wall tokens were chosen to clear
 * ~4.7:1 as *text* on `--bg`, which puts them at OKLCH L 0.65–0.92 — brighter
 * than the 0.48–0.67 band that chart marks want. A 2px line at L 0.78 glares
 * against graphite and, more importantly, hues that bright collapse into each
 * other under simulated colourblindness because lightness stops separating
 * them. Every value below was produced by taking the app hue, stepping its
 * lightness into the band at the app's own chroma, and then running the
 * separation checks until they passed. `--red` survived almost unchanged;
 * green had to come down a step, and that step is exactly what lifts
 * throttle-vs-brake from ΔE 5.5 (a hard fail) to 9.5.
 *
 * ── The two palettes are mutually exclusive on screen ────────────────────────
 * Colour follows the entity that varies, and which entity varies depends on how
 * many laps are loaded:
 *
 *   one lap  → the channels vary. Colour = channel (speed/throttle/brake/gear).
 *   two laps → the lap varies. Colour = lap (A/B), and the channel is carried by
 *              which pane you are looking at, which is a complete encoding on
 *              its own.
 *
 * So CHANNEL and LAP never appear together, and neither has to separate from
 * the other. The delta pane is a third thing — a diverging fill with a zero
 * datum — and it is the only place green and red mean "ahead" and "behind".
 *
 * ── Validation (scripts/validate_palette.js, dark, surface #131519) ──────────
 *   CHANNEL, adjacent pairs (the stacked line panes):
 *     PASS band · PASS chroma · PASS CVD 9.5 · PASS normal 27.4 · PASS ≥3:1
 *   CHANNEL, the one pane holding two series at once (throttle vs brake):
 *     PASS all-pairs, CVD 9.5, normal 34.6
 *   LAP pair, all pairs:
 *     PASS band · PASS chroma · PASS CVD 31.8 · PASS normal 34.8 · PASS ≥3:1
 *   DELTA diverging pair:
 *     PASS all, CVD 9.5, normal 34.6
 *
 * Two known, deliberate results:
 *   - speed↔gear falls to ΔE 0.8 under deuteranopia. They are never in the same
 *     pane and never in the same legend; each pane holds one series and names it
 *     in its own title, so nothing is identified by colour alone.
 *   - lap A (gold) ↔ delta-behind (red) falls to ΔE 1.3 under deuteranopia. Also
 *     never the same pane: one is a 2px line in a trace pane, the other a 22%
 *     fill under a zero datum, and the delta additionally carries its sign as a
 *     numeral and as which side of the datum it grows on.
 */

/** The surface charts are drawn on — `--surface-1`. The validator's reference. */
export const CHART_SURFACE = '#131519';

/**
 * Channel identity, used when a single lap is loaded. Fixed order; never cycled.
 */
export const CHANNEL = {
  speed: '#1994FF',
  throttle: '#009A4B',
  brake: '#FF4236',
  gear: '#AE66FF'
} as const;

/**
 * Lap identity, used when two laps are loaded.
 *
 * Gold is the subject (bright, in front); blue is the reference (a step darker,
 * recessive). The lightness gap is not styling — it is what carries the pair
 * through colourblind simulation, where the hue difference alone does not
 * survive. Warm/cool plus light/dark is the most robust two-series encoding
 * there is.
 */
export const LAP = {
  a: '#B98B02',
  b: '#3364DB'
} as const;

/** Diverging: which side of the zero datum the cumulative delta is on. */
export const DELTA = {
  ahead: '#009A4B',
  behind: '#FF4236',
  /** The datum itself is neutral — a diverging scale never has a hue at zero. */
  zero: '#6B7482'
} as const;

/**
 * The palette as CSS custom properties, for the chips, bars and swatches drawn
 * around the canvas.
 *
 * These are injected rather than declared in `app.css` so the values exist in
 * exactly one place. uPlot paints to a canvas and needs literal colour strings
 * in JavaScript, so the constants above have to be JavaScript; mirroring them
 * into a stylesheet would mean two copies of every hue and a silent drift the
 * first time one is re-stepped — a swatch that no longer matches the trace it
 * labels, with nothing to catch it.
 */
export const CSS_VARIABLES: Readonly<Record<string, string>> = {
  '--chart-speed': CHANNEL.speed,
  '--chart-throttle': CHANNEL.throttle,
  '--chart-brake': CHANNEL.brake,
  '--chart-gear': CHANNEL.gear,
  '--chart-lap-a': LAP.a,
  '--chart-lap-b': LAP.b,
  '--chart-ahead': DELTA.ahead,
  '--chart-behind': DELTA.behind
};

/** Write the palette onto an element, `:root` by default. Call once at start-up. */
export function installChartPalette(root?: HTMLElement): void {
  const el = root ?? (typeof document === 'undefined' ? null : document.documentElement);
  if (!el) return;
  for (const [name, value] of Object.entries(CSS_VARIABLES)) {
    el.style.setProperty(name, value);
  }
}

/** Chart furniture, pulled from the app's graphite so panes sit in the page. */
export const CHART_INK = {
  axis: '#99a1ad',
  axisFaint: '#626b79',
  grid: '#222730',
  gridStrong: '#39404c',
  cursor: '#8892a0'
} as const;

/** Opacity for area fills under a line. Fills recede; lines carry identity. */
export const FILL_ALPHA = 0.22;
/** Opacity for the ghost band showing an excluded or invalid sample. */
export const GHOST_ALPHA = 0.35;

/** `#rrggbb` + alpha -> `#rrggbbaa`. uPlot takes any CSS colour string. */
export function withAlpha(hex: string, alpha: number): string {
  const a = Math.round(Math.max(0, Math.min(1, alpha)) * 255)
    .toString(16)
    .padStart(2, '0');
  return `${hex}${a}`;
}

// ── uPlot furniture ──────────────────────────────────────────────────────────

/**
 * Font stacks for canvas text. uPlot draws axis labels itself, so it needs the
 * literal font shorthand rather than a class — these mirror app.css so a tick
 * label and a table cell are visibly the same typeface.
 */
const SANS =
  'ui-sans-serif, -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", system-ui, sans-serif';

export const AXIS_FONT = `500 11px ${SANS}`;
export const LABEL_FONT = `650 10px ${SANS}`;

/**
 * A y-axis in the house style: faint grid, no axis line, tick labels in the
 * secondary ink. `size` is the gutter reserved for labels — fixed rather than
 * auto so a stack of panes shares one left edge and the traces line up
 * vertically, which is the entire point of stacking them.
 */
export interface AxisSpec {
  label?: string;
  size?: number;
  values?: (self: unknown, ticks: number[]) => Array<string | number | null>;
  splits?: number[];
  side?: 0 | 1 | 2 | 3;
  grid?: boolean;
}

/** The shared left-gutter width, in CSS pixels. */
export const Y_AXIS_SIZE = 52;

export function yAxis(spec: AxisSpec = {}): Record<string, unknown> {
  return {
    scale: 'y',
    side: spec.side ?? 3,
    size: spec.size ?? Y_AXIS_SIZE,
    stroke: CHART_INK.axis,
    font: AXIS_FONT,
    labelFont: LABEL_FONT,
    label: spec.label,
    labelSize: spec.label ? 16 : 0,
    labelGap: 2,
    gap: 4,
    ticks: { show: false },
    splits: spec.splits,
    values: spec.values,
    grid: {
      show: spec.grid ?? true,
      stroke: CHART_INK.grid,
      width: 1
    }
  };
}

/** The distance axis. Shown only on the bottom pane of a stack. */
export function xAxisDistance(show: boolean): Record<string, unknown> {
  return {
    scale: 'x',
    side: 2,
    show,
    size: show ? 30 : 0,
    stroke: CHART_INK.axis,
    font: AXIS_FONT,
    gap: 4,
    ticks: { show: false },
    values: (_u: unknown, ticks: number[]) => ticks.map((t) => `${Math.round(t)}`),
    grid: { show: true, stroke: CHART_INK.grid, width: 1 }
  };
}

/** A 2px trace. The default mark: thin, no points, no fill. */
export function lineSeries(
  label: string,
  color: string,
  extra: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    label,
    stroke: color,
    width: 2,
    scale: 'y',
    points: { show: false },
    ...extra
  };
}

/**
 * A gear trace. Gears are integers held between shifts, so a straight-line
 * interpolation between samples would draw ramps the car never did — the
 * stepped path is the only honest one. `stepped: true` is read by
 * UPlotChart.svelte, which owns the uPlot import and attaches its path builder.
 */
export function stepSeries(label: string, color: string): Record<string, unknown> {
  return { ...lineSeries(label, color), stepped: true };
}

/**
 * A filled trace: a 2px line with a translucent wash beneath it. Used for the
 * pedal traces and the delta, where the area between the curve and the datum is
 * the quantity being read.
 */
export function areaSeries(
  label: string,
  color: string,
  extra: Record<string, unknown> = {}
): Record<string, unknown> {
  return lineSeries(label, color, { fill: withAlpha(color, FILL_ALPHA), ...extra });
}

/** Shared cursor styling: a hairline, no drag-select box until the user drags. */
export const CURSOR_STYLE = {
  stroke: CHART_INK.cursor,
  width: 1,
  dash: [3, 3]
} as const;

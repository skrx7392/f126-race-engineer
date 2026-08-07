/**
 * Formatting the analysis pages need and the pit wall does not.
 *
 * Kept separate from `format.ts` so the live dashboard's formatting surface
 * stays exactly as it was. The two rules from there still apply and are the
 * reason these exist at all: never render a partial or placeholder number, and
 * never let the number of digits change between rows.
 */

import { DASH } from './format';

/**
 * A session's date. Epoch seconds in, a short local date out.
 *
 * The session browser is a list of things that happened, and the useful
 * distinction between rows is the day, not the second. The time is included
 * because two sessions on one evening are the common case.
 */
export function formatSessionDate(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return DASH;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

/** Distance along the lap: `1284.5` -> `"1285 m"`. */
export function formatMetres(m: number | null | undefined, digits = 0): string {
  if (m == null || !Number.isFinite(m)) return DASH;
  return `${m.toFixed(digits)} m`;
}

/**
 * A signed distance, for brake-point deltas.
 *
 * The sign convention is stated wherever this is used, because "later" and
 * "earlier" are not obvious from a number: positive means braking further down
 * the road than the reference.
 */
export function formatMetresDelta(m: number | null | undefined, digits = 0): string {
  if (m == null || !Number.isFinite(m)) return DASH;
  const sign = m > 0 ? '+' : m < 0 ? '−' : '±';
  return `${sign}${Math.abs(m).toFixed(digits)}`;
}

/** Speed with a fixed decimal, or an em dash. */
export function formatSpeed(kmh: number | null | undefined): string {
  if (kmh == null || !Number.isFinite(kmh)) return DASH;
  return kmh.toFixed(1);
}

/** Signed speed difference against a reference. */
export function formatSpeedDelta(kmh: number | null | undefined): string {
  if (kmh == null || !Number.isFinite(kmh)) return DASH;
  const sign = kmh > 0 ? '+' : kmh < 0 ? '−' : '±';
  return `${sign}${Math.abs(kmh).toFixed(1)}`;
}

/**
 * Degradation as it is spoken: `78.4` -> `"0.078 s/lap"`.
 *
 * Milliseconds per lap is the contract's unit and the wrong one to read: nobody
 * says "seventy-eight milliseconds a lap", they say "about eight hundredths".
 */
export function formatDegradation(msPerLap: number | null | undefined): string {
  if (msPerLap == null || !Number.isFinite(msPerLap)) return DASH;
  const sign = msPerLap > 0 ? '+' : msPerLap < 0 ? '−' : '±';
  return `${sign}${(Math.abs(msPerLap) / 1000).toFixed(3)}`;
}

/** `0.913` -> `"0.91"`. Two decimals is all an r² is worth. */
export function formatR2(r2: number | null | undefined): string {
  if (r2 == null || !Number.isFinite(r2)) return DASH;
  return r2.toFixed(2);
}

/** Percentage with one decimal, for tyre wear. */
export function formatPercent(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return DASH;
  return `${pct.toFixed(1)}%`;
}

/**
 * How well a fit is supported, as a word.
 *
 * An r² is a statistic most people cannot calibrate on sight, and a degradation
 * slope read off a bad fit is worse than no number at all — so the chip says
 * whether to believe it, and the number sits beside the word rather than alone.
 */
export function fitConfidence(r2: number | null | undefined): 'strong' | 'fair' | 'weak' {
  if (r2 == null || !Number.isFinite(r2)) return 'weak';
  if (r2 >= 0.8) return 'strong';
  if (r2 >= 0.5) return 'fair';
  return 'weak';
}

// ── stint lap ranges ─────────────────────────────────────────────────────────

/** One stint's lap range as it is displayed. */
export interface StintRange {
  /** First lap attributed to the stint. */
  from: number;
  /** Last lap attributed to the stint. Never earlier than `from`. */
  to: number;
  /** Laps the displayed range covers: `to - from + 1`. */
  laps: number;
}

/** The bounds every stint shape carries, whichever endpoint it came from. */
interface StintBounds {
  lap_start: number | null;
  lap_end: number | null;
}

/**
 * Stint lap ranges for display, with the pit lap counted exactly once.
 *
 * A pit stop happens *during* a lap: that lap is the in-lap for the set coming
 * off and the out-lap for the set going on, so the recorder honestly stores it
 * as both the end of one stint and the start of the next. Rendered literally
 * the strip reads "Soft 1–6 · Medium 6–12": lap 6 appears twice, the widths
 * overlap, and the stint lengths add up to one more than the laps actually run.
 *
 * The convention adopted here is that **the pit lap belongs to the stint it
 * started on** — you drove that lap on the old tyres and only crossed the line
 * on the new ones. So a stint that begins on the lap the previous one ended is
 * displayed from `previous.lap_end + 1`, giving contiguous, non-overlapping
 * ranges whose lengths sum to the race distance.
 *
 * This is display normalisation only. The stored and returned data keeps the
 * shared boundary, and the degradation fits still work from the real
 * `lap_start` — moving a model's origin to make a label tidier would change the
 * numbers the model reports.
 *
 * @param stints  Stints in order, as the API returned them.
 * @param lastLap Fallback for an open-ended final stint (`lap_end` null).
 */
export function displayStintRanges(
  stints: readonly StintBounds[],
  lastLap?: number | null
): StintRange[] {
  const ranges: StintRange[] = [];
  let previousEnd: number | null = null;

  for (const stint of stints) {
    const rawFrom = stint.lap_start ?? (previousEnd === null ? 1 : previousEnd + 1);
    const rawTo = stint.lap_end ?? lastLap ?? rawFrom;
    // `<=` rather than `===`: a stop that straddles more than one lap boundary
    // should still land after the previous stint, not inside it.
    const from = previousEnd !== null && rawFrom <= previousEnd ? previousEnd + 1 : rawFrom;
    const to = Math.max(from, rawTo);
    ranges.push({ from, to, laps: to - from + 1 });
    previousEnd = to;
  }

  return ranges;
}

// ── strategy ─────────────────────────────────────────────────────────────────

/**
 * How much of the game's on-screen wear gauge one telemetry percent is worth.
 *
 * The UDP stream and the in-car display do not agree: a set reading 28 % in the
 * telemetry shows roughly 56 % on the OSD. The ratio is **uncalibrated** — it is
 * an eyeball comparison across two races, not a measurement — so it is used for
 * one thing only, annotating a number the driver will also see in the car, and
 * never inside a calculation. Every model on the strategy page works in
 * telemetry percent.
 */
export const IN_GAME_WEAR_FACTOR = 2;

/** `28` -> `"≈56% OSD"`. Always rendered as an approximation, beside the real figure. */
export function inGameWear(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return DASH;
  return `≈${Math.round(pct * IN_GAME_WEAR_FACTOR)}%`;
}

/**
 * A projected race time: `1253110` -> `"20:53"`.
 *
 * Seconds and no finer. The number is a model output over dozens of laps, and
 * rendering it to the millisecond would claim a precision the fit does not have.
 */
export function formatMinutesSeconds(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return DASH;
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/** Signed seconds against a reference plan: `1580` -> `"+1.6"`. */
export function formatSecondsDelta(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return DASH;
  const seconds = ms / 1000;
  const sign = seconds > 0 ? '+' : seconds < 0 ? '−' : '±';
  return `${sign}${Math.abs(seconds).toFixed(1)}`;
}

// ── career ───────────────────────────────────────────────────────────────────

/**
 * An unsigned spread in seconds: `410` -> `"0.41"`.
 *
 * For IQRs and other magnitudes that are not deltas — a spread has no sign, so
 * borrowing `formatSecondsDelta` would print a `+` that reads as "worse than
 * something", which a spread is not.
 */
export function formatSeconds(ms: number | null | undefined, digits = 2): string {
  if (ms == null || !Number.isFinite(ms)) return DASH;
  return (ms / 1000).toFixed(digits);
}

/**
 * A coefficient of variation: `0.38` -> `"0.38%"`.
 *
 * Two decimals where `formatPercent` keeps one: a race CV lives well under one
 * percent, and rounding 0.38 to 0.4 throws away the digit being compared.
 */
export function formatCvPercent(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return DASH;
  return `${pct.toFixed(2)}%`;
}

/** A classified position: `4` -> `"P4"`, unknown -> em dash, never "P0". */
export function formatPosition(position: number | null | undefined): string {
  if (position == null || !Number.isFinite(position) || position < 1) return DASH;
  return `P${Math.round(position)}`;
}

/**
 * `1` + `2` -> `"S1 R2"` — season and round, the way the track page's evolution
 * chart labels its visits. Deliberately parallel to `lapLabel`'s "S3 L12", with
 * R for round marking the different meaning of the S.
 */
export function visitLabel(season: number, round: number): string {
  return `S${season} R${round}`;
}

/** Corner kind as the table renders it. */
export const CORNER_KIND_LABEL: Record<string, string> = {
  slow: 'Slow',
  medium: 'Medium',
  fast: 'Fast'
};

/** A car's display name, falling back to its index. */
export function carLabel(name: string | null | undefined, carIndex: number): string {
  const trimmed = (name ?? '').trim();
  return trimmed.length > 0 ? trimmed : `Car ${carIndex}`;
}

/** `3` + `12` -> `"S3 L12"`, the compact way a lap is referred to across pages. */
export function lapLabel(sessionId: number, lap: number): string {
  return `S${sessionId} L${lap}`;
}

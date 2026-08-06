/**
 * Number and time formatting for a screen read at ~1.5 m in ~300 ms.
 *
 * Two rules run through all of it:
 *  - Never render a partial or placeholder number. Unknown is an em dash, so a
 *    missing value can never be mistaken for a real one.
 *  - Fixed decimal places, always. Digits that appear and disappear make a
 *    column jump, and a jumping column costs a glance.
 */

export const DASH = '—';

const pad2 = (n: number): string => (n < 10 ? `0${n}` : String(n));

/** `92345` -> `"1:32.345"`. Lap and sector times on the clock. */
export function formatLapTime(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return DASH;
  const total = Math.round(ms);
  const minutes = Math.floor(total / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  const millis = total % 1000;
  const frac = String(millis).padStart(3, '0');
  return minutes > 0 ? `${minutes}:${pad2(seconds)}.${frac}` : `${seconds}.${frac}`;
}

/** `21345` -> `"21.345"`. Sector times never reach a minute. */
export function formatSectorTime(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return DASH;
  return (ms / 1000).toFixed(3);
}

/** Interval to another car: `1234` -> `"+1.234"`. */
export function formatGap(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return DASH;
  if (ms === 0) return '—';
  return `+${(ms / 1000).toFixed(3)}`;
}

/**
 * Signed delta against a reference lap: `-142` -> `"-0.142"`.
 * The sign is always present — it is the single most important glyph on screen.
 */
export function formatDelta(ms: number | null | undefined, digits = 3): string {
  if (ms == null || !Number.isFinite(ms)) return DASH;
  const seconds = ms / 1000;
  const sign = seconds > 0 ? '+' : seconds < 0 ? '−' : '±';
  return `${sign}${Math.abs(seconds).toFixed(digits)}`;
}

/** Session clock: `1740` -> `"29:00"`, `3725` -> `"1:02:05"`. */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return DASH;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}

/** Fixed-precision number, or an em dash. */
export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

/** Signed fixed-precision number, e.g. fuel laps in hand. */
export function formatSigned(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value > 0 ? '+' : value < 0 ? '−' : '±';
  return `${sign}${Math.abs(value).toFixed(digits)}`;
}

/** `-1` -> `"R"`, `0` -> `"N"`, otherwise the gear number. */
export function formatGear(gear: number | null | undefined): string {
  if (gear == null || !Number.isFinite(gear)) return DASH;
  if (gear < 0) return 'R';
  if (gear === 0) return 'N';
  return String(Math.round(gear));
}

/** Joules to a compact megajoule reading. */
export function formatMegajoules(joules: number | null | undefined): string {
  if (joules == null || !Number.isFinite(joules)) return DASH;
  return (joules / 1_000_000).toFixed(2);
}

const KIND_LABELS: Record<string, string> = {
  practice: 'Practice',
  quali: 'Qualifying',
  race: 'Race',
  time_trial: 'Time trial',
  other: 'Session'
};

export function sessionKindLabel(kind: string | null | undefined): string {
  if (!kind) return 'Session';
  return KIND_LABELS[kind] ?? 'Session';
}

const DELTA_KIND_LABELS: Record<string, string> = {
  session_best: 'vs session best',
  personal_best: 'vs personal best',
  race_best: 'vs race best'
};

export function deltaKindLabel(kind: string | null | undefined): string {
  if (!kind) return 'no reference';
  return DELTA_KIND_LABELS[kind] ?? 'vs reference';
}

/** Clamp helper used by every bar in the UI. */
export function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** Map a value onto `[0, 1]` across `[min, max]`, clamped. */
export function normalize(value: number, min: number, max: number): number {
  if (max === min) return 0;
  return clamp01((value - min) / (max - min));
}

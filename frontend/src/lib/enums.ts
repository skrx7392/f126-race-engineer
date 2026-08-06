/**
 * Raw F1-game enums translated into the words and colours a driver already
 * knows. The protocol passes these through untouched (docs/ws-protocol.md notes
 * `session_type`, `fia_flag`, `compound_visual` and friends as "raw enum").
 *
 * Every colour here is redundant with a label or glyph somewhere in the UI: the
 * compound pill carries its letter, the flag banner carries its word, the
 * constructor chip sits beside the driver's name. Colour is never the only
 * channel carrying identity.
 */

// ── tyre compounds ───────────────────────────────────────────────────────────

export interface Compound {
  /** Single-letter code shown inside the pill. */
  code: string;
  label: string;
  color: string;
  /** Ink colour that meets contrast on `color`. */
  ink: string;
}

const SOFT: Compound = { code: 'S', label: 'Soft', color: '#DA291C', ink: '#FFFFFF' };
const MEDIUM: Compound = { code: 'M', label: 'Medium', color: '#FFD12E', ink: '#14150F' };
const HARD: Compound = { code: 'H', label: 'Hard', color: '#EDEDE6', ink: '#14150F' };
const INTER: Compound = { code: 'I', label: 'Intermediate', color: '#3FB618', ink: '#0B1004' };
const WET: Compound = { code: 'W', label: 'Wet', color: '#1E7FD4', ink: '#FFFFFF' };
const UNKNOWN_COMPOUND: Compound = { code: '?', label: 'Unknown', color: '#4A515C', ink: '#F2F4F7' };

/** `compound_visual` — what the broadcast graphic shows. */
const VISUAL_COMPOUNDS: Record<number, Compound> = {
  7: INTER,
  8: WET,
  15: WET, // F2 wet
  16: SOFT,
  17: MEDIUM,
  18: HARD,
  19: SOFT, // F2 super soft
  20: SOFT,
  21: MEDIUM,
  22: HARD
};

/** `compound_actual` — the specific dry construction (C0..C6). */
const ACTUAL_COMPOUND_NAMES: Record<number, string> = {
  7: 'INT',
  8: 'WET',
  16: 'C5',
  17: 'C4',
  18: 'C3',
  19: 'C2',
  20: 'C1',
  21: 'C0',
  22: 'C6'
};

export function compoundOf(visual: number | null | undefined): Compound {
  if (visual == null) return UNKNOWN_COMPOUND;
  return VISUAL_COMPOUNDS[visual] ?? UNKNOWN_COMPOUND;
}

export function actualCompoundName(actual: number | null | undefined): string | null {
  if (actual == null) return null;
  return ACTUAL_COMPOUND_NAMES[actual] ?? null;
}

// ── constructors ─────────────────────────────────────────────────────────────

interface Team {
  name: string;
  color: string;
}

/**
 * Real constructor colours. These are brand values, not a designed categorical
 * ramp, and they do not clear a colourblind-separation check (Haas grey against
 * Mercedes teal is the worst pair). They are used only as a 3px chip next to the
 * driver's name, which is what actually identifies the row — the chip is a
 * redundant accent, never the sole encoding.
 */
const TEAMS: Record<number, Team> = {
  0: { name: 'Mercedes', color: '#00D7B6' },
  1: { name: 'Ferrari', color: '#E8002D' },
  2: { name: 'Red Bull', color: '#3671C6' },
  3: { name: 'Williams', color: '#64C4FF' },
  4: { name: 'Aston Martin', color: '#229971' },
  5: { name: 'Alpine', color: '#FF87BC' },
  6: { name: 'RB', color: '#6692FF' },
  7: { name: 'Haas', color: '#B6BABD' },
  8: { name: 'McLaren', color: '#FF8000' },
  9: { name: 'Sauber', color: '#52E252' }
};

const NEUTRAL_TEAM: Team = { name: 'Unknown', color: '#5A626E' };

export function teamColor(teamId: number | null | undefined): string {
  if (teamId == null) return NEUTRAL_TEAM.color;
  return (TEAMS[teamId] ?? NEUTRAL_TEAM).color;
}

export function teamName(teamId: number | null | undefined): string {
  if (teamId == null) return NEUTRAL_TEAM.name;
  return (TEAMS[teamId] ?? NEUTRAL_TEAM).name;
}

// ── flags & safety car ───────────────────────────────────────────────────────

export type FlagState = 'none' | 'green' | 'blue' | 'yellow' | 'red' | 'unknown';

export function flagState(fiaFlag: number | null | undefined): FlagState {
  switch (fiaFlag) {
    case 1:
      return 'green';
    case 2:
      return 'blue';
    case 3:
      return 'yellow';
    case 4:
      return 'red';
    case 0:
      return 'none';
    default:
      return 'unknown';
  }
}

export type SafetyCarState = 'none' | 'full' | 'virtual' | 'formation';

export function safetyCarState(sc: number | null | undefined): SafetyCarState {
  switch (sc) {
    case 1:
      return 'full';
    case 2:
      return 'virtual';
    case 3:
      return 'formation';
    default:
      return 'none';
  }
}

export const SAFETY_CAR_LABEL: Record<SafetyCarState, string> = {
  none: '',
  full: 'Safety car',
  virtual: 'Virtual safety car',
  formation: 'Formation lap'
};

// ── weather ──────────────────────────────────────────────────────────────────

export type WeatherKind = 'clear' | 'light-cloud' | 'overcast' | 'light-rain' | 'heavy-rain' | 'storm';

const WEATHER: Record<number, { kind: WeatherKind; label: string; glyph: string }> = {
  0: { kind: 'clear', label: 'Clear', glyph: '○' },
  1: { kind: 'light-cloud', label: 'Light cloud', glyph: '◔' },
  2: { kind: 'overcast', label: 'Overcast', glyph: '◕' },
  3: { kind: 'light-rain', label: 'Light rain', glyph: '◍' },
  4: { kind: 'heavy-rain', label: 'Heavy rain', glyph: '◉' },
  5: { kind: 'storm', label: 'Storm', glyph: '◈' }
};

const UNKNOWN_WEATHER = { kind: 'clear' as WeatherKind, label: 'Unknown', glyph: '–' };

export function weatherOf(w: number | null | undefined) {
  if (w == null) return UNKNOWN_WEATHER;
  return WEATHER[w] ?? UNKNOWN_WEATHER;
}

/** True once the track is wet enough to change the tyre call. */
export function isWet(w: number | null | undefined): boolean {
  return w != null && w >= 3;
}

// ── car state ────────────────────────────────────────────────────────────────

export type PitState = 'none' | 'pitting' | 'in-pit';

export function pitState(status: number | null | undefined): PitState {
  switch (status) {
    case 1:
      return 'pitting';
    case 2:
      return 'in-pit';
    default:
      return 'none';
  }
}

export const PIT_LABEL: Record<PitState, string> = {
  none: '',
  pitting: 'PIT',
  'in-pit': 'BOX'
};

/** Result statuses that mean the car is no longer running. */
const OUT_STATUSES = new Set([4, 5, 6, 7]);

export function isOut(resultStatus: number | null | undefined): boolean {
  return resultStatus != null && OUT_STATUSES.has(resultStatus);
}

export function resultLabel(resultStatus: number | null | undefined): string {
  switch (resultStatus) {
    case 3:
      return 'FIN';
    case 4:
      return 'DNF';
    case 5:
      return 'DSQ';
    case 6:
      return 'NC';
    case 7:
      return 'RET';
    default:
      return '';
  }
}

// ── energy deployment ────────────────────────────────────────────────────────

const DEPLOY_MODES: Record<number, string> = {
  0: 'None',
  1: 'Medium',
  2: 'Hotlap',
  3: 'Overtake',
  4: 'Push'
};

export function deployModeLabel(mode: number | null | undefined): string {
  if (mode == null) return '—';
  return DEPLOY_MODES[mode] ?? `Mode ${mode}`;
}

// ── tyre thermal window ──────────────────────────────────────────────────────

/** Surface-temperature working range, in °C. */
export const TYRE_WINDOW_MIN = 85;
export const TYRE_WINDOW_MAX = 105;

export type ThermalState = 'cold' | 'working' | 'hot';

export function thermalState(tempC: number | null | undefined): ThermalState {
  if (tempC == null) return 'working';
  if (tempC < TYRE_WINDOW_MIN) return 'cold';
  if (tempC > TYRE_WINDOW_MAX) return 'hot';
  return 'working';
}

/** Wheel-array order from the protocol: `[rl, rr, fl, fr]`. */
export const WHEEL_ORDER = ['RL', 'RR', 'FL', 'FR'] as const;
/** Indices laid out as the car is drawn: fronts on top. */
export const WHEEL_LAYOUT = [2, 3, 0, 1] as const;

/**
 * Synthetic archive, for building the analysis pages without a database.
 *
 * This follows the same rule as `mock.ts`: it is not a folder of fixture JSON,
 * it is a small simulation that produces the shapes the backend produces, and
 * it is injected at the *transport* boundary rather than inside the pages. The
 * pages call `fetch('/api/…')` exactly as they will in production; mock mode
 * only changes who answers. So a page that works here works against the real
 * backend, and there is no second data path to keep in sync.
 *
 * What it models, because each drives something visible:
 *   - a Bahrain lap-distance speed profile with 15 named corners, so the corner
 *     segmentation has something real to find;
 *   - 20 laps across two stints on two compounds, with tyre degradation, so the
 *     stint fits have a slope worth plotting and an r² worth trusting;
 *   - per-lap mistakes localised to individual corners, so the corner table has
 *     an actual culprit to point at rather than uniform noise;
 *   - in/out laps and one 107% lap, so the stint fit has laps to exclude and the
 *     exclusion reasons have something to say;
 *   - a second session at the same track, so cross-session comparison is
 *     reachable, and a third at another track, so the 422 track-mismatch path is
 *     reachable too.
 *
 * Times are derived by integrating the speed profile rather than being made up
 * independently of it, so the lap times, the telemetry, the cumulative delta and
 * the corner time losses are all consistent with one another. That consistency
 * is the point: it is what makes the delta trace cross zero in the right places.
 */

import type {
  CareerConsistency,
  CareerOverview,
  CareerPb,
  CareerPbSectors,
  CareerQualiResult,
  CareerRaceResult,
  CareerSeason,
  CareerSprintResult,
  CareerTag,
  CareerTotals,
  CareerTrackResponse,
  CareerTrackSession,
  CareerVisit,
  CareerWeekend,
  CompareResponse,
  CompareSide,
  Corner,
  CornerKind,
  CornersResponse,
  Debrief,
  Lap,
  LapTelemetry,
  Participant,
  PlansRanking,
  SessionDetail,
  SessionSummary,
  Stint,
  StintLap,
  StintsResponse,
  StrategyCalibrationStint,
  StrategyCompound,
  StrategyPlan,
  StrategyPlanStint,
  StrategyResponse,
  StrategySessionUsed,
  TyreStintRow
} from './analysis-api';

// ── deterministic randomness ─────────────────────────────────────────────────

/** mulberry32, as in mock.ts — small, reproducible, good enough for fixtures. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A stable pseudo-random draw for a (lap, salt) pair, independent of order. */
function jitter(lap: number, salt: number): number {
  return mulberry32(lap * 7919 + salt * 104729)();
}

// ── track model ──────────────────────────────────────────────────────────────

export const BAHRAIN_TRACK_ID = 3;
export const BAHRAIN_LENGTH_M = 5412;
const TOP_SPEED_KMH = 325;

/** Corner apexes as a fraction of lap distance, with minimum speed and width. */
interface CornerSpec {
  n: number;
  at: number;
  v: number;
  /** Half-width of the corner's speed well, as a fraction of the lap. */
  w: number;
}

/**
 * 15 corners, in the order they are driven.
 *
 * The windows `[at - w, at + w]` are deliberately disjoint and ordered. Real
 * corner segmentation can produce touching or nested windows, but a fixture
 * that does is indistinguishable from a segmentation bug when you are looking
 * at the corner table — so this one keeps a gap between every pair, and a test
 * holds it to that.
 */
const CORNERS: readonly CornerSpec[] = [
  { n: 1, at: 0.112, v: 122, w: 0.026 },
  { n: 2, at: 0.168, v: 246, w: 0.017 },
  { n: 3, at: 0.212, v: 181, w: 0.019 },
  { n: 4, at: 0.281, v: 228, w: 0.018 },
  { n: 5, at: 0.33, v: 118, w: 0.024 },
  { n: 6, at: 0.383, v: 152, w: 0.02 },
  { n: 7, at: 0.43, v: 232, w: 0.017 },
  { n: 8, at: 0.478, v: 116, w: 0.024 },
  { n: 9, at: 0.524, v: 163, w: 0.018 },
  { n: 10, at: 0.585, v: 93, w: 0.028 },
  { n: 11, at: 0.655, v: 205, w: 0.019 },
  { n: 12, at: 0.716, v: 124, w: 0.024 },
  { n: 13, at: 0.784, v: 231, w: 0.017 },
  { n: 14, at: 0.85, v: 188, w: 0.02 },
  { n: 15, at: 0.925, v: 141, w: 0.025 }
];

/** Corner classification, matching the contract's `kind` field. */
function kindOf(minSpeed: number): CornerKind {
  if (minSpeed < 140) return 'slow';
  if (minSpeed < 210) return 'medium';
  return 'fast';
}

const smoothstep = (x: number): number => {
  const t = x < 0 ? 0 : x > 1 ? 1 : x;
  return t * t * (3 - 2 * t);
};

/** Shortest distance between two lap fractions, accounting for the wrap. */
function wrapDistance(a: number): number {
  const d = Math.abs(a) % 1;
  return d > 0.5 ? 1 - d : d;
}

/**
 * How a lap differs from the canonical one.
 *
 * `mistakes` lowers a corner's apex speed — that is how a lap acquires a
 * localised time loss the corner table can attribute to a specific turn.
 * `shifts` moves the whole speed well a few metres up or down the road, which
 * is what makes the braking-point column carry real numbers instead of zeroes:
 * braking later moves the well later, and the detected brake point with it.
 */
interface LapStyle {
  mistakes: ReadonlyMap<number, number>;
  shifts: ReadonlyMap<number, number>;
}

const NEUTRAL_STYLE: LapStyle = { mistakes: new Map(), shifts: new Map() };

/**
 * Speed (km/h) at a lap fraction.
 *
 * `wellScale` widens or narrows every corner's speed well without touching its
 * apex speed or the straight-line maximum — it is the braking and acceleration
 * zone around each corner, and it is the one free parameter used to make the
 * lap come out at a believable time (see `speedWellScale`). The wells are
 * allowed to overlap: a real braking zone often starts before the previous
 * corner's exit is done, and `Math.min` picks the binding constraint. The
 * *reported* corner windows are the narrow `c.w` bands and stay disjoint.
 */
function rawSpeedAt(frac: number, wellScale: number, style: LapStyle = NEUTRAL_STYLE): number {
  let v = TOP_SPEED_KMH;
  for (const c of CORNERS) {
    const half = c.w * wellScale;
    const d = wrapDistance(frac - c.at - (style.shifts.get(c.n) ?? 0));
    if (d < half) {
      const penalty = style.mistakes.get(c.n) ?? 0;
      const apex = Math.max(55, c.v - penalty);
      v = Math.min(v, apex + (TOP_SPEED_KMH - apex) * smoothstep(d / half));
    }
  }
  return v;
}

const GEAR_THRESHOLDS = [0, 88, 128, 168, 208, 250, 292, 314];

function gearFor(speedKmh: number): number {
  let g = 1;
  for (let i = 0; i < GEAR_THRESHOLDS.length; i++) {
    const t = GEAR_THRESHOLDS[i];
    if (t !== undefined && speedKmh >= t) g = i + 1;
  }
  return Math.min(8, g);
}

function rpmFor(speedKmh: number, gear: number): number {
  const lo = GEAR_THRESHOLDS[gear - 1] ?? 0;
  const hi = GEAR_THRESHOLDS[gear] ?? TOP_SPEED_KMH;
  const span = Math.max(1, hi - lo);
  const within = Math.min(1, Math.max(0, (speedKmh - lo) / span));
  return Math.round(7_000 + within * 5_600);
}

// ── per-lap model ────────────────────────────────────────────────────────────

/** Number of laps in the headline session. */
const LAP_COUNT = 20;
/** Where the single pit stop falls: lap 11 is the in-lap, 12 the out-lap. */
const PIT_IN_LAP = 11;
const PIT_OUT_LAP = 12;
/**
 * The scripted mistake: braking too late into turn 10 and running wide.
 *
 * Placed in the longer first stint deliberately. It is a genuine outlier that
 * the 107% rule does not catch, so it stays in the fit — which is the honest
 * case, and the one worth being able to see: the stints page shows a point off
 * the line, and the corners page names the corner that put it there.
 */
const MISTAKE_LAP = 7;
/** A lap ruined by traffic — above 107% of the stint median, so excluded. */
const TRAFFIC_LAP = 17;
/** Laps deleted for track limits. */
const INVALID_LAPS: ReadonlySet<number> = new Set([4, 16]);

/**
 * How a given lap was driven: where it lost speed, and where it braked.
 *
 * Braking points move by a handful of metres lap to lap, which is both true of
 * a real driver and the reason the corner table's brake-point delta column has
 * anything in it. The scripted lock-up on lap 14 is a late brake into turn 10
 * followed by a slow apex — the shift and the penalty together, because that is
 * what actually happens when you overcook an entry.
 */
function styleFor(lap: number): LapStyle {
  const mistakes = new Map<number, number>();
  const shifts = new Map<number, number>();

  if (lap === MISTAKE_LAP) {
    mistakes.set(10, 10);
    // ~27 m late — comfortably outside the lap-to-lap braking scatter below, so
    // the brake-point delta reads as a mistake rather than as noise.
    shifts.set(10, 0.005);
  }
  // Everyone is scruffier on cold tyres out of the pits.
  if (lap === PIT_OUT_LAP) {
    mistakes.set(1, 14);
    mistakes.set(5, 11);
  }

  for (const c of CORNERS) {
    // A few metres of braking-point scatter on every corner, every lap.
    shifts.set(c.n, (shifts.get(c.n) ?? 0) + (jitter(lap, c.n + 500) - 0.5) * 0.003);
    // And an occasional untidy corner, so no two laps are identical.
    // Kept small on purpose: per-corner scruffiness must stay well under the
    // degradation slope, or the stint fit is fitting noise.
    const r = jitter(lap, c.n);
    if (r > 0.82) mistakes.set(c.n, (mistakes.get(c.n) ?? 0) + 1 + r * 2.5);
  }
  return { mistakes, shifts };
}

/** Stint membership: 1 = laps 1..11 (medium), 2 = laps 12..20 (hard). */
function stintOf(lap: number): 1 | 2 {
  return lap <= PIT_IN_LAP ? 1 : 2;
}

/** Tyre age in laps at the start of a lap. */
function tyreAge(lap: number): number {
  return stintOf(lap) === 1 ? lap - 1 : lap - PIT_OUT_LAP;
}

/**
 * The pace scale for a lap: how much slower than the reference profile it is.
 *
 * Fuel burns off (the car gets quicker), tyres degrade (it gets slower), and
 * the second stint starts on a harder, slower compound with less degradation.
 * The two effects nearly cancel in stint one, which is what a real race trace
 * looks like and what makes the stint fit worth showing.
 */
function paceScale(lap: number): number {
  const age = tyreAge(lap);
  const stint = stintOf(lap);
  // Degradation outruns the fuel burn on both compounds, so both stints slope
  // upward — a fit whose slope is buried in noise teaches nothing about how to
  // read a fit, which is the whole job of the stints page.
  const degPerLap = stint === 1 ? 0.0018 : 0.0013;
  const compoundOffset = stint === 1 ? 0 : 0.0042;
  const fuelGain = lap * 0.0003;
  let scale = 1 + compoundOffset + degPerLap * age - fuelGain;

  if (lap === PIT_IN_LAP) scale += 0.19; // in-lap: pit entry
  if (lap === PIT_OUT_LAP) scale += 0.21; // out-lap: pit exit and cold tyres
  if (lap === TRAFFIC_LAP) scale += 0.095; // stuck behind a backmarker
  if (lap === 1) scale += 0.028; // opening lap, still warming up

  // Lap-to-lap scatter, kept well under the degradation slope so the fit has a
  // signal to find (r² lands around 0.9 rather than 0.3).
  scale += (jitter(lap, 991) - 0.5) * 0.0009;
  return scale;
}

/**
 * Pedal positions inferred from a speed series.
 *
 * Derived from the gradient of the *output* series rather than from the
 * underlying 4 m integration: the two grids do not divide evenly into one
 * another, so sampling the finer one at irregular offsets aliases, and the
 * brake trace comes out as a band of hash instead of a curve. Differencing the
 * series that will actually be drawn cannot alias against itself.
 *
 * The physics is the honest part: if the car is slowing it is braking, if it is
 * accelerating it is on the throttle, and how hard follows from how quickly.
 */
function pedalsFromSpeed(speed: number[]): { throttle: number[]; brake: number[] } {
  const n = speed.length;
  const throttle: number[] = new Array(n);
  const brake: number[] = new Array(n);
  // Look two samples either side: enough to smooth the step, short enough to
  // keep the braking point where it actually is.
  const w = 2;
  for (let i = 0; i < n; i++) {
    const back = speed[Math.max(0, i - w)] ?? 0;
    const fwd = speed[Math.min(n - 1, i + w)] ?? 0;
    const dv = fwd - back;
    const b = dv < -0.5 ? Math.min(1, -dv / 22) : 0;
    throttle[i] = Math.round((dv > 0.4 ? Math.min(1, 0.55 + dv / 16) : b > 0.1 ? 0 : 0.45) * 100) / 100;
    brake[i] = Math.round(b * 100) / 100;
  }
  return { throttle, brake };
}

/** Integration step for turning a speed profile into times. */
const STEP_M = 4;

/**
 * What a clean reference lap should come out at. Bahrain race pace is around
 * 1:31.5, and the fifteen corner speeds above were chosen for shape rather than
 * to add up to that.
 *
 * Rather than hand-tuning fifteen numbers until the total lands — or scaling
 * every speed, which would drag the 325 km/h straight-line maximum down with it
 * — the one free parameter is how long the braking and acceleration zones are.
 * That is solved for numerically at first use. Apex speeds and top speed stay
 * exactly as specified; change a corner and the lap time stays believable by
 * itself.
 */
const TARGET_BASE_LAP_MS = 91_500;

/** Integrate a whole lap at a given well width. Monotonic: wider is slower. */
function lapSecondsAt(wellScale: number): number {
  let t = 0;
  for (let s = 0; s <= BAHRAIN_LENGTH_M; s += STEP_M) {
    t += STEP_M / (rawSpeedAt(s / BAHRAIN_LENGTH_M, wellScale) / 3.6);
  }
  return t;
}

let wellScaleCache: number | null = null;

/** The braking-zone width that puts a clean lap on `TARGET_BASE_LAP_MS`. */
function speedWellScale(): number {
  if (wellScaleCache === null) {
    let lo = 0.4;
    let hi = 8;
    for (let i = 0; i < 44; i++) {
      const mid = (lo + hi) / 2;
      if (lapSecondsAt(mid) * 1000 < TARGET_BASE_LAP_MS) lo = mid;
      else hi = mid;
    }
    wellScaleCache = (lo + hi) / 2;
  }
  return wellScaleCache;
}

/** Speed at a lap fraction on the calibrated profile. */
function baseSpeedAt(frac: number, style: LapStyle = NEUTRAL_STYLE): number {
  return rawSpeedAt(frac, speedWellScale(), style);
}

/**
 * Integrate one lap: cumulative time at every `STEP_M` metres.
 *
 * Returns time in seconds at each sample, plus the sample distances. Everything
 * downstream — lap time, telemetry, delta, corner losses — is read off this, so
 * they cannot disagree.
 */
function integrateLap(lap: number): { dist: number[]; time: number[]; speed: number[] } {
  const style = styleFor(lap);
  const scale = paceScale(lap);
  const dist: number[] = [];
  const time: number[] = [];
  const speed: number[] = [];
  let t = 0;
  for (let s = 0; s <= BAHRAIN_LENGTH_M; s += STEP_M) {
    const frac = s / BAHRAIN_LENGTH_M;
    const v = baseSpeedAt(frac, style) / scale;
    dist.push(s);
    speed.push(v);
    time.push(t);
    // v is km/h; convert to m/s for the time increment.
    t += STEP_M / (v / 3.6);
  }
  return { dist, time, speed };
}

const lapCache = new Map<number, { dist: number[]; time: number[]; speed: number[] }>();

function lapTrace(lap: number): { dist: number[]; time: number[]; speed: number[] } {
  let cached = lapCache.get(lap);
  if (!cached) {
    cached = integrateLap(lap);
    lapCache.set(lap, cached);
  }
  return cached;
}

export function lapTimeMs(lap: number): number {
  const { time } = lapTrace(lap);
  return Math.round((time[time.length - 1] ?? 0) * 1000);
}

/** Sector boundaries as fractions of lap distance. */
const SECTOR_SPLITS = [0.333, 0.668];

function sectorsFor(lap: number): [number, number, number] {
  const { dist, time } = lapTrace(lap);
  const total = time[time.length - 1] ?? 0;
  const at = (frac: number): number => {
    const target = frac * BAHRAIN_LENGTH_M;
    for (let i = 0; i < dist.length; i++) {
      if ((dist[i] ?? 0) >= target) return time[i] ?? 0;
    }
    return total;
  };
  const s1 = at(SECTOR_SPLITS[0] ?? 0.333);
  const s2 = at(SECTOR_SPLITS[1] ?? 0.668);
  return [
    Math.round(s1 * 1000),
    Math.round((s2 - s1) * 1000),
    Math.round((total - s2) * 1000)
  ];
}

// ── sessions ─────────────────────────────────────────────────────────────────

/** The player's car index, matching the contract's worked examples. */
export const PLAYER_CAR_INDEX = 21;
/** A team-mate, so the car picker has a second entry that is not the player. */
const TEAMMATE_CAR_INDEX = 20;

export const HEADLINE_SESSION_ID = 3;
const QUALI_SESSION_ID = 2;
const OTHER_TRACK_SESSION_ID = 1;

/**
 * A second strategy circuit, and the reason it exists.
 *
 * Bahrain is a modelled weekend: two compounds fitted out of the race, one out of quali, and
 * a hard that only ever did a short run and gets its slope derived from the circuit's
 * wear-to-time coefficient. Montreal is the *other* weekend — the one this recorder actually
 * sees most of the time — where every set did an out-lap, two flyers and an in-lap, so
 * nothing can be fitted, nothing can be calibrated, and the only honest plan set is a
 * feasibility one ranked by rule. Both have to be reachable in `dev:mock` or half of this
 * page can only be seen against production data.
 */
export const MONTREAL_TRACK_ID = 6;
const MONTREAL_PRACTICE_SESSION_ID = 4;
const MONTREAL_RACE_SESSION_ID = 5;
const MONTREAL_RACE_LAPS = 14;

/** Wall-clock anchor. Fixed, so the fixture is byte-identical between runs. */
const BASE_WALL = 1_771_000_000;

/**
 * The game's final-classification packet, reduced to what the career pages
 * read. Absent from a seed means the packet never landed for that session —
 * the ordinary way a recording ends early — and every derived field is null.
 */
interface RaceClassification {
  position: number;
  grid_position: number;
  points: number;
  pit_stops: number;
  status: string;
  fastest_lap: boolean;
}

interface SessionSeed {
  id: number;
  type: number;
  typeName: string;
  trackId: number;
  trackName: string;
  laps: number;
  startedOffset: number;
  durationS: number;
  totalLaps: number | null;
  /** Per-lap offset against the shared Bahrain lap model, in ms. */
  lapOffsetMs?: number;
  /** Race-type sessions only; absent = no classification was recorded. */
  classification?: RaceClassification;
  /** Quali-type sessions: the classified position (poles come from these). */
  qualiPosition?: number;
}

/**
 * A third circuit, existing purely for the career fixture.
 *
 * Miami is the sprint-format weekend (season 1, round 1) and — because it is the
 * first circuit the career revisits — the weekend that starts season 2. Its
 * second visit is also the one whose race never recorded a classification, and
 * the one carrying the pinned (and conflicting) tags, so every marked state the
 * career page can show is reachable in `dev:mock`.
 */
export const MIAMI_TRACK_ID = 30;
const MIAMI_P1_SESSION_ID = 6;
const MIAMI_SHOOTOUT_SESSION_ID = 7;
export const MIAMI_SPRINT_SESSION_ID = 8;
const MIAMI_OSQ_SESSION_ID = 9;
export const MIAMI_GP_SESSION_ID = 10;
export const MIAMI_S2_QUALI_SESSION_ID = 11;
/** The race whose classification packet never landed: every result field null. */
export const MIAMI_S2_RACE_SESSION_ID = 12;
/** Time trial: excluded from career rounds, still sets the Bahrain PB. */
export const BAHRAIN_TT_SESSION_ID = 13;

const SESSION_SEEDS: readonly SessionSeed[] = [
  {
    id: HEADLINE_SESSION_ID,
    type: 15,
    typeName: 'Race',
    trackId: BAHRAIN_TRACK_ID,
    trackName: 'Bahrain',
    laps: LAP_COUNT,
    startedOffset: 0,
    durationS: 5400,
    totalLaps: LAP_COUNT,
    // The numbers the debrief fixture narrates: P4 from P6, 12 points, one stop.
    classification: {
      position: 4,
      grid_position: 6,
      points: 12,
      pit_stops: 1,
      status: 'finished',
      fastest_lap: false
    }
  },
  {
    id: QUALI_SESSION_ID,
    type: 7,
    typeName: 'Qualifying 3',
    trackId: BAHRAIN_TRACK_ID,
    trackName: 'Bahrain',
    laps: 8,
    startedOffset: -7200,
    durationS: 720,
    totalLaps: null,
    lapOffsetMs: -1_250,
    qualiPosition: 1
  },
  {
    id: OTHER_TRACK_SESSION_ID,
    type: 2,
    typeName: 'Practice 2',
    trackId: 11,
    trackName: 'Monza',
    laps: 12,
    startedOffset: -172_800,
    durationS: 3600,
    totalLaps: null,
    lapOffsetMs: 8_400
  },
  {
    id: MONTREAL_RACE_SESSION_ID,
    type: 15,
    typeName: 'Race',
    trackId: MONTREAL_TRACK_ID,
    trackName: 'Montreal',
    laps: MONTREAL_RACE_LAPS,
    startedOffset: -601_200,
    durationS: 3600,
    totalLaps: MONTREAL_RACE_LAPS,
    classification: {
      position: 1,
      grid_position: 3,
      points: 25,
      pit_stops: 1,
      status: 'finished',
      fastest_lap: false
    }
  },
  {
    id: MONTREAL_PRACTICE_SESSION_ID,
    type: 1,
    typeName: 'Practice 1',
    trackId: MONTREAL_TRACK_ID,
    trackName: 'Montreal',
    laps: 9,
    startedOffset: -604_800,
    durationS: 3600,
    totalLaps: null
  },

  // ── the career fixture's own weekends ──────────────────────────────────────
  // Season 1, round 1: the sprint-format Miami weekend. The shootout (type 12)
  // classifies P1 on purpose — shootouts set the sprint grid, and the poles
  // count must ignore it in favour of the one-shot qualifying's P2.
  {
    id: MIAMI_P1_SESSION_ID,
    type: 1,
    typeName: 'Practice 1',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 10,
    startedOffset: -1_296_000,
    durationS: 3600,
    totalLaps: null
  },
  {
    id: MIAMI_SHOOTOUT_SESSION_ID,
    type: 12,
    typeName: 'Sprint Shootout',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 5,
    startedOffset: -1_209_600,
    durationS: 1800,
    totalLaps: null,
    lapOffsetMs: -900,
    qualiPosition: 1
  },
  {
    id: MIAMI_SPRINT_SESSION_ID,
    type: 15,
    typeName: 'Sprint',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 8,
    startedOffset: -1_206_000,
    durationS: 1800,
    totalLaps: 8,
    classification: {
      position: 1,
      grid_position: 3,
      points: 8,
      pit_stops: 0,
      status: 'finished',
      fastest_lap: false
    }
  },
  {
    id: MIAMI_OSQ_SESSION_ID,
    type: 9,
    typeName: 'One-Shot Qualifying',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 4,
    startedOffset: -1_202_400,
    durationS: 900,
    totalLaps: null,
    lapOffsetMs: -1_300,
    qualiPosition: 2
  },
  {
    id: MIAMI_GP_SESSION_ID,
    type: 16,
    typeName: 'Race 2',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 17,
    startedOffset: -1_198_800,
    durationS: 5400,
    totalLaps: 17,
    classification: {
      position: 2,
      grid_position: 2,
      points: 18,
      pit_stops: 1,
      status: 'finished',
      fastest_lap: true
    }
  },

  // Season 2, round 1: Miami again — the repeat is what increments the season.
  // The quali improves on last visit's pace (career progress the track page's
  // evolution chart can show); the race recorded no classification at all.
  {
    id: MIAMI_S2_QUALI_SESSION_ID,
    type: 7,
    typeName: 'Qualifying 3',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 6,
    startedOffset: 86_400,
    durationS: 720,
    totalLaps: null,
    lapOffsetMs: -1_500,
    qualiPosition: 1
  },
  {
    id: MIAMI_S2_RACE_SESSION_ID,
    type: 15,
    typeName: 'Race',
    trackId: MIAMI_TRACK_ID,
    trackName: 'Miami',
    laps: 17,
    startedOffset: 90_000,
    durationS: 5400,
    totalLaps: 17
    // No classification: the packet never landed. The session still appears.
  },

  // A Bahrain time trial the evening before quali: not a career round, but the
  // quickest thing ever driven there, so the PB board has to credit it.
  {
    id: BAHRAIN_TT_SESSION_ID,
    type: 18,
    typeName: 'Time Trial',
    trackId: BAHRAIN_TRACK_ID,
    trackName: 'Bahrain',
    laps: 6,
    startedOffset: -90_000,
    durationS: 1800,
    totalLaps: null,
    lapOffsetMs: -2_000
  }
];

function seedFor(id: number): SessionSeed | null {
  return SESSION_SEEDS.find((s) => s.id === id) ?? null;
}

/**
 * Every non-headline session's laps are a simple offset of the race model
 * rather than their own simulation (the seed's `lapOffsetMs`). They exist to
 * populate the session browser, the lap pickers and the career derivation, and
 * nothing plots them in anger.
 */
function lapOffsetFor(sessionId: number): number {
  return seedFor(sessionId)?.lapOffsetMs ?? 0;
}

function sessionLapTimeMs(sessionId: number, lap: number, carIndex: number): number {
  const base = lapTimeMs(((lap - 1) % LAP_COUNT) + 1) + lapOffsetFor(sessionId);
  // The team-mate is a few tenths off, consistently.
  const carOffset = carIndex === PLAYER_CAR_INDEX ? 0 : 420 + Math.round(jitter(lap, carIndex) * 260);
  return base + carOffset;
}

function participantsFor(): Participant[] {
  return [
    {
      car_index: TEAMMATE_CAR_INDEX,
      name: 'TEAM-MATE',
      team_id: 4,
      race_number: 18,
      is_ai: true,
      is_player: false
    },
    {
      car_index: PLAYER_CAR_INDEX,
      name: 'YOU',
      team_id: 4,
      race_number: 14,
      is_ai: false,
      is_player: true
    }
  ];
}

function bestLapOf(sessionId: number, seed: SessionSeed): number | null {
  let best: number | null = null;
  for (let lap = 1; lap <= seed.laps; lap++) {
    if (INVALID_LAPS.has(lap)) continue;
    const t = sessionLapTimeMs(sessionId, lap, PLAYER_CAR_INDEX);
    if (best === null || t < best) best = t;
  }
  return best;
}

/** The fastest lap by any car — the team-mate is slower, so this is the player's. */
function fieldBestLapOf(sessionId: number, seed: SessionSeed): number | null {
  let best: number | null = null;
  for (const car of [PLAYER_CAR_INDEX, TEAMMATE_CAR_INDEX]) {
    for (let lap = 1; lap <= seed.laps; lap++) {
      if (car === PLAYER_CAR_INDEX && INVALID_LAPS.has(lap)) continue;
      const t = sessionLapTimeMs(sessionId, lap, car);
      if (best === null || t < best) best = t;
    }
  }
  return best;
}

function summaryOf(seed: SessionSeed): SessionSummary {
  return {
    id: seed.id,
    session_uid: `17984412345678${String(seed.id).padStart(6, '0')}`,
    segment: 0,
    packet_format: 2026,
    session_type: seed.type,
    session_type_name: seed.typeName,
    track_id: seed.trackId,
    track_name: seed.trackName,
    started_at_wall: BASE_WALL + seed.startedOffset,
    ended_at_wall: BASE_WALL + seed.startedOffset + seed.durationS,
    ended_reason: 'chequered_flag',
    joined_in_progress: false,
    player_car_index: PLAYER_CAR_INDEX,
    total_laps: seed.totalLaps,
    session_duration_s: seed.durationS,
    weather_json: { weather: 0, track_temp_c: 33, air_temp_c: 26, forecast: [] },
    final_classification_json: null,
    raw_file: `/data/raw/session-${seed.id}.f1raw`,
    // Two cars run every lap, so the all-cars total is twice what the driver
    // drove — which is exactly why the table shows `player_lap_count` instead.
    lap_count: seed.laps * 2,
    player_lap_count: seed.laps,
    segments: 1,
    field_best_lap_ms: fieldBestLapOf(seed.id, seed),
    player_name: 'YOU',
    // Deliberately present: the list endpoint does not compute this today, but
    // the field is optional in the client type and the table renders it when it
    // appears. Serving it here keeps the column exercised in dev.
    best_lap_ms: bestLapOf(seed.id, seed)
  };
}

export function mockSessions(): SessionSummary[] {
  return SESSION_SEEDS.map(summaryOf).sort(
    (x, y) => (y.started_at_wall ?? 0) - (x.started_at_wall ?? 0)
  );
}

function stintRowsFor(seed: SessionSeed): TyreStintRow[] {
  if (seed.id !== HEADLINE_SESSION_ID) {
    return [
      {
        car_index: PLAYER_CAR_INDEX,
        stint_no: 1,
        compound_actual: 16,
        compound_visual: 16,
        lap_start: 1,
        lap_end: seed.laps,
        wear_at_end_json: null,
        end_reason: 'session_end'
      }
    ];
  }
  return [
    {
      car_index: PLAYER_CAR_INDEX,
      stint_no: 1,
      compound_actual: 17,
      compound_visual: 17,
      lap_start: 1,
      lap_end: PIT_IN_LAP,
      wear_at_end_json: { rl: 31.0, rr: 33.5, fl: 20.1, fr: 22.0 },
      end_reason: 'pit'
    },
    {
      car_index: PLAYER_CAR_INDEX,
      stint_no: 2,
      compound_actual: 18,
      compound_visual: 18,
      lap_start: PIT_OUT_LAP,
      lap_end: seed.laps,
      wear_at_end_json: { rl: 24.4, rr: 26.1, fl: 15.9, fr: 17.2 },
      end_reason: 'session_end'
    }
  ];
}

export function mockSessionDetail(id: number): SessionDetail | null {
  const seed = seedFor(id);
  if (!seed) return null;
  // The detail endpoint is a single-row query, not the list's cross-segment
  // aggregate, so it computes neither of these. Dropping them here keeps the
  // page's own fallback (count the laps it fetched) on the path dev exercises.
  const summary = summaryOf(seed);
  delete summary.player_lap_count;
  delete summary.segments;
  return {
    ...summary,
    telemetry_sample_count: seed.laps * 1800,
    wear_sample_count: seed.laps * 20,
    event_count: 30 + seed.laps,
    best_lap_ms: bestLapOf(seed.id, seed),
    participants: participantsFor(),
    tyre_stints: stintRowsFor(seed)
  };
}

// ── laps ─────────────────────────────────────────────────────────────────────

function lapRow(sessionId: number, carIndex: number, lap: number, seed: SessionSeed): Lap {
  const isPlayer = carIndex === PLAYER_CAR_INDEX;
  const t = sessionLapTimeMs(sessionId, lap, carIndex);
  const sectors = sectorsFor(((lap - 1) % LAP_COUNT) + 1);
  const share = t / Math.max(1, sectors[0] + sectors[1] + sectors[2]);
  const stint = seed.id === HEADLINE_SESSION_ID ? stintOf(lap) : 1;
  const compound = seed.id === HEADLINE_SESSION_ID ? (stint === 1 ? 17 : 18) : 16;
  const invalid = isPlayer && seed.id === HEADLINE_SESSION_ID && INVALID_LAPS.has(lap);

  return {
    session_id: sessionId,
    car_index: carIndex,
    lap_number: lap,
    generation: 0,
    lap_time_ms: t,
    s1_ms: Math.round(sectors[0] * share),
    s2_ms: Math.round(sectors[1] * share),
    s3_ms: Math.round(sectors[2] * share),
    valid: !invalid,
    compound_actual: compound,
    compound_visual: compound,
    tyre_age_laps: seed.id === HEADLINE_SESSION_ID ? tyreAge(lap) : lap - 1,
    fuel_start_kg: Math.max(1, 103 - lap * 1.8),
    fuel_end_kg: Math.max(0.5, 103 - (lap + 1) * 1.8),
    ers_deployed_j: 3_900_000,
    ers_harvested_j: 1_800_000,
    top_speed_kmh: Math.round((TOP_SPEED_KMH / paceScale(lap)) * 10) / 10,
    penalties_s: 0,
    wall_ts: BASE_WALL + seed.startedOffset + lap * 95
  };
}

export function mockLaps(sessionId: number, carIndex?: number | null): Lap[] {
  const seed = seedFor(sessionId);
  if (!seed) return [];
  const cars =
    carIndex === null || carIndex === undefined
      ? [TEAMMATE_CAR_INDEX, PLAYER_CAR_INDEX]
      : [carIndex];
  const out: Lap[] = [];
  for (const car of cars) {
    if (car !== PLAYER_CAR_INDEX && car !== TEAMMATE_CAR_INDEX) continue;
    for (let lap = 1; lap <= seed.laps; lap++) out.push(lapRow(sessionId, car, lap, seed));
  }
  return out.sort((x, y) => x.car_index - y.car_index || x.lap_number - y.lap_number);
}

// ── telemetry ────────────────────────────────────────────────────────────────

/** The capture rate the contract promises: "rows are already 20 Hz". */
const SAMPLE_HZ = 20;

/**
 * The lap whose telemetry carries duplicate distance samples.
 *
 * This is the in-lap (`PIT_IN_LAP`): the car crawls to the pit box, so consecutive
 * samples round to the same distance. Keeping it on one known lap means every other
 * fixture stays clean and a test can assert both the clean and the degenerate case.
 */
export const DUPLICATE_DISTANCE_LAP = PIT_IN_LAP;

/** Repeat a handful of distance values in place, the way a slow crawl does. */
function injectDuplicateDistances(distance: number[]): void {
  // Three samples on one value near the pit entry, then a plain pair further along —
  // both shapes occur in the real capture.
  const triple = Math.floor(distance.length * 0.82);
  const pair = Math.floor(distance.length * 0.9);
  if (triple + 2 >= distance.length || pair + 1 >= distance.length) return;
  distance[triple + 1] = distance[triple] as number;
  distance[triple + 2] = distance[triple] as number;
  distance[pair + 1] = distance[pair] as number;
}

export function mockTelemetry(
  sessionId: number,
  lap: number,
  carIndex?: number | null
): LapTelemetry | null {
  const seed = seedFor(sessionId);
  if (!seed || lap < 1 || lap > seed.laps) return null;
  const car = carIndex ?? PLAYER_CAR_INDEX;

  const modelLap = ((lap - 1) % LAP_COUNT) + 1;
  const { dist, time, speed } = lapTrace(modelLap);
  const total = time[time.length - 1] ?? 0;

  const distance_m: number[] = [];
  const speed_kmh: number[] = [];
  const steer: number[] = [];
  const gear: number[] = [];
  const rpm: number[] = [];
  const drs_or_aero: number[] = [];
  const session_time_s: number[] = [];

  // Walk the lap in even time steps, reading distance off the integration.
  const steps = Math.floor(total * SAMPLE_HZ);
  let cursor = 0;
  for (let i = 0; i <= steps; i++) {
    const t = i / SAMPLE_HZ;
    while (cursor < time.length - 2 && (time[cursor + 1] ?? 0) < t) cursor++;
    const t0 = time[cursor] ?? 0;
    const t1 = time[cursor + 1] ?? t0 + 1e-6;
    const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
    const s = (dist[cursor] ?? 0) + f * ((dist[cursor + 1] ?? 0) - (dist[cursor] ?? 0));
    const v = (speed[cursor] ?? 0) + f * ((speed[cursor + 1] ?? 0) - (speed[cursor] ?? 0));
    const g = gearFor(v);

    distance_m.push(Math.round(s * 10) / 10);
    speed_kmh.push(Math.round(v * 10) / 10);
    steer.push(Math.round(Math.sin((s / BAHRAIN_LENGTH_M) * Math.PI * 14) * 0.55 * 100) / 100);
    gear.push(g);
    rpm.push(rpmFor(v, g));
    // DRS on the two detection-zone straights.
    const frac = s / BAHRAIN_LENGTH_M;
    drs_or_aero.push((frac > 0.02 && frac < 0.1) || (frac > 0.62 && frac < 0.65) ? 1 : 0);
    session_time_s.push(Math.round((lap * 95 + t) * 100) / 100);
  }

  // Real captures are NOT strictly monotonic in distance and the UI must not assume it.
  // The wire format rounds distance, and anywhere the car is crawling — a standing start,
  // a pit-lane stint, a safety-car train, the overlap left by a flashback — two 20 Hz
  // samples land on the same value. Session 102's opening lap had one distance repeated
  // three times and its pit lap eleven such samples. Mock mode reproduces that on the
  // designated slow lap so the charts are exercised against it, rather than only ever
  // seeing a textbook-clean axis.
  if (lap === DUPLICATE_DISTANCE_LAP) {
    injectDuplicateDistances(distance_m);
  }

  const { throttle, brake } = pedalsFromSpeed(speed_kmh);

  return {
    session_id: sessionId,
    lap_number: lap,
    car_index: car,
    points: distance_m.length,
    distance_m,
    speed_kmh,
    throttle,
    brake,
    steer,
    gear,
    rpm,
    drs_or_aero,
    session_time_s
  };
}

// ── compare ──────────────────────────────────────────────────────────────────

/** Grid step for the resampled comparison, matching the contract's "~5 m". */
const GRID_STEP_M = 5;

/** Sample a lap's integration onto an arbitrary distance grid. */
function resample(
  lap: number,
  grid: number[]
): { speed: number[]; throttle: number[]; brake: number[]; gear: number[]; time: number[] } {
  const { dist, time, speed } = lapTrace(lap);
  const outSpeed: number[] = [];
  const outGear: number[] = [];
  const outTime: number[] = [];

  let cursor = 0;
  for (const s of grid) {
    while (cursor < dist.length - 2 && (dist[cursor + 1] ?? 0) < s) cursor++;
    const d0 = dist[cursor] ?? 0;
    const d1 = dist[cursor + 1] ?? d0 + 1;
    const f = d1 === d0 ? 0 : (s - d0) / (d1 - d0);
    const v = (speed[cursor] ?? 0) + f * ((speed[cursor + 1] ?? 0) - (speed[cursor] ?? 0));
    const t = (time[cursor] ?? 0) + f * ((time[cursor + 1] ?? 0) - (time[cursor] ?? 0));

    outSpeed.push(Math.round(v * 10) / 10);
    outGear.push(gearFor(v));
    outTime.push(t);
  }

  const { throttle, brake } = pedalsFromSpeed(outSpeed);
  return { speed: outSpeed, throttle, brake, gear: outGear, time: outTime };
}

/** Thrown shapes mirror the API's own failure modes. */
export interface MockFailure {
  status: number;
  detail: string;
}

export function mockCompare(
  sessionA: number,
  lapA: number,
  sessionB: number,
  lapB: number
): CompareResponse | MockFailure {
  const seedA = seedFor(sessionA);
  const seedB = seedFor(sessionB);
  if (!seedA || !seedB) return { status: 404, detail: 'session not found' };
  if (seedA.trackId !== seedB.trackId) {
    return { status: 422, detail: 'sessions are at different tracks' };
  }
  if (lapA < 1 || lapA > seedA.laps || lapB < 1 || lapB > seedB.laps) {
    return { status: 404, detail: 'lap not found' };
  }

  const grid: number[] = [];
  for (let s = 0; s <= BAHRAIN_LENGTH_M; s += GRID_STEP_M) grid.push(s);

  const modelA = ((lapA - 1) % LAP_COUNT) + 1;
  const modelB = ((lapB - 1) % LAP_COUNT) + 1;
  const ra = resample(modelA, grid);
  const rb = resample(modelB, grid);

  const offsetA = lapOffsetFor(sessionA) / 1000;
  const offsetB = lapOffsetFor(sessionB) / 1000;
  const totalA = ra.time[ra.time.length - 1] ?? 1;
  const totalB = rb.time[rb.time.length - 1] ?? 1;

  // Fold each session's constant lap-time offset in proportionally, so a
  // cross-session comparison shows a delta that grows across the lap.
  const delta_ms = grid.map((_, i) => {
    const ta = (ra.time[i] ?? 0) * (1 + offsetA / totalA);
    const tb = (rb.time[i] ?? 0) * (1 + offsetB / totalB);
    return Math.round((ta - tb) * 1000);
  });

  const side = (
    sessionId: number,
    lapNumber: number,
    r: ReturnType<typeof resample>
  ): CompareSide => ({
    session_id: sessionId,
    lap_number: lapNumber,
    lap_time_ms: sessionLapTimeMs(sessionId, lapNumber, PLAYER_CAR_INDEX),
    speed_kmh: r.speed,
    throttle: r.throttle,
    brake: r.brake,
    gear: r.gear
  });

  return {
    track_id: seedA.trackId,
    track_name: seedA.trackName,
    grid_m: grid,
    a: side(sessionA, lapA, ra),
    b: side(sessionB, lapB, rb),
    delta_ms,
    sectors_a: sectorsFor(modelA),
    sectors_b: sectorsFor(modelB)
  };
}

// ── corners ──────────────────────────────────────────────────────────────────

export function mockCorners(
  sessionId: number,
  lap: number,
  ref: string | number
): CornersResponse | MockFailure {
  const seed = seedFor(sessionId);
  if (!seed) return { status: 404, detail: 'session not found' };
  if (lap < 1 || lap > seed.laps) return { status: 404, detail: 'lap not found' };

  // `ref=best` resolves to the session-best valid lap of the player.
  let refLap: number;
  if (ref === 'best' || ref === '' || ref === 'track_best') {
    // Mirrors the backend: the subject lap is excluded from automatic resolution. It used
    // to be eligible, so asking for corners on the session-best lap resolved the reference
    // to that same lap and produced a table of ±0.000 that looked like a real result.
    let best: number | null = null;
    let bestLap: number | null = null;
    for (let l = 1; l <= seed.laps; l++) {
      if (INVALID_LAPS.has(l) || l === lap) continue;
      const t = sessionLapTimeMs(sessionId, l, PLAYER_CAR_INDEX);
      if (best === null || t < best) {
        best = t;
        bestLap = l;
      }
    }
    refLap = bestLap ?? lap;
  } else {
    const n = Number(ref);
    if (!Number.isFinite(n) || n < 1 || n > seed.laps) {
      return { status: 422, detail: 'ref must be "best" or a lap number' };
    }
    refLap = n;
  }

  const modelLap = ((lap - 1) % LAP_COUNT) + 1;
  const modelRef = ((refLap - 1) % LAP_COUNT) + 1;
  const cur = lapTrace(modelLap);
  const refTrace = lapTrace(modelRef);

  /** Time at a distance, by linear interpolation over the integration. */
  const timeAt = (trace: { dist: number[]; time: number[] }, m: number): number => {
    const { dist, time } = trace;
    if (m <= 0) return time[0] ?? 0;
    for (let i = 0; i < dist.length - 1; i++) {
      const d0 = dist[i] ?? 0;
      const d1 = dist[i + 1] ?? d0;
      if (d1 >= m) {
        const f = d1 === d0 ? 0 : (m - d0) / (d1 - d0);
        return (time[i] ?? 0) + f * ((time[i + 1] ?? 0) - (time[i] ?? 0));
      }
    }
    return time[time.length - 1] ?? 0;
  };

  /** Minimum speed within a distance window. */
  const minSpeedIn = (
    trace: { dist: number[]; speed: number[] },
    from: number,
    to: number
  ): number => {
    let min = Infinity;
    for (let i = 0; i < trace.dist.length; i++) {
      const d = trace.dist[i] ?? 0;
      if (d < from) continue;
      if (d > to) break;
      min = Math.min(min, trace.speed[i] ?? Infinity);
    }
    return Number.isFinite(min) ? Math.round(min * 10) / 10 : 0;
  };

  /**
   * Where the car came off the throttle: the braking point.
   *
   * Detected as the first place speed falls meaningfully below the local
   * maximum on the approach — so the search has to start at the *peak* between
   * this corner and the last one, not at a fixed distance before the entry. A
   * fixed offset lands inside the previous corner's exit on a tight sequence,
   * where speed is still rising; the "first fall" is then pinned to the window
   * edge and reports the same number for every lap, which is precisely the bug
   * this shape avoids.
   */
  const brakePoint = (
    trace: { dist: number[]; speed: number[] },
    searchFrom: number,
    apex: number
  ): number => {
    const { dist, speed } = trace;
    let iMax = -1;
    let vMax = -Infinity;
    for (let i = 0; i < dist.length; i++) {
      const d = dist[i] ?? 0;
      if (d < searchFrom) continue;
      if (d > apex) break;
      const v = speed[i] ?? 0;
      if (v > vMax) {
        vMax = v;
        iMax = i;
      }
    }
    if (iMax < 0) return Math.round(searchFrom * 10) / 10;
    for (let i = iMax; i < dist.length; i++) {
      const d = dist[i] ?? 0;
      if (d > apex) break;
      if ((speed[i] ?? 0) < vMax - 3) return Math.round(d * 10) / 10;
    }
    return Math.round((dist[iMax] ?? searchFrom) * 10) / 10;
  };

  const corners: Corner[] = CORNERS.map((c, i) => {
    const apex_m = Math.round(c.at * BAHRAIN_LENGTH_M * 10) / 10;
    const entry_m = Math.round((c.at - c.w) * BAHRAIN_LENGTH_M * 10) / 10;
    const exit_m = Math.round((c.at + c.w) * BAHRAIN_LENGTH_M * 10) / 10;
    // Look for the braking point from the previous apex — the stretch between
    // two apexes always contains the speed peak, however tight the sequence.
    const searchFrom = i === 0 ? 0 : (CORNERS[i - 1]?.at ?? 0) * BAHRAIN_LENGTH_M;

    const lossS =
      timeAt(cur, exit_m) - timeAt(cur, entry_m) - (timeAt(refTrace, exit_m) - timeAt(refTrace, entry_m));

    const min_speed_kmh = minSpeedIn(cur, entry_m, exit_m);
    const ref_min_speed_kmh = minSpeedIn(refTrace, entry_m, exit_m);

    return {
      n: c.n,
      entry_m,
      apex_m,
      exit_m,
      min_speed_kmh,
      ref_min_speed_kmh,
      brake_point_m: brakePoint(cur, searchFrom, apex_m),
      ref_brake_point_m: brakePoint(refTrace, searchFrom, apex_m),
      time_loss_ms: Math.round(lossS * 1000),
      kind: kindOf(ref_min_speed_kmh)
    };
  });

  const totalS =
    (cur.time[cur.time.length - 1] ?? 0) - (refTrace.time[refTrace.time.length - 1] ?? 0);
  const total_delta_ms = Math.round(totalS * 1000);
  const cornerLoss = corners.reduce((sum, c) => sum + c.time_loss_ms, 0);

  return {
    session_id: sessionId,
    lap_number: lap,
    ref_lap_number: refLap,
    ref_session_id: sessionId,
    ref_session_label: null,
    // Mirrors the backend: a reference that IS the subject lap is an identity check, not
    // a result. The page captions it instead of presenting the ±0 columns as a comparison.
    self_reference: refLap === lap,
    corners,
    straights_time_loss_ms: total_delta_ms - cornerLoss,
    total_delta_ms
  };
}

// ── stints ───────────────────────────────────────────────────────────────────

/** Least squares over (x, y) pairs, returning slope, intercept and r². */
function linearFit(xs: number[], ys: number[]): { slope: number; base: number; r2: number } {
  const n = xs.length;
  if (n < 2) return { slope: 0, base: ys[0] ?? 0, r2: 0 };
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < n; i++) {
    const dx = (xs[i] ?? 0) - mx;
    const dy = (ys[i] ?? 0) - my;
    sxy += dx * dy;
    sxx += dx * dx;
    syy += dy * dy;
  }
  const slope = sxx === 0 ? 0 : sxy / sxx;
  const base = my - slope * mx;
  const r2 = sxx === 0 || syy === 0 ? 0 : (sxy * sxy) / (sxx * syy);
  return { slope, base, r2 };
}

// ── debrief ──────────────────────────────────────────────────────────────────

/**
 * The prose an LLM wrote from the race's fact sheet, and a slice of that sheet.
 *
 * Written to look like a real grounded debrief rather than lorem ipsum: every
 * figure quoted here appears in the `fact_sheet` beside it, which is the whole
 * point of the feature and the thing the panel exists to show.
 *
 * Only the headline race has one. The quali and the Monza session deliberately
 * do not, so the "no debrief yet" state is reachable in dev without a backend —
 * the same reason `OTHER_TRACK_SESSION_ID` exists for the 422 path.
 */
const MOCK_DEBRIEF_TEXT = `P4 from P6, and the race was won in the first stint rather than the last.

Your best lap was a 1:33.412 on lap 8, and the median of your eleven representative laps was 1:34.180 — a spread of 0.62 s between the first and third quartile. That is tight for a race stint, and it is the reason the strategy held: you were repeatable enough that the medium stint's degradation of 84 ms per lap was the only thing eating the gap.

The soft stint told a different story. Six laps, 148 ms per lap of degradation, and by lap 6 you were 0.9 s off the pace you set on lap 2. The stop came at the right time.

Against the qualifying benchmark you lost 1.31 s over the lap, and 0.74 s of that sits in three corners. The worst is the slow corner at 1285 m: you are 8.5 km/h down at the apex and braking 27 m earlier than the reference. The medium corner at 2460 m costs another 0.24 s on the same pattern — early brake, low minimum, late reapplication.

One flashback, no penalties, no safety car. Nothing about this race was chaotic; it was decided on tyre management and one corner.

Focus for the next session: the slow corner at 1285 m. Carry the brake 25 m deeper and hold the minimum speed above 120 km/h.`;

const MOCK_FACT_SHEET = {
  fact_sheet_version: 1,
  session: { track: 'Bahrain', type: 'Race', is_race: true },
  result: { finish_position: 4, grid_position: 6, places_gained: 2, points_scored: 12 },
  pace: {
    best_lap: '1:33.412',
    best_lap_number: 8,
    median_lap: '1:34.180',
    iqr_s: 0.62,
    consistency_laps_used: 11
  },
  stints: [
    { stint: 1, compound: 'Soft', degradation_ms_per_lap: 148, degradation_confidence: 'strong' },
    { stint: 2, compound: 'Medium', degradation_ms_per_lap: 84, degradation_confidence: 'strong' }
  ],
  events: { flashbacks: 1, penalties: [], safety_car_deployments: 0, pit_stops: 1 }
};

/** The stored debrief for one session, or null when it has none. */
export function mockDebrief(sessionId: number): Debrief | null {
  const seed = seedFor(sessionId);
  if (!seed || seed.id !== HEADLINE_SESSION_ID) return null;
  return {
    session_id: seed.id,
    // Generated a couple of minutes after the session closed, as the serve path does.
    created_at: BASE_WALL + seed.startedOffset + seed.durationS + 137,
    model: 'example-model:8b',
    prompt_version: 1,
    text: MOCK_DEBRIEF_TEXT,
    fact_sheet: MOCK_FACT_SHEET
  };
}

export function mockStints(sessionId: number): StintsResponse | MockFailure {
  const seed = seedFor(sessionId);
  if (!seed) return { status: 404, detail: 'session not found' };

  const rows = stintRowsFor(seed);
  const stints: Stint[] = rows.map((row) => {
    const from = row.lap_start ?? 1;
    const to = row.lap_end ?? seed.laps;

    const raw: Array<{ lap: number; time: number }> = [];
    for (let lap = from; lap <= to; lap++) {
      raw.push({ lap, time: sessionLapTimeMs(sessionId, lap, PLAYER_CAR_INDEX) });
    }
    const sorted = raw.map((r) => r.time).sort((x, y) => x - y);
    const median = sorted[Math.floor(sorted.length / 2)] ?? 0;

    const laps: StintLap[] = raw.map(({ lap, time }) => {
      const valid = !INVALID_LAPS.has(lap);
      let reason: string | null = null;
      if (lap === PIT_IN_LAP) reason = 'in-lap';
      else if (lap === PIT_OUT_LAP || lap === from) reason = 'out-lap';
      else if (!valid) reason = 'lap deleted';
      else if (time > median * 1.07) reason = 'above 107% of stint median';
      return {
        lap_number: lap,
        lap_time_ms: time,
        valid,
        excluded: reason !== null,
        exclude_reason: reason
      };
    });

    const used = laps.filter((l) => !l.excluded && l.lap_time_ms !== null);
    const fit =
      used.length >= 2
        ? (() => {
            const f = linearFit(
              used.map((l) => l.lap_number),
              used.map((l) => l.lap_time_ms as number)
            );
            return {
              deg_ms_per_lap: Math.round(f.slope * 10) / 10,
              base_ms: Math.round(f.base + f.slope * from),
              r2: Math.round(f.r2 * 1000) / 1000,
              n_used: used.length
            };
          })()
        : null;

    const wear = row.wear_at_end_json as Record<string, number> | null;
    return {
      stint_no: row.stint_no,
      car_index: row.car_index,
      compound_visual: row.compound_visual,
      lap_start: from,
      lap_end: to,
      laps,
      fit,
      wear_end_pct: wear ? [wear['rl'] ?? 0, wear['rr'] ?? 0, wear['fl'] ?? 0, wear['fr'] ?? 0] : null
    };
  });

  return { session_id: sessionId, stints };
}

// ── strategy ─────────────────────────────────────────────────────────────────

/**
 * The strategy sheet, modelled rather than transcribed.
 *
 * Same rule as everything else here: this is not a fixture blob, it is the same
 * decision the backend makes — measure each compound, cap a stint at the wear
 * cliff, enumerate the legal orderings, hand laps out to whichever stint's next
 * lap is cheapest, and rank on the total. So the page is exercised against a
 * plan whose stint lengths and pit windows are internally consistent, and the
 * "untested compound" and "no legal plan" paths are reachable by changing the
 * inputs rather than by hand-writing a second payload.
 */

/** Must match `analysis/strategy.py::WEAR_CLIFF_PCT`. */
const WEAR_CLIFF_PCT = 28.0;
const FUEL_MARGIN_LAPS = 0.45;
const MAX_PLANS = 12;
const SC_FLEXIBLE_WINDOW_LAPS = 3;
const MIN_FIT_LAPS = 4;
const FUEL_KG_PER_LAP = 1.82;
const PIT_LOSS_S = 20.6;
/** `analysis/strategy.py::DEFAULT_PIT_LOSS_S`, for a circuit with no recorded stop. */
const DEFAULT_PIT_LOSS_S = 21.0;

/** The two ranking notes, word for word as `strategy.py::_RANKING_NOTES` emits them. */
const RANKING_NOTES: Record<PlansRanking, string> = {
  time:
    'ranked on projected race time: every compound in these plans has a lap-time model, ' +
    "and each plan's stint lengths are the split that minimises the sum of those models " +
    'plus the pit loss.',
  heuristic:
    'ranked by rule, not by time: at least one compound here has a measured wear rate but ' +
    'no lap-time model, so no race time is projected for any of these plans — fewest stops ' +
    'first, then the softer compound earlier. The stint lengths, pit windows and projected ' +
    'wear are all real; only the order is a preference. Run four clean laps on one set at ' +
    'this circuit and the whole set becomes time-ranked.'
};

const CALIBRATION_ASSUMPTION =
  'time lost per percent of worst-wheel wear is treated as the same for all three dry ' +
  'compounds at this circuit. That is an assumption, not a measurement: it is what lets a ' +
  'compound with a wear rate and no lap-time fit of its own be given a slope. A compound ' +
  'with its own four-lap fit always keeps it.';

interface CompoundEvidence {
  compound: number;
  name: string;
  /**
   * The stint's own least-squares line, when it ran long enough for one. Absent means the
   * set never did `MIN_FIT_LAPS` clean laps — the ordinary practice run — and the slope, if
   * there is one at all, is derived from the circuit's coefficient instead.
   */
  fit?: { baseMs: number; degMsPerLap: number; r2: number; lapsUsed: number };
  /** Base pace for a derived model: the middle of this compound's own clean laps here. */
  medianCleanMs?: number;
  medianCleanLaps?: number;
  wearPctPerLap: number;
  wearLaps: number;
  wearSource: 'wear_samples' | 'stint_end_wear';
  evidence: 'race' | 'practice';
  sessionId: number;
  sessionLabel: string;
  stintNo: number;
  lapRange: [number, number];
}

/** The one stint at a circuit that lap time was regressed against cumulative wear on. */
interface CalibrationEvidence {
  msPerWearPct: number;
  compound: number;
  name: string;
  evidence: 'race' | 'practice';
  sessionId: number;
  sessionLabel: string;
  stintNo: number;
  lapRange: [number, number];
  lapsUsed: number;
  r2: number;
  wearSpanPct: number;
}

/**
 * What the weekend measured, per circuit.
 *
 * Bahrain has the full picture: two compounds fitted out of the race and quali, and a hard
 * that only ever did a short run — a wear rate and no line of its own — so its slope is
 * *derived* from the circuit's wear-to-time coefficient. That is the case the compound table
 * has to mark, because a derived slope is a good estimate and not a measurement of that
 * tyre. Montreal is the weekend with no long run anywhere: three wear rates, no fits, no
 * coefficient, and therefore feasibility plans ranked by rule. Monza has one session and no
 * race at all, so it exercises the "tell me how long the race is" 422.
 */
const STRATEGY_EVIDENCE: Record<number, CompoundEvidence[]> = {
  [BAHRAIN_TRACK_ID]: [
    {
      compound: 17,
      name: 'Medium',
      fit: { baseMs: 93_050, degMsPerLap: 168, r2: 0.88, lapsUsed: 8 },
      wearPctPerLap: 3.35,
      wearLaps: 10,
      wearSource: 'wear_samples',
      evidence: 'race',
      sessionId: HEADLINE_SESSION_ID,
      sessionLabel: `Race (session ${HEADLINE_SESSION_ID})`,
      stintNo: 1,
      lapRange: [1, PIT_IN_LAP]
    },
    {
      // Three laps at the end of the race and the flag: a wear rate, and nothing to fit.
      compound: 18,
      name: 'Hard',
      medianCleanMs: 93_910,
      medianCleanLaps: 3,
      wearPctPerLap: 2.9,
      wearLaps: 3,
      wearSource: 'wear_samples',
      evidence: 'race',
      sessionId: HEADLINE_SESSION_ID,
      sessionLabel: `Race (session ${HEADLINE_SESSION_ID})`,
      stintNo: 2,
      lapRange: [PIT_OUT_LAP, LAP_COUNT]
    },
    {
      // Quali running: quick, and going off twice as fast as anything in the race.
      compound: 16,
      name: 'Soft',
      fit: { baseMs: 91_720, degMsPerLap: 402, r2: 0.74, lapsUsed: 5 },
      wearPctPerLap: 6.2,
      wearLaps: 5,
      wearSource: 'stint_end_wear',
      evidence: 'practice',
      sessionId: QUALI_SESSION_ID,
      sessionLabel: `Qualifying 3 (session ${QUALI_SESSION_ID})`,
      stintNo: 1,
      lapRange: [1, 6]
    }
  ],
  [MONTREAL_TRACK_ID]: [
    {
      compound: 16,
      name: 'Soft',
      medianCleanMs: 76_240,
      medianCleanLaps: 2,
      wearPctPerLap: 5.4,
      wearLaps: 2,
      wearSource: 'wear_samples',
      evidence: 'practice',
      sessionId: MONTREAL_PRACTICE_SESSION_ID,
      sessionLabel: `Practice 1 (session ${MONTREAL_PRACTICE_SESSION_ID})`,
      stintNo: 1,
      lapRange: [1, 3]
    },
    {
      compound: 17,
      name: 'Medium',
      medianCleanMs: 76_880,
      medianCleanLaps: 2,
      wearPctPerLap: 3.6,
      wearLaps: 2,
      wearSource: 'wear_samples',
      evidence: 'practice',
      sessionId: MONTREAL_PRACTICE_SESSION_ID,
      sessionLabel: `Practice 1 (session ${MONTREAL_PRACTICE_SESSION_ID})`,
      stintNo: 2,
      lapRange: [3, 6]
    },
    {
      compound: 18,
      name: 'Hard',
      medianCleanMs: 77_410,
      medianCleanLaps: 2,
      wearPctPerLap: 2.7,
      wearLaps: 2,
      wearSource: 'wear_samples',
      evidence: 'practice',
      sessionId: MONTREAL_PRACTICE_SESSION_ID,
      sessionLabel: `Practice 1 (session ${MONTREAL_PRACTICE_SESSION_ID})`,
      stintNo: 3,
      lapRange: [6, 8]
    }
  ],
  11: []
};

/**
 * Bahrain's race stint on the mediums, regressed against wear instead of lap number: 168 ms
 * a lap over 3.35 % of wear a lap is 50.1 ms per percent. Montreal has none — no stint there
 * ran four clean laps — which is exactly what puts its plans on the heuristic path.
 */
const STRATEGY_CALIBRATION: Record<number, CalibrationEvidence | undefined> = {
  [BAHRAIN_TRACK_ID]: {
    msPerWearPct: 50.1,
    compound: 17,
    name: 'Medium',
    evidence: 'race',
    sessionId: HEADLINE_SESSION_ID,
    sessionLabel: `Race (session ${HEADLINE_SESSION_ID})`,
    stintNo: 1,
    lapRange: [1, PIT_IN_LAP],
    lapsUsed: 8,
    r2: 0.86,
    wearSpanPct: 30.2
  }
};

/** The calibration stint block, shared by the top-level coefficient and every borrower. */
function calibrationStint(c: CalibrationEvidence): StrategyCalibrationStint {
  return {
    compound_visual: c.compound,
    name: c.name,
    evidence: c.evidence,
    session_id: c.sessionId,
    session_label: c.sessionLabel,
    stint_no: c.stintNo,
    lap_range: [c.lapRange[0], c.lapRange[1]],
    laps_used: c.lapsUsed,
    r2: c.r2,
    wear_span_pct: c.wearSpanPct
  };
}

function strategyPace(
  evidence: CompoundEvidence,
  calibration: CalibrationEvidence | undefined
): StrategyCompound['pace'] {
  if (evidence.fit) {
    return {
      base_ms: evidence.fit.baseMs,
      deg_ms_per_lap: evidence.fit.degMsPerLap,
      r2: evidence.fit.r2,
      laps_used: evidence.fit.lapsUsed,
      evidence: evidence.evidence,
      session_id: evidence.sessionId,
      session_label: evidence.sessionLabel,
      stint_no: evidence.stintNo,
      lap_range: [evidence.lapRange[0], evidence.lapRange[1]],
      source: 'fit'
    };
  }
  if (!calibration || evidence.medianCleanMs == null) return null;
  // The coefficient times this compound's own wear rate. Both parents travel with it.
  const deg = Math.round(calibration.msPerWearPct * evidence.wearPctPerLap * 10) / 10;
  const weaker = calibration.evidence === 'practice' ? 'practice' : evidence.evidence;
  return {
    base_ms: evidence.medianCleanMs,
    deg_ms_per_lap: deg,
    r2: null,
    laps_used: evidence.medianCleanLaps ?? 0,
    evidence: weaker,
    session_id: calibration.sessionId,
    session_label: calibration.sessionLabel,
    stint_no: calibration.stintNo,
    lap_range: [calibration.lapRange[0], calibration.lapRange[1]],
    source: 'derived',
    base_source: {
      source: 'median_clean_laps',
      laps: evidence.medianCleanLaps ?? 0,
      evidence: evidence.evidence,
      session_ids: [evidence.sessionId]
    },
    derived_from: {
      ms_per_wear_pct: calibration.msPerWearPct,
      wear_pct_per_lap: evidence.wearPctPerLap,
      calibration: calibrationStint(calibration),
      wear: {
        source: evidence.wearSource,
        laps: evidence.wearLaps,
        evidence: evidence.evidence,
        session_id: evidence.sessionId,
        session_label: evidence.sessionLabel,
        stint_no: evidence.stintNo
      }
    }
  };
}

function strategyCompound(
  compound: number,
  evidence: CompoundEvidence | undefined,
  raceLaps: number,
  calibration: CalibrationEvidence | undefined
): StrategyCompound {
  const name = compoundLabel(compound);
  if (!evidence) {
    return {
      compound_visual: compound,
      name,
      dry: true,
      untested: true,
      stints_seen: 0,
      evidence: null,
      pace: null,
      wear: null,
      max_stint_laps: null,
      feasible: false,
      plannable: false,
      not_plannable_reason: 'no stint on this compound was recorded this weekend'
    };
  }
  const maxLaps = Math.max(
    1,
    Math.min(raceLaps, Math.floor(WEAR_CLIFF_PCT / evidence.wearPctPerLap))
  );
  const pace = strategyPace(evidence, calibration);
  return {
    compound_visual: compound,
    name: evidence.name,
    dry: true,
    untested: false,
    stints_seen: 1,
    evidence: pace?.evidence ?? evidence.evidence,
    pace,
    wear: {
      pct_per_lap: evidence.wearPctPerLap,
      source: evidence.wearSource,
      laps: evidence.wearLaps,
      evidence: evidence.evidence,
      session_id: evidence.sessionId,
      session_label: evidence.sessionLabel,
      stint_no: evidence.stintNo
    },
    max_stint_laps: maxLaps,
    projected_wear_at_max_pct: Math.round(evidence.wearPctPerLap * maxLaps * 10) / 10,
    // A wear rate alone is enough to know a plan finishes; a pace model is what lets it be
    // ranked on time. The page distinguishes them, so the fixture has to as well.
    feasible: true,
    plannable: pace !== null,
    not_plannable_reason:
      pace !== null
        ? null
        : `no pace model — no stint on it had ${MIN_FIT_LAPS} clean laps to fit a line ` +
          'through, and no stint at this circuit ran long enough to calibrate lap time ' +
          'against wear and lend it one. Usable in feasibility plans — how long a set ' +
          'lasts is known — but those plans cannot be ranked on time'
  };
}

/** Names for the always-listed dry compounds, matching `enums.ts`. */
function compoundLabel(visual: number): string {
  return visual === 16 ? 'Soft' : visual === 17 ? 'Medium' : visual === 18 ? 'Hard' : 'Unknown';
}

/** Greedy convex allocation, exactly as `strategy.py::_allocate` does it. */
function allocate(
  chain: StrategyCompound[],
  raceLaps: number
): number[] | null {
  const caps = chain.map((c) => c.max_stint_laps ?? 0);
  const total = caps.reduce((a, b) => a + b, 0);
  if (raceLaps < chain.length || total < raceLaps) return null;
  const laps = chain.map(() => 1);
  for (let handed = chain.length; handed < raceLaps; handed++) {
    let best = -1;
    let bestCost = Infinity;
    chain.forEach((c, i) => {
      if (laps[i]! >= caps[i]!) return;
      const cost = (c.pace?.base_ms ?? 0) + Math.max(0, c.pace?.deg_ms_per_lap ?? 0) * laps[i]!;
      if (cost < bestCost) {
        best = i;
        bestCost = cost;
      }
    });
    if (best < 0) return null;
    laps[best]!++;
  }
  return laps;
}

/**
 * `strategy.py::_allocate_by_wear`: with no pace to trade, laps go to whichever stint is
 * least far through its own wear ceiling, so no set is pushed nearer the cliff than another.
 */
function allocateByWear(chain: StrategyCompound[], raceLaps: number): number[] | null {
  const caps = chain.map((c) => c.max_stint_laps ?? 0);
  const total = caps.reduce((a, b) => a + b, 0);
  if (raceLaps < chain.length || total < raceLaps) return null;
  const laps = chain.map(() => 1);
  for (let handed = chain.length; handed < raceLaps; handed++) {
    let best = -1;
    let bestLoad = Infinity;
    caps.forEach((cap, i) => {
      if (laps[i]! >= cap) return;
      const load = (laps[i]! + 1) / cap;
      if (load < bestLoad) {
        best = i;
        bestLoad = load;
      }
    });
    if (best < 0) return null;
    laps[best]!++;
  }
  return laps;
}

function planFor(
  chain: StrategyCompound[],
  raceLaps: number,
  ranking: PlansRanking,
  pitLossS: number
): StrategyPlan | null {
  const timed = ranking === 'time';
  const laps = timed ? allocate(chain, raceLaps) : allocateByWear(chain, raceLaps);
  if (!laps) return null;

  const stints: StrategyPlanStint[] = [];
  let totalMs = pitLossS * 1000 * (chain.length - 1);
  let cursor = 1;
  chain.forEach((c, i) => {
    const n = laps[i]!;
    const base = c.pace?.base_ms ?? 0;
    const deg = Math.max(0, c.pace?.deg_ms_per_lap ?? 0);
    totalMs += n * base + (deg * n * (n - 1)) / 2;
    stints.push({
      compound_visual: c.compound_visual,
      name: c.name,
      lap_start: cursor,
      lap_end: cursor + n - 1,
      laps: n,
      projected_end_wear_pct: Math.round((c.wear?.pct_per_lap ?? 0) * n * 10) / 10
    });
    cursor += n;
  });

  const caps = chain.map((c) => c.max_stint_laps ?? 0);
  const windows = [];
  for (let stop = 1; stop < chain.length; stop++) {
    const after = caps.slice(stop).reduce((a, b) => a + b, 0);
    const before = caps.slice(0, stop).reduce((a, b) => a + b, 0);
    const earliest = Math.max(stop, raceLaps - after);
    const latest = Math.min(before, raceLaps - (chain.length - stop));
    windows.push({
      stop,
      planned_lap: laps.slice(0, stop).reduce((a, b) => a + b, 0),
      earliest_lap: earliest,
      latest_lap: latest,
      window_laps: Math.max(0, latest - earliest)
    });
  }

  const widest = windows.reduce(
    (a, b) => (b.window_laps > a.window_laps ? b : a),
    windows[0] ?? { stop: 0, planned_lap: 0, earliest_lap: 0, latest_lap: 0, window_laps: 0 }
  );
  const lastStop = windows[windows.length - 1]?.planned_lap ?? 0;
  const flexible = widest.window_laps >= SC_FLEXIBLE_WINDOW_LAPS;

  return {
    rank: 0,
    stops: chain.length - 1,
    compounds: chain.map((c) => c.compound_visual),
    label: chain.map((c) => c.name).join(' → '),
    stints,
    // Null, not zero, when nothing measured a time: the page must show an em-dash rather
    // than a number that would read as a projection.
    total_time_ms: timed ? Math.round(totalMs * 10) / 10 : null,
    delta_to_best_ms: timed ? 0 : null,
    pit_windows: windows,
    safety_car: flexible
      ? {
          flexibility: 'flexible',
          note:
            `stop ${widest.stop} can be taken anywhere from lap ${widest.earliest_lap} to lap ` +
            `${widest.latest_lap}, so a safety car inside that window is close to a free stop. ` +
            `Last stop is planned for lap ${lastStop}, leaving ${raceLaps - lastStop} laps to run.`
        }
      : {
          flexibility: 'tight',
          note:
            `every stop has a window of ${widest.window_laps} lap(s) or less — the wear ` +
            'ceiling fixes when you box, so a safety car outside those laps cannot be used ' +
            `without going past the cliff. Last stop is planned for lap ${lastStop}.`
        }
  };
}

export function mockStrategy(
  trackId: number,
  raceLaps: number | null
): StrategyResponse | MockFailure {
  const sessions = SESSION_SEEDS.filter((s) => s.trackId === trackId);
  if (sessions.length === 0) return { status: 404, detail: 'no sessions recorded at this track' };

  const race = sessions.find((s) => s.type >= 15 && s.type <= 17 && s.totalLaps);
  if (raceLaps === null && !race) {
    return {
      status: 422,
      detail: 'no race at this track recorded its distance; pass race_laps=<int>'
    };
  }
  const laps = raceLaps ?? race?.totalLaps ?? LAP_COUNT;
  const source = raceLaps === null ? `${race?.typeName} (session ${race?.id})` : 'request';

  const evidence = STRATEGY_EVIDENCE[trackId] ?? [];
  const calibration = STRATEGY_CALIBRATION[trackId];
  const byCompound = new Map(evidence.map((e) => [e.compound, e]));
  const compounds = [16, 17, 18].map((c) =>
    strategyCompound(c, byCompound.get(c), laps, calibration)
  );

  // A circuit with no recorded stop borrows a named constant and says so — the state the
  // Bahrain fixture can never reach, and the one every practice-only weekend lands in.
  const measuredPitLoss = sessions.some((s) => s.id === HEADLINE_SESSION_ID);
  const pitLossS = measuredPitLoss ? PIT_LOSS_S : DEFAULT_PIT_LOSS_S;

  const rank = (pool: StrategyCompound[], ranking: PlansRanking): StrategyPlan[] => {
    const out: StrategyPlan[] = [];
    for (const stops of [1, 2]) {
      const combos: StrategyCompound[][] = [[]];
      for (let i = 0; i <= stops; i++) {
        const next: StrategyCompound[][] = [];
        for (const combo of combos) for (const c of pool) next.push([...combo, c]);
        combos.length = 0;
        combos.push(...next);
      }
      for (const combo of combos) {
        if (new Set(combo.map((c) => c.compound_visual)).size < 2) continue;
        const plan = planFor(combo, laps, ranking, pitLossS);
        if (plan) out.push(plan);
      }
    }
    if (ranking === 'time') {
      out.sort(
        (a, b) =>
          (a.total_time_ms ?? 0) - (b.total_time_ms ?? 0) ||
          a.stops - b.stops ||
          a.compounds.join().localeCompare(b.compounds.join())
      );
      const best = out[0]?.total_time_ms ?? 0;
      out.forEach((plan, i) => {
        plan.rank = i + 1;
        plan.delta_to_best_ms = Math.round(((plan.total_time_ms ?? 0) - best) * 10) / 10;
      });
    } else {
      // Fewest stops, then softer earlier — ascending on the compound sequence, since the
      // visual numbers run 16 soft, 17 medium, 18 hard.
      out.sort(
        (a, b) => a.stops - b.stops || a.compounds.join().localeCompare(b.compounds.join())
      );
      out.forEach((plan, i) => {
        plan.rank = i + 1;
      });
    }
    return out;
  };

  // Time ranking first and kept whenever it produces anything: a projected time is strictly
  // more information than a rule. Feasibility is the fallback, not the preference.
  const plannable = compounds.filter((c) => c.plannable);
  const feasible = compounds.filter((c) => c.feasible);
  let plans: StrategyPlan[] = plannable.length >= 2 ? rank(plannable, 'time') : [];
  let ranking: PlansRanking | null = plans.length > 0 ? 'time' : null;
  if (plans.length === 0 && feasible.length >= 2 && feasible.length !== plannable.length) {
    plans = rank(feasible, 'heuristic');
    ranking = plans.length > 0 ? 'heuristic' : null;
  }

  const fuelSessionId = race?.id ?? sessions[0]?.id ?? HEADLINE_SESSION_ID;
  const sessionsUsed: StrategySessionUsed[] = sessions.map((s) => ({
    id: s.id,
    session_type: s.type,
    session_type_name: s.typeName,
    // 15–17 are the races; 18 is a time trial and reads as practice trim.
    evidence: s.type >= 15 && s.type <= 17 ? 'race' : 'practice',
    started_at_wall: BASE_WALL + s.startedOffset,
    contributed: evidence
      .filter((e) => e.sessionId === s.id)
      .flatMap((e) => [
        ...(e.fit ? [`${e.name.toLowerCase()} pace`] : []),
        `${e.name.toLowerCase()} wear`
      ])
      // A derived slope is attributed to the stint the coefficient came from, since that is
      // where the number in the degradation column was actually measured.
      .concat(
        calibration && calibration.sessionId === s.id
          ? [
              'wear-to-time calibration',
              ...compounds
                .filter((c) => c.pace?.source === 'derived')
                .map((c) => `${c.name.toLowerCase()} pace`)
            ]
          : []
      )
      .concat(s.id === fuelSessionId ? ['fuel burn'] : [])
      .concat(measuredPitLoss && s.id === HEADLINE_SESSION_ID ? ['pit-lane loss'] : [])
      .sort()
  }));

  const omitted: Record<string, string> = {};
  if (plans.length === 0) {
    omitted['plans'] =
      'no legal plan: the two-compound rule needs two dry compounds with a measured wear ' +
      `rate and this weekend has ${feasible.length}.`;
  }
  if (!calibration && compounds.some((c) => c.feasible && c.pace === null)) {
    omitted['wear_calibration'] =
      `no stint at this circuit ran ${MIN_FIT_LAPS} clean laps while wearing at least 2% of ` +
      'a set, so there is no wear-to-time coefficient to lend the compounds that have a ' +
      'wear rate and no fit of their own. One long run on any dry compound calibrates all ' +
      'three.';
  }

  return {
    track_id: trackId,
    track_name: sessions[0]?.trackName ?? null,
    race_laps: laps,
    race_laps_source: source,
    wear_cliff_pct: WEAR_CLIFF_PCT,
    wear_calibration: calibration
      ? {
          ...calibrationStint(calibration),
          ms_per_wear_pct: calibration.msPerWearPct,
          assumption: CALIBRATION_ASSUMPTION
        }
      : null,
    compounds,
    plans: plans.slice(0, MAX_PLANS),
    plans_considered: plans.length,
    plans_ranking: ranking,
    plans_ranking_note: ranking ? RANKING_NOTES[ranking] : null,
    fuel: {
      kg_per_lap: FUEL_KG_PER_LAP,
      laps_measured: 16,
      evidence: 'race',
      session_ids: [fuelSessionId],
      race_laps: laps,
      margin_laps: FUEL_MARGIN_LAPS,
      slider_laps: Math.round((laps + FUEL_MARGIN_LAPS) * 100) / 100,
      recommended_kg: Math.round((laps + FUEL_MARGIN_LAPS) * FUEL_KG_PER_LAP * 100) / 100
    },
    pit_loss_s: pitLossS,
    pit_loss: measuredPitLoss
      ? {
          seconds: PIT_LOSS_S,
          source: 'measured',
          stops_measured: 1,
          stops: [
            {
              session_id: HEADLINE_SESSION_ID,
              stop_after_stint: 1,
              laps: [PIT_IN_LAP, PIT_OUT_LAP],
              loss_s: PIT_LOSS_S
            }
          ],
          detail:
            'median of 1 stop(s) measured at this circuit, each as the pit lap’s excess over ' +
            'the driver’s own clean-lap median in the same race'
        }
      : {
          seconds: DEFAULT_PIT_LOSS_S,
          source: 'default',
          stops_measured: 0,
          stops: [],
          detail:
            'no pit stop was recorded at this circuit this weekend, so a calendar-median ' +
            `${DEFAULT_PIT_LOSS_S.toFixed(0)} s is assumed. Every plan’s stop count depends ` +
            'on this number; drive a race with a stop in it and the sheet will measure it.'
        },
    sessions_used: sessionsUsed,
    omitted
  };
}

// ── career ───────────────────────────────────────────────────────────────────

/**
 * The career endpoints, derived rather than transcribed.
 *
 * Same rule as the strategy sheet: this is not a canned payload, it is the same
 * derivation the backend runs — weekends grouped from consecutive same-track
 * sessions, seasons incremented on a track repeat, the last race-type session
 * of a weekend treated as the grand prix, poles read off quali (5–9) and never
 * off a shootout (10–14), and totals summed from the same classifications the
 * weekends display. So every number the career page shows can be checked
 * against the weekends beside it, and the marked states — sprint format, a
 * pinned tag, a tag conflict, a race with no classification — are reachable by
 * having put those shapes in the session seeds rather than by faking a blob.
 */

const isRaceType = (t: number): boolean => t >= 15 && t <= 17;
const isQualiType = (t: number): boolean => t >= 5 && t <= 9;
const TIME_TRIAL_TYPE = 18;

/** The 48 h gap that splits two same-track runs into two visits. */
const WEEKEND_SPLIT_S = 48 * 3600;

/** Rows `f126 tag` would have written, keyed by session. Newest wins a conflict. */
interface CareerTagRow {
  sessionId: number;
  season: number;
  round: number | null;
  note: string | null;
  updatedAt: number;
}

/**
 * Two disagreeing tags on the season-2 Miami weekend: an older one left over
 * from an imported save, and a newer hand-written pin that agrees with the
 * derivation. The newest wins, and the weekend carries `tag_conflict` so the
 * page can say the older row is still sitting in the table.
 */
const CAREER_TAG_ROWS: readonly CareerTagRow[] = [
  {
    sessionId: MIAMI_S2_RACE_SESSION_ID,
    season: 1,
    round: 5,
    note: 'imported save, before the season split',
    updatedAt: BASE_WALL + 100_000
  },
  {
    sessionId: MIAMI_S2_QUALI_SESSION_ID,
    season: 2,
    round: 1,
    note: 'season 2 opener, pinned by hand',
    updatedAt: BASE_WALL + 200_000
  }
];

/** The derivation rules, restated in the payload as the contract requires. */
const CAREER_NOTES: Record<string, string> = {
  weekend_rule:
    'a weekend is a maximal chronological run of sessions at one circuit, split when ' +
    'consecutive sessions there are more than 48 h apart. Time trials are not career ' +
    'rounds; they still count for personal bests.',
  season_rule:
    'season 1 starts at the first recorded weekend and the season increments when a ' +
    'weekend begins at a circuit already visited that season. A tag written by f126 tag ' +
    'pins its weekend, and later weekends derive forward from the pinned value; ' +
    'disagreeing tags inside one weekend are resolved newest-first and flagged.'
};

/** Sorted copy of a session's numbers, for the quantile reads below. */
function quantileSorted(sorted: readonly number[], q: number): number {
  if (sorted.length === 0) return 0;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  const f = pos - lo;
  return (sorted[lo] ?? 0) * (1 - f) + (sorted[hi] ?? 0) * f;
}

/**
 * The representative-lap statistics for one session: valid, non-in/out laps
 * within 107 % of their median — the same rule the fact sheet states. The
 * fixture's only pit stop is the headline race's, so those are the only in/out
 * laps there are to drop.
 */
function consistencyOf(sessionId: number): CareerConsistency | null {
  const seed = seedFor(sessionId);
  if (!seed) return null;
  const inOut: ReadonlySet<number> =
    sessionId === HEADLINE_SESSION_ID ? new Set([PIT_IN_LAP, PIT_OUT_LAP]) : new Set();

  const clean: number[] = [];
  for (let lap = 1; lap <= seed.laps; lap++) {
    if (INVALID_LAPS.has(lap) || inOut.has(lap)) continue;
    clean.push(sessionLapTimeMs(sessionId, lap, PLAYER_CAR_INDEX));
  }
  if (clean.length === 0) {
    return { session_id: sessionId, laps_used: 0, median_ms: null, iqr_ms: null, cv_pct: null };
  }

  const preMedian = quantileSorted([...clean].sort((a, b) => a - b), 0.5);
  const used = clean.filter((t) => t <= preMedian * 1.07).sort((a, b) => a - b);
  const median = quantileSorted(used, 0.5);
  const iqr = quantileSorted(used, 0.75) - quantileSorted(used, 0.25);
  const mean = used.reduce((a, b) => a + b, 0) / used.length;
  const variance =
    used.length > 1
      ? used.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (used.length - 1)
      : 0;
  const cv = mean > 0 ? (Math.sqrt(variance) / mean) * 100 : 0;

  return {
    session_id: sessionId,
    laps_used: used.length,
    median_ms: Math.round(median),
    iqr_ms: Math.round(iqr),
    cv_pct: Math.round(cv * 100) / 100
  };
}

/** One derived weekend, still in seed form. */
interface WeekendSeeds {
  trackId: number;
  trackName: string;
  seeds: SessionSeed[];
  season: number;
  round: number;
  tag: CareerTag | null;
  tagConflict: boolean;
}

/**
 * Group the career sessions into weekends and number them.
 *
 * Weekend = maximal chronological run of sessions sharing `track_id`, split on
 * a gap over 48 h. Season increments when a weekend starts at a circuit already
 * visited that season; a tag pins its weekend's `(season, round)` and later
 * weekends derive forward from the pinned value.
 */
function careerWeekendSeeds(): WeekendSeeds[] {
  const considered = [...SESSION_SEEDS]
    .filter((s) => s.trackId >= 0 && s.type !== TIME_TRIAL_TYPE)
    .sort((a, b) => a.startedOffset - b.startedOffset);

  const weekends: WeekendSeeds[] = [];
  for (const seed of considered) {
    const current = weekends[weekends.length - 1];
    const previous = current?.seeds[current.seeds.length - 1];
    if (
      current !== undefined &&
      previous !== undefined &&
      current.trackId === seed.trackId &&
      seed.startedOffset - previous.startedOffset <= WEEKEND_SPLIT_S
    ) {
      current.seeds.push(seed);
    } else {
      weekends.push({
        trackId: seed.trackId,
        trackName: seed.trackName,
        seeds: [seed],
        season: 0,
        round: 0,
        tag: null,
        tagConflict: false
      });
    }
  }

  let season = 1;
  let round = 0;
  let visited = new Set<number>();
  for (const weekend of weekends) {
    let s = visited.has(weekend.trackId) ? season + 1 : season;
    let r = s === season ? round + 1 : 1;

    const tags = CAREER_TAG_ROWS.filter((t) =>
      weekend.seeds.some((seed) => seed.id === t.sessionId)
    ).sort((a, b) => a.updatedAt - b.updatedAt);
    const winner = tags[tags.length - 1];
    if (winner) {
      weekend.tag = { season: winner.season, round: winner.round, note: winner.note };
      weekend.tagConflict = tags.some(
        (t) => t.season !== winner.season || t.round !== winner.round
      );
      s = winner.season;
      if (winner.round !== null) r = winner.round;
      else if (s !== season) r = 1;
    }

    if (s !== season) visited = new Set();
    season = s;
    round = r;
    visited.add(weekend.trackId);
    weekend.season = s;
    weekend.round = r;
  }
  return weekends;
}

function qualiResultOf(seed: SessionSeed): CareerQualiResult {
  return {
    session_id: seed.id,
    position: seed.qualiPosition ?? null,
    best_lap_ms: bestLapOf(seed.id, seed)
  };
}

function sprintResultOf(seed: SessionSeed): CareerSprintResult {
  return {
    session_id: seed.id,
    position: seed.classification?.position ?? null,
    grid_position: seed.classification?.grid_position ?? null,
    points: seed.classification?.points ?? null,
    status: seed.classification?.status ?? null
  };
}

function raceResultOf(seed: SessionSeed): CareerRaceResult {
  return {
    session_id: seed.id,
    position: seed.classification?.position ?? null,
    grid_position: seed.classification?.grid_position ?? null,
    points: seed.classification?.points ?? null,
    pit_stops: seed.classification?.pit_stops ?? null,
    // The best lap comes from the recorded laps, not the classification, so it
    // survives a missing packet.
    best_lap_ms: bestLapOf(seed.id, seed),
    fastest_lap: seed.classification?.fastest_lap ?? null,
    status: seed.classification?.status ?? null
  };
}

function weekendPayload(w: WeekendSeeds): CareerWeekend {
  const raceSeeds = w.seeds.filter((s) => isRaceType(s.type));
  // More than one race-type session: the last is the grand prix, the earlier
  // ones are sprints, and the sprint column shows the last of those.
  const gp = raceSeeds[raceSeeds.length - 1];
  const sprint = raceSeeds.length > 1 ? raceSeeds[raceSeeds.length - 2] : undefined;
  const qualiSeeds = w.seeds.filter((s) => isQualiType(s.type));
  const quali = qualiSeeds[qualiSeeds.length - 1];

  // Classification points over race-type sessions, sprint included. A missing
  // classification makes the sum unknowable, and unknown is null — never zero.
  const points =
    raceSeeds.length === 0
      ? 0
      : raceSeeds.every((s) => s.classification)
        ? raceSeeds.reduce((sum, s) => sum + (s.classification?.points ?? 0), 0)
        : null;

  const first = w.seeds[0];
  const last = w.seeds[w.seeds.length - 1];

  return {
    season: w.season,
    round: w.round,
    track_id: w.trackId,
    track_name: w.trackName,
    format: raceSeeds.length > 1 ? 'sprint' : 'standard',
    started_at_wall: first ? BASE_WALL + first.startedOffset : null,
    ended_at_wall: last ? BASE_WALL + last.startedOffset + last.durationS : null,
    session_ids: w.seeds.map((s) => s.id),
    sessions: w.seeds.map((s) => ({
      id: s.id,
      session_type: s.type,
      session_type_name: s.typeName,
      started_at_wall: BASE_WALL + s.startedOffset,
      best_lap_ms: bestLapOf(s.id, s)
    })),
    quali: quali ? qualiResultOf(quali) : null,
    sprint: sprint ? sprintResultOf(sprint) : null,
    race: gp ? raceResultOf(gp) : null,
    points,
    consistency: gp ? consistencyOf(gp.id) : null,
    tags: w.tag,
    tag_conflict: w.tagConflict
  };
}

/**
 * Counting stats over a set of weekends. Wins, podiums and fastest laps read
 * the grand prix only; sprint wins are their own count; poles come off quali.
 * A null (unknown) never contributes — the total is the sum of what the
 * weekends actually show.
 */
function totalsOf(weekends: readonly CareerWeekend[]): CareerTotals {
  const totals: CareerTotals = {
    points: 0,
    wins: 0,
    podiums: 0,
    poles: 0,
    fastest_laps: 0,
    sprint_wins: 0,
    races: 0
  };
  for (const w of weekends) {
    if (w.points !== null) totals.points += w.points;
    if (w.race) {
      totals.races += 1;
      if (w.race.position === 1) totals.wins += 1;
      if (w.race.position !== null && w.race.position <= 3) totals.podiums += 1;
      if (w.race.fastest_lap === true) totals.fastest_laps += 1;
    }
    if (w.quali?.position === 1) totals.poles += 1;
    if (w.sprint?.position === 1) totals.sprint_wins += 1;
  }
  return totals;
}

/** A PB with the evidence of where it — and each of its sectors — was set. */
interface PbDetail {
  entry: CareerPb;
  sectors: CareerPbSectors;
}

/**
 * The circuit's personal best, read off every session there — time trials
 * included. The theoretical best is the sum of the circuit's best valid
 * sectors, each credited to the session and lap that set it.
 */
function pbFor(trackId: number): PbDetail | null {
  const seeds = SESSION_SEEDS.filter((s) => s.trackId === trackId).sort(
    (a, b) => a.startedOffset - b.startedOffset
  );

  let best: { time: number; seed: SessionSeed; row: Lap } | null = null;
  const sectorBest: Array<{ ms: number; sessionId: number; lap: number } | null> = [
    null,
    null,
    null
  ];
  let topSpeed: number | null = null;

  for (const seed of seeds) {
    for (const row of mockLaps(seed.id, PLAYER_CAR_INDEX)) {
      // The same validity rule bestLapOf applies, so the PB, the visit bests
      // and the session bests can never disagree with each other.
      if (INVALID_LAPS.has(row.lap_number)) continue;
      if (row.top_speed_kmh !== null && (topSpeed === null || row.top_speed_kmh > topSpeed)) {
        topSpeed = row.top_speed_kmh;
      }
      if (row.lap_time_ms !== null && (best === null || row.lap_time_ms < best.time)) {
        best = { time: row.lap_time_ms, seed, row };
      }
      const sectors = [row.s1_ms, row.s2_ms, row.s3_ms];
      sectors.forEach((ms, i) => {
        if (ms === null) return;
        const held = sectorBest[i];
        if (held === null || held === undefined || ms < held.ms) {
          sectorBest[i] = { ms, sessionId: seed.id, lap: row.lap_number };
        }
      });
    }
  }
  if (!best) return null;

  const [s1, s2, s3] = sectorBest;
  const theoretical =
    s1 && s2 && s3 ? s1.ms + s2.ms + s3.ms : null;

  return {
    entry: {
      track_id: trackId,
      track_name: best.seed.trackName,
      best_lap_ms: best.time,
      session_id: best.seed.id,
      session_label: `${best.seed.typeName} (session ${best.seed.id})`,
      lap_number: best.row.lap_number,
      compound_visual: best.row.compound_visual,
      compound_name: compoundLabel(best.row.compound_visual ?? -1),
      set_at_wall: best.row.wall_ts,
      theoretical_ms: theoretical,
      top_speed_kmh: topSpeed
    },
    sectors: {
      s1_ms: s1?.ms ?? null,
      s1_session_id: s1?.sessionId ?? null,
      s1_lap_number: s1?.lap ?? null,
      s2_ms: s2?.ms ?? null,
      s2_session_id: s2?.sessionId ?? null,
      s2_lap_number: s2?.lap ?? null,
      s3_ms: s3?.ms ?? null,
      s3_session_id: s3?.sessionId ?? null,
      s3_lap_number: s3?.lap ?? null
    }
  };
}

export function mockCareerOverview(): CareerOverview {
  const weekends = careerWeekendSeeds().map(weekendPayload);

  const seasonNumbers = [...new Set(weekends.map((w) => w.season))].sort((a, b) => a - b);
  const seasons: CareerSeason[] = seasonNumbers.map((n) => {
    const inSeason = weekends.filter((w) => w.season === n);
    return { season: n, rounds: inSeason.length, totals: totalsOf(inSeason), weekends: inSeason };
  });

  // PB board order is chronological first visit, not fastest-first.
  const firstVisit: number[] = [];
  for (const seed of [...SESSION_SEEDS].sort((a, b) => a.startedOffset - b.startedOffset)) {
    if (seed.trackId >= 0 && !firstVisit.includes(seed.trackId)) firstVisit.push(seed.trackId);
  }
  const pbs = firstVisit
    .map((t) => pbFor(t)?.entry)
    .filter((p): p is CareerPb => p !== undefined);

  return {
    seasons,
    career_totals: totalsOf(weekends),
    pbs,
    untracked_sessions: SESSION_SEEDS.filter(
      (s) => s.trackId < 0 || s.type === TIME_TRIAL_TYPE
    ).length,
    notes: CAREER_NOTES
  };
}

export function mockCareerTrack(trackId: number): CareerTrackResponse | MockFailure {
  const seeds = SESSION_SEEDS.filter((s) => s.trackId === trackId).sort(
    (a, b) => a.startedOffset - b.startedOffset
  );
  if (seeds.length === 0) {
    return { status: 404, detail: 'no sessions recorded at this track' };
  }

  const visits: CareerVisit[] = careerWeekendSeeds()
    .filter((w) => w.trackId === trackId)
    .map((w) => {
      const payload = weekendPayload(w);
      let best: number | null = null;
      let bestSession: number | null = null;
      for (const seed of w.seeds) {
        const b = bestLapOf(seed.id, seed);
        if (b !== null && (best === null || b < best)) {
          best = b;
          bestSession = seed.id;
        }
      }
      return {
        season: payload.season,
        round: payload.round,
        started_at_wall: payload.started_at_wall,
        session_ids: payload.session_ids,
        best_lap_ms: best,
        best_lap_session_id: bestSession,
        quali: payload.quali,
        sprint: payload.sprint,
        race: payload.race,
        consistency: payload.consistency
      };
    });

  const sessions: CareerTrackSession[] = seeds.map((seed) => {
    const consistency = consistencyOf(seed.id);
    let topSpeed: number | null = null;
    for (const row of mockLaps(seed.id, PLAYER_CAR_INDEX)) {
      if (row.top_speed_kmh !== null && (topSpeed === null || row.top_speed_kmh > topSpeed)) {
        topSpeed = row.top_speed_kmh;
      }
    }
    return {
      id: seed.id,
      session_type: seed.type,
      session_type_name: seed.typeName,
      started_at_wall: BASE_WALL + seed.startedOffset,
      best_lap_ms: bestLapOf(seed.id, seed),
      laps_total: seed.laps,
      laps_used: consistency?.laps_used ?? null,
      median_ms: consistency?.median_ms ?? null,
      iqr_ms: consistency?.iqr_ms ?? null,
      cv_pct: consistency?.cv_pct ?? null,
      top_speed_kmh: topSpeed
    };
  });

  const pb = pbFor(trackId);
  return {
    track_id: trackId,
    track_name: seeds[0]?.trackName ?? null,
    pb: pb ? { ...pb.entry, sectors: pb.sectors } : null,
    visits,
    sessions
  };
}

// ── transport interception ───────────────────────────────────────────────────

export interface MockResponse {
  status: number;
  body: unknown;
}

const num = (v: string | null): number | null => {
  if (v === null || !/^[0-9]{1,20}$/.test(v)) return null;
  const n = Number(v);
  return Number.isSafeInteger(n) ? n : null;
};

const failure = (v: unknown): v is MockFailure =>
  typeof v === 'object' && v !== null && 'status' in v && 'detail' in v;

const asResponse = (v: unknown): MockResponse =>
  failure(v) ? { status: v.status, body: { detail: v.detail } } : { status: 200, body: v };

/**
 * Route one API request. Returns null for anything that is not an analysis
 * endpoint, so the caller can fall through to the real network.
 *
 * Exported separately from the fetch patch so tests can exercise the routing
 * table without touching globals.
 */
export function handleMockRequest(rawUrl: string): MockResponse | null {
  const url = new URL(rawUrl, 'http://mock.local');
  const path = url.pathname;
  const p = url.searchParams;

  if (!path.startsWith('/api/')) return null;

  if (path === '/api/sessions') return { status: 200, body: mockSessions() };

  const detail = /^\/api\/sessions\/([0-9]{1,20})$/.exec(path);
  if (detail) {
    const found = mockSessionDetail(Number(detail[1]));
    return found
      ? { status: 200, body: found }
      : { status: 404, body: { detail: 'session not found' } };
  }

  const laps = /^\/api\/sessions\/([0-9]{1,20})\/laps$/.exec(path);
  if (laps) return { status: 200, body: mockLaps(Number(laps[1]), num(p.get('car_index'))) };

  const debrief = /^\/api\/sessions\/([0-9]{1,20})\/debrief$/.exec(path);
  if (debrief) {
    const found = mockDebrief(Number(debrief[1]));
    return found
      ? { status: 200, body: found }
      : { status: 404, body: { detail: 'no debrief for this session' } };
  }

  const telemetry = /^\/api\/sessions\/([0-9]{1,20})\/laps\/([0-9]{1,20})\/telemetry$/.exec(path);
  if (telemetry) {
    const found = mockTelemetry(
      Number(telemetry[1]),
      Number(telemetry[2]),
      num(p.get('car_index'))
    );
    return found
      ? { status: 200, body: found }
      : { status: 404, body: { detail: 'lap not found' } };
  }

  if (path === '/api/analysis/compare') {
    const sa = num(p.get('session_a'));
    const la = num(p.get('lap_a'));
    const sb = num(p.get('session_b'));
    const lb = num(p.get('lap_b'));
    if (sa === null || la === null || sb === null || lb === null) {
      return { status: 422, body: { detail: 'session_a, lap_a, session_b, lap_b are required' } };
    }
    return asResponse(mockCompare(sa, la, sb, lb));
  }

  if (path === '/api/analysis/corners') {
    const sid = num(p.get('session_id'));
    const lap = num(p.get('lap'));
    if (sid === null || lap === null) {
      return { status: 422, body: { detail: 'session_id and lap are required' } };
    }
    return asResponse(mockCorners(sid, lap, p.get('ref') ?? 'best'));
  }

  if (path === '/api/analysis/stints') {
    const sid = num(p.get('session_id'));
    if (sid === null) return { status: 422, body: { detail: 'session_id is required' } };
    return asResponse(mockStints(sid));
  }

  if (path === '/api/analysis/strategy') {
    const track = num(p.get('track_id'));
    if (track === null) return { status: 422, body: { detail: 'track_id is required' } };
    return asResponse(mockStrategy(track, num(p.get('race_laps'))));
  }

  if (path === '/api/career/overview') return { status: 200, body: mockCareerOverview() };

  const careerTrack = /^\/api\/career\/tracks\/([0-9]{1,20})$/.exec(path);
  if (careerTrack) return asResponse(mockCareerTrack(Number(careerTrack[1])));

  return { status: 404, body: { detail: 'not found' } };
}

export interface AnalysisMockOptions {
  /** Artificial latency, so loading states are visible while developing. */
  delayMs?: number;
  /**
   * Force every analysis endpoint to fail with this status. `?mock=1&fail=503`
   * is how the "database unavailable" panel gets exercised without a database
   * to take away.
   */
  failStatus?: number | null;
}

const FAIL_DETAIL: Record<number, string> = {
  404: 'session not found',
  422: 'invalid request',
  503: 'database unavailable'
};

/**
 * Answer `/api/*` from the simulation instead of the network.
 *
 * Patching `fetch` rather than injecting a client keeps the pages ignorant of
 * mock mode entirely — the same rule `mock.ts` follows for the WebSocket.
 * Returns a function that restores the original.
 */
export function installAnalysisMock(opts: AnalysisMockOptions = {}): () => void {
  const { delayMs = 0, failStatus = null } = opts;
  const original = globalThis.fetch;

  const patched: typeof fetch = async (input, init) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : (input as Request).url;

    const handled = handleMockRequest(url);
    if (handled === null) return original(input, init);

    if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));

    const result =
      failStatus === null
        ? handled
        : {
            status: failStatus,
            body: { detail: FAIL_DETAIL[failStatus] ?? 'request failed' }
          };

    return new Response(JSON.stringify(result.body), {
      status: result.status,
      headers: { 'content-type': 'application/json' }
    });
  };

  globalThis.fetch = patched;
  return () => {
    globalThis.fetch = original;
  };
}

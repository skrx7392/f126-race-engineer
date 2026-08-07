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
  CompareResponse,
  CompareSide,
  Corner,
  CornerKind,
  CornersResponse,
  Lap,
  LapTelemetry,
  Participant,
  SessionDetail,
  SessionSummary,
  Stint,
  StintLap,
  StintsResponse,
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

/** Wall-clock anchor. Fixed, so the fixture is byte-identical between runs. */
const BASE_WALL = 1_771_000_000;

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
}

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
    totalLaps: LAP_COUNT
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
    totalLaps: null
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
    totalLaps: null
  }
];

function seedFor(id: number): SessionSeed | null {
  return SESSION_SEEDS.find((s) => s.id === id) ?? null;
}

/**
 * Quali and Monza laps are a simple offset of the race model rather than their
 * own simulation. They exist to populate the session browser and the
 * cross-session lap picker, and nothing plots them in anger.
 */
function lapOffsetFor(sessionId: number): number {
  if (sessionId === QUALI_SESSION_ID) return -1_250;
  if (sessionId === OTHER_TRACK_SESSION_ID) return 8_400;
  return 0;
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

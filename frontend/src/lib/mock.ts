/**
 * Synthetic session generator.
 *
 * This is not a fixture file. It is a small simulation that emits the same
 * message types the backend does, at the same rates, through the same
 * `RaceStore.handleMessage()` entry point — so `?mock=1` exercises the real
 * data path, not a parallel one.
 *
 * What it models, because each drives something visible:
 *   - a lap-distance speed profile (corners), so throttle/brake/gear/rev-lights
 *     move the way they do on a real lap instead of jittering randomly;
 *   - 22 cars advancing at slightly different paces, so the tower reorders and
 *     intervals evolve;
 *   - tyre wear and thermal load, fuel burn, and ERS harvest/deploy tied to
 *     that same profile;
 *   - sector and lap events with real purple/green/yellow ranking against a
 *     running session best.
 *
 * Everything is driven by a seeded PRNG so a given seed replays identically,
 * which is what makes the monotonicity test meaningful.
 */

import type {
  FastFrame,
  ForecastSample,
  RaceEvent,
  ServerMessage,
  SessionInfo,
  SessionKind,
  SectorTriple,
  SlowFrame,
  TowerEntry,
  Wheels
} from './protocol';
import { PROTOCOL_VERSION } from './protocol';
import type { RaceStore } from './ws.svelte';

// ── deterministic randomness ─────────────────────────────────────────────────

/** mulberry32 — small, fast, good enough, and reproducible from a seed. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ── track model ──────────────────────────────────────────────────────────────

const TRACK_NAME = 'Suzuka';
const TRACK_ID = 3;
const TRACK_LENGTH_M = 5807;
const BASE_LAP_MS = 92_000;
const TOP_SPEED_KMH = 322;

/** Sector splits as a fraction of lap distance. */
const SECTOR_SPLITS = [0.335, 0.66];
/** Share of lap time per sector — Suzuka's S2 is the long one. */
const SECTOR_SHARE = [0.235, 0.395, 0.37];

/** Corner apexes: position around the lap, minimum speed, and approach width. */
const CORNERS: ReadonlyArray<{ at: number; v: number; w: number }> = [
  { at: 0.045, v: 128, w: 0.05 }, // Turn 1/2
  { at: 0.13, v: 175, w: 0.045 }, // Esses
  { at: 0.19, v: 168, w: 0.035 },
  { at: 0.27, v: 145, w: 0.045 }, // Degner
  { at: 0.35, v: 88, w: 0.05 }, // Hairpin
  { at: 0.47, v: 195, w: 0.05 }, // Spoon entry
  { at: 0.53, v: 130, w: 0.04 }, // Spoon
  { at: 0.78, v: 290, w: 0.04 }, // 130R
  { at: 0.93, v: 95, w: 0.055 } // Casio triangle
];

const smoothstep = (x: number): number => {
  const t = x < 0 ? 0 : x > 1 ? 1 : x;
  return t * t * (3 - 2 * t);
};

/** Shortest signed distance between two lap fractions, accounting for the wrap. */
function wrapDistance(a: number): number {
  const d = Math.abs(a) % 1;
  return d > 0.5 ? 1 - d : d;
}

/** Target speed (km/h) at a given fraction around the lap. */
export function speedAtFraction(frac: number): number {
  let v = TOP_SPEED_KMH;
  for (const c of CORNERS) {
    const d = wrapDistance(frac - c.at);
    if (d < c.w) {
      v = Math.min(v, c.v + (TOP_SPEED_KMH - c.v) * smoothstep(d / c.w));
    }
  }
  return v;
}

const GEAR_THRESHOLDS = [0, 85, 125, 165, 205, 245, 285, 310];

function gearFor(speedKmh: number): number {
  let g = 1;
  for (let i = 0; i < GEAR_THRESHOLDS.length; i++) {
    const t = GEAR_THRESHOLDS[i];
    if (t !== undefined && speedKmh >= t) g = i + 1;
  }
  return Math.min(8, g);
}

/** RPM from where the speed sits inside the current gear's band. */
function rpmFor(speedKmh: number, gear: number): number {
  const lo = GEAR_THRESHOLDS[gear - 1] ?? 0;
  const hi = GEAR_THRESHOLDS[gear] ?? TOP_SPEED_KMH;
  const span = Math.max(1, hi - lo);
  const within = Math.min(1, Math.max(0, (speedKmh - lo) / span));
  return Math.round(6_800 + within * 5_400);
}

// ── grid ─────────────────────────────────────────────────────────────────────

const AI_DRIVERS: ReadonlyArray<{ name: string; team: number }> = [
  { name: 'VERSTAPPEN', team: 2 },
  { name: 'NORRIS', team: 8 },
  { name: 'LECLERC', team: 1 },
  { name: 'PIASTRI', team: 8 },
  { name: 'SAINZ', team: 1 },
  { name: 'HAMILTON', team: 0 },
  { name: 'RUSSELL', team: 0 },
  { name: 'PEREZ', team: 2 },
  { name: 'ALONSO', team: 4 },
  { name: 'STROLL', team: 4 },
  { name: 'GASLY', team: 5 },
  { name: 'OCON', team: 5 },
  { name: 'ALBON', team: 3 },
  { name: 'SARGEANT', team: 3 },
  { name: 'TSUNODA', team: 6 },
  { name: 'RICCIARDO', team: 6 },
  { name: 'BOTTAS', team: 9 },
  { name: 'ZHOU', team: 9 },
  { name: 'MAGNUSSEN', team: 7 },
  { name: 'HULKENBERG', team: 7 },
  { name: 'LAWSON', team: 6 }
];

interface Car {
  index: number;
  name: string;
  teamId: number;
  isPlayer: boolean;
  /** Nominal lap time, ms. */
  pace: number;
  /** Laps completed, as a float. */
  distance: number;
  lapNumber: number;
  lastLapMs: number | null;
  bestLapMs: number | null;
  compound: number;
  tyreAge: number;
  pitStatus: number;
  penalties: number;
  resultStatus: number;
}

const PLAYER_INDEX = 0;
/**
 * Which grid slot the player starts in. Mid-field rather than pole: it is the
 * only starting position that exercises the focus band, the interval column and
 * the ahead/behind pace comparison all at once.
 */
const PLAYER_GRID_SLOT = 10;
/** Grid stagger, in laps, between consecutive slots. */
const GRID_GAP_LAPS = 0.004;

// ── engine ───────────────────────────────────────────────────────────────────

export interface MockOptions {
  kind?: SessionKind;
  seed?: number;
  /**
   * Simulated seconds to run through before the first frame is shown.
   *
   * Lap one of a race is the least interesting state the dashboard can be in —
   * no lap times, no wear, no pace history, nothing in the tower but the grid
   * order. Warping forward reaches a state with worn tyres, real pace data and
   * a used tyre set without waiting out a 92-second lap.
   */
  warpSeconds?: number;
}

export class MockEngine {
  readonly kind: SessionKind;
  #rand: () => number;

  /** Session clock, seconds. */
  #t = 0;
  #timeLeft: number;
  #cars: Car[] = [];

  // Player lap state
  #lapNumber = 1;
  #lapFraction = 0;
  #lapElapsedMs = 0;
  /** Target time for the lap in progress. */
  #lapTarget = BASE_LAP_MS;
  #refLapMs = BASE_LAP_MS;
  #bestLapMs: number | null = null;
  #lapValid = true;
  #sectorsDone: SectorTriple = [null, null, null];
  #bestLapSectors: SectorTriple = [null, null, null];
  #sessionBestSectors: SectorTriple = [null, null, null];

  // Car state
  #wear: Wheels = [0.4, 0.5, 0.3, 0.35];
  #surfaceTemp: Wheels = [88, 89, 86, 87];
  #fuelKg = 103;
  #burnLastLap = 1.9;
  #energyPct = 74;
  #deployMode = 2;
  #damage = {
    front_left_wing_pct: 0,
    front_right_wing_pct: 0,
    rear_wing_pct: 0,
    floor_pct: 0,
    diffuser_pct: 0,
    sidepod_pct: 0,
    gearbox_pct: 0,
    engine_pct: 0
  };
  #lapTimes: number[] = [];

  // Session state
  #flag = 0;
  #safetyCar = 0;
  #flagUntil = -1;
  #stalled = false;
  #weather = 1;
  #trackTemp = 31;
  #airTemp = 24;

  // Emission bookkeeping
  #slowAccumulatorMs = 0;
  #pending: RaceEvent[] = [];
  #pitLap: number;
  /** Grid offset that puts the player mid-field at the start of a race. */
  #playerStart: number;
  #flashbackDone = false;
  #stallDone = false;
  #stallUntil = -1;
  #parseErrors = 0;
  #kernelDrops = 0;

  constructor(opts: MockOptions = {}) {
    this.kind = opts.kind ?? 'race';
    this.#rand = mulberry32(opts.seed ?? 0xf1f1);
    this.#timeLeft = this.kind === 'race' ? 3600 : this.kind === 'practice' ? 3600 : 720;
    this.#pitLap = this.kind === 'race' ? 16 : -1;
    this.#playerStart = this.kind === 'race' ? -PLAYER_GRID_SLOT * GRID_GAP_LAPS : 0;
    this.#buildGrid();
    // Practice and qualifying start on low fuel; only the race carries a load.
    if (this.kind !== 'race') this.#fuelKg = 42;
  }

  #buildGrid(): void {
    const cars: Car[] = [
      {
        index: PLAYER_INDEX,
        name: 'YOU',
        teamId: 4,
        isPlayer: true,
        pace: BASE_LAP_MS,
        distance: 0,
        lapNumber: 1,
        lastLapMs: null,
        bestLapMs: null,
        compound: 17,
        tyreAge: 0,
        pitStatus: 0,
        penalties: 0,
        resultStatus: 2
      }
    ];
    const limit = this.kind === 'time_trial' ? 0 : AI_DRIVERS.length;
    for (let i = 0; i < limit; i++) {
      const d = AI_DRIVERS[i];
      if (!d) continue;
      // Front of the grid is quicker; spread is about 2.5 s across the field.
      const pace = BASE_LAP_MS - 900 + i * 120 + (this.#rand() - 0.5) * 260;
      // Slots run 0..21 with the player holding PLAYER_GRID_SLOT.
      const slot = i < PLAYER_GRID_SLOT ? i : i + 1;
      cars.push({
        index: i + 1,
        name: d.name,
        teamId: d.team,
        isPlayer: false,
        pace,
        // Qualifying has cars scattered on track; the race forms a grid.
        distance: this.kind === 'race' ? -slot * GRID_GAP_LAPS : this.#rand() * 0.9,
        lapNumber: 1,
        lastLapMs: null,
        bestLapMs: this.kind === 'race' ? null : Math.round(pace + this.#rand() * 400),
        compound: 17,
        tyreAge: 0,
        pitStatus: 0,
        penalties: 0,
        resultStatus: 2
      });
    }
    this.#cars = cars;
  }

  // ── stepping ───────────────────────────────────────────────────────────────

  /**
   * Advance the simulation and return the messages the backend would have sent.
   * Emits one `fast` per call, a `slow` each accumulated second, plus events.
   */
  advance(dtMs: number): ServerMessage[] {
    const dt = dtMs / 1000;
    this.#t += dt;
    if (this.#timeLeft > 0) this.#timeLeft = Math.max(0, this.#timeLeft - dt);

    this.#stepPlayer(dtMs);
    this.#stepField(dtMs);
    this.#stepSessionState();

    const out: ServerMessage[] = [this.#fastFrame()];

    this.#slowAccumulatorMs += dtMs;
    if (this.#slowAccumulatorMs >= 1000) {
      this.#slowAccumulatorMs -= 1000;
      out.push(this.#slowFrame());
    }

    if (this.#pending.length) {
      out.push(...this.#pending);
      this.#pending = [];
    }
    return out;
  }

  #stepPlayer(dtMs: number): void {
    const prevFraction = this.#lapFraction;
    this.#lapElapsedMs += dtMs;
    this.#lapFraction += dtMs / this.#lapTarget;

    // Sector crossings.
    for (let s = 0; s < SECTOR_SPLITS.length; s++) {
      const split = SECTOR_SPLITS[s];
      if (split !== undefined && prevFraction < split && this.#lapFraction >= split) {
        this.#completeSector(s);
      }
    }

    if (this.#lapFraction >= 1) {
      this.#completeLap();
      return;
    }

    // Wear, heat, fuel and energy all track the same lap profile.
    const frac = this.#lapFraction;
    const speed = speedAtFraction(frac);
    const load = 1 - (speed - 88) / (TOP_SPEED_KMH - 88);
    const dtLaps = dtMs / this.#lapTarget;

    const wearPerLap = [0.95, 1.02, 0.66, 0.71];
    for (let i = 0; i < 4; i++) {
      const rate = wearPerLap[i] ?? 0.8;
      this.#wear[i] = Math.min(100, (this.#wear[i] ?? 0) + rate * dtLaps * (0.7 + load));
      // Heat toward a load-dependent target; fronts run cooler than rears here.
      const target = 84 + load * 26 + (i < 2 ? 4 : 0) + (this.#weather >= 3 ? -12 : 0);
      const cur = this.#surfaceTemp[i] ?? 90;
      this.#surfaceTemp[i] = cur + (target - cur) * Math.min(1, dtMs / 900);
    }

    this.#fuelKg = Math.max(0, this.#fuelKg - this.#burnLastLap * dtLaps);

    // Harvest under braking, deploy on the power.
    const braking = this.#brakeAt(frac) > 0.15;
    this.#energyPct = Math.min(
      100,
      Math.max(0, this.#energyPct + (braking ? 9 : -3.4) * (dtMs / 1000))
    );
  }

  #brakeAt(frac: number): number {
    const here = speedAtFraction(frac);
    const ahead = speedAtFraction((frac + 0.004) % 1);
    return ahead < here ? Math.min(1, (here - ahead) / 26) : 0;
  }

  #throttleAt(frac: number): number {
    const here = speedAtFraction(frac);
    const ahead = speedAtFraction((frac + 0.004) % 1);
    if (ahead > here) return Math.min(1, 0.55 + (ahead - here) / 18);
    return this.#brakeAt(frac) > 0.15 ? 0 : 0.42;
  }

  /** Live delta: converges on the true lap-time difference, wobbling en route. */
  #deltaMs(): number | null {
    if (this.#refLapMs <= 0) return null;
    const frac = Math.min(1, this.#lapFraction);
    const trueDiff = this.#lapTarget - this.#refLapMs;
    const wobble = 340 * Math.sin(frac * Math.PI * 6.2) * (1 - frac);
    return Math.round(trueDiff * frac + wobble);
  }

  #completeSector(index: number): void {
    const share = SECTOR_SHARE[index] ?? 0.33;
    const jitter = 1 + (this.#rand() - 0.5) * 0.012;
    const timeMs = Math.round(this.#lapTarget * share * jitter);
    this.#sectorsDone[index] = timeMs;

    const sessionBest = this.#sessionBestSectors[index];
    const personalBest = this.#bestLapSectors[index];
    let color: 'purple' | 'green' | 'yellow';
    if (sessionBest === null || timeMs < sessionBest) {
      color = 'purple';
      this.#sessionBestSectors[index] = timeMs;
    } else if (personalBest === null || timeMs < personalBest) {
      color = 'green';
    } else {
      color = 'yellow';
    }

    this.#pending.push({
      type: 'event',
      t: this.#t,
      code: 'SECTOR',
      data: { sector: index + 1, time_ms: timeMs, color }
    });
  }

  #completeLap(): void {
    const lapMs = Math.round(this.#lapElapsedMs);
    const sectors: SectorTriple = [
      this.#sectorsDone[0],
      this.#sectorsDone[1],
      // S3 is whatever the lap did not spend in S1+S2.
      Math.max(1, lapMs - (this.#sectorsDone[0] ?? 0) - (this.#sectorsDone[1] ?? 0))
    ];

    const valid = this.#lapValid;
    let color: 'purple' | 'green' | 'yellow' = 'yellow';
    if (valid) {
      const fieldBest = Math.min(
        ...this.#cars.filter((c) => c.bestLapMs !== null).map((c) => c.bestLapMs as number),
        Number.POSITIVE_INFINITY
      );
      if (lapMs < fieldBest) {
        color = 'purple';
        this.#pending.push({
          type: 'event',
          t: this.#t,
          code: 'FASTEST_LAP',
          data: { car_index: PLAYER_INDEX, name: 'YOU', time_ms: lapMs }
        });
      } else if (this.#bestLapMs === null || lapMs < this.#bestLapMs) {
        color = 'green';
      }
      if (this.#bestLapMs === null || lapMs < this.#bestLapMs) {
        this.#bestLapMs = lapMs;
        this.#bestLapSectors = [...sectors] as SectorTriple;
        this.#refLapMs = lapMs;
      }
    }

    this.#pending.push({
      type: 'event',
      t: this.#t,
      code: 'LAP',
      data: { lap_number: this.#lapNumber, time_ms: lapMs, valid, color, sectors }
    });

    const player = this.#cars[PLAYER_INDEX];
    if (player) {
      player.lastLapMs = lapMs;
      player.lapNumber = this.#lapNumber + 1;
      player.tyreAge += 1;
      if (valid && (player.bestLapMs === null || lapMs < player.bestLapMs)) {
        player.bestLapMs = lapMs;
      }
    }

    this.#lapTimes.push(lapMs);
    if (this.#lapTimes.length > 3) this.#lapTimes.shift();
    this.#burnLastLap = 1.72 + this.#rand() * 0.3;

    // Roll the next lap.
    this.#lapNumber += 1;
    this.#lapFraction -= 1;
    this.#lapElapsedMs = this.#lapFraction * this.#lapTarget;
    this.#lapValid = true;
    this.#sectorsDone = [null, null, null];
    // Pace drifts with tyre wear and fuel; the wobble keeps the delta alive.
    const wearPenalty = ((this.#wear[0] ?? 0) + (this.#wear[1] ?? 0)) * 7;
    this.#lapTarget = BASE_LAP_MS + wearPenalty - (103 - this.#fuelKg) * 22 + (this.#rand() - 0.5) * 900;

    this.#handleLapMilestones();
  }

  #handleLapMilestones(): void {
    const lap = this.#lapNumber;

    // Scheduled stop.
    if (lap === this.#pitLap) {
      this.#pending.push({ type: 'event', t: this.#t, code: 'PIT_IN', data: { lap_number: lap } });
      const player = this.#cars[PLAYER_INDEX];
      if (player) {
        player.pitStatus = 1;
        player.compound = 18;
        player.tyreAge = 0;
      }
      this.#wear = [0.2, 0.2, 0.1, 0.15];
      this.#surfaceTemp = [72, 73, 70, 71];
    }
    if (lap === this.#pitLap + 1) {
      this.#pending.push({ type: 'event', t: this.#t, code: 'PIT_OUT', data: { lap_number: lap } });
      const player = this.#cars[PLAYER_INDEX];
      if (player) player.pitStatus = 0;
    }

    // An occasional track-limits deletion.
    if (this.#rand() < 0.09) {
      this.#lapValid = false;
      this.#pending.push({
        type: 'event',
        t: this.#t,
        code: 'LAP_INVALID',
        data: { lap_number: lap }
      });
    }

    // A yellow, then a safety car later on.
    if (lap === 8 && this.#flag === 0) {
      this.#flag = 3;
      this.#flagUntil = this.#t + 45;
      this.#pending.push({ type: 'event', t: this.#t, code: 'FLAG', data: { flag: 'yellow' } });
    }
    if (lap === 22 && this.#safetyCar === 0) {
      this.#safetyCar = 1;
      this.#flagUntil = this.#t + 120;
      this.#pending.push({ type: 'event', t: this.#t, code: 'SC', data: { status: 'deployed' } });
    }

    // Fire a flashback once so its distinct toast styling is reachable in a demo.
    if (lap === 5 && !this.#flashbackDone) {
      this.#flashbackDone = true;
      this.#pending.push({
        type: 'event',
        t: this.#t,
        code: 'FLASHBACK',
        data: { to_session_time_s: Math.max(0, this.#t - 12) }
      });
    }

    // Light contact damage, occasionally.
    if (this.#rand() < 0.07) {
      this.#damage.front_right_wing_pct = Math.min(
        60,
        this.#damage.front_right_wing_pct + Math.round(this.#rand() * 12)
      );
    }
  }

  #stepField(dtMs: number): void {
    for (const car of this.#cars) {
      if (car.isPlayer) {
        car.distance = this.#lapNumber - 1 + this.#lapFraction + this.#playerStart;
        continue;
      }
      // Slower under a safety car, and a little noise so gaps breathe.
      const scFactor = this.#safetyCar === 1 ? 1.4 : this.#safetyCar === 2 ? 1.15 : 1;
      const lapMs = car.pace * scFactor * (1 + (this.#rand() - 0.5) * 0.006);
      const before = car.distance;
      car.distance += dtMs / lapMs;
      if (Math.floor(car.distance) > Math.floor(before)) {
        car.lastLapMs = Math.round(lapMs);
        car.lapNumber = Math.max(1, Math.floor(car.distance) + 1);
        car.tyreAge += 1;
        if (car.bestLapMs === null || lapMs < car.bestLapMs) car.bestLapMs = Math.round(lapMs);
      }
    }
  }

  #stepSessionState(): void {
    if (this.#flagUntil > 0 && this.#t >= this.#flagUntil) {
      if (this.#safetyCar === 1) {
        this.#safetyCar = 0;
        this.#pending.push({ type: 'event', t: this.#t, code: 'SC', data: { status: 'in' } });
      }
      if (this.#flag === 3) {
        this.#pending.push({ type: 'event', t: this.#t, code: 'FLAG', data: { flag: 'clear' } });
      }
      this.#flag = 0;
      this.#flagUntil = -1;
    }

    // A single brief capture dropout, well into the session, so the stalled
    // surface is reachable without spoiling an early screenshot. Scheduled in
    // simulation time rather than with a timer, so `advance()` stays pure with
    // respect to the wall clock and replays identically under test.
    if (!this.#stallDone && this.#t > 240) {
      this.#stallDone = true;
      this.#stalled = true;
      this.#stallUntil = this.#t + 6;
      this.#pending.push({
        type: 'event',
        t: this.#t,
        code: 'STALLED',
        data: { stalled: true }
      });
    }

    if (this.#stalled && this.#stallUntil > 0 && this.#t >= this.#stallUntil) {
      this.#stalled = false;
      this.#stallUntil = -1;
      this.#pending.push({
        type: 'event',
        t: this.#t,
        code: 'STALLED',
        data: { stalled: false }
      });
    }
  }

  // ── frame construction ─────────────────────────────────────────────────────

  #fastFrame(): FastFrame {
    const frac = Math.min(0.9999, Math.max(0, this.#lapFraction));
    const speed = speedAtFraction(frac);
    const gear = gearFor(speed);
    const rpm = rpmFor(speed, gear);
    return {
      type: 'fast',
      t: this.#t,
      speed_kmh: Math.round(speed * 10) / 10,
      gear,
      rpm,
      throttle: Math.round(this.#throttleAt(frac) * 100) / 100,
      brake: Math.round(this.#brakeAt(frac) * 100) / 100,
      steer: Math.round(Math.sin(frac * Math.PI * 9) * 0.6 * 100) / 100,
      drs_open: frac > 0.86 && frac < 0.95,
      aero_mode: null,
      ers_deploy_mode: this.#deployMode,
      rev_lights_percent: Math.round(Math.min(100, Math.max(0, ((rpm - 8_600) / 3_900) * 100))),
      lap_number: this.#lapNumber,
      lap_distance_m: Math.round(frac * TRACK_LENGTH_M * 10) / 10,
      current_lap_ms: Math.round(this.#lapElapsedMs),
      delta_best_ms: this.#deltaMs(),
      delta_kind: this.kind === 'race' ? 'race_best' : this.kind === 'time_trial' ? 'personal_best' : 'session_best'
    };
  }

  #tower(): TowerEntry[] {
    const ranked = [...this.#cars];
    if (this.kind === 'race') {
      ranked.sort((a, b) => b.distance - a.distance);
    } else {
      ranked.sort((a, b) => (a.bestLapMs ?? Infinity) - (b.bestLapMs ?? Infinity));
    }

    const leader = ranked[0];
    return ranked.map((car, i) => {
      const ahead = ranked[i - 1];
      let gapAhead: number | null = null;
      let gapLeader: number | null = null;

      if (this.kind === 'race') {
        if (ahead) gapAhead = Math.round((ahead.distance - car.distance) * car.pace);
        if (leader) gapLeader = Math.round((leader.distance - car.distance) * car.pace);
      } else {
        if (ahead?.bestLapMs != null && car.bestLapMs != null) {
          gapAhead = car.bestLapMs - ahead.bestLapMs;
        }
        if (leader?.bestLapMs != null && car.bestLapMs != null) {
          gapLeader = car.bestLapMs - leader.bestLapMs;
        }
      }

      return {
        car_index: car.index,
        position: i + 1,
        name: car.name,
        team_id: car.teamId,
        is_player: car.isPlayer,
        lap_number: car.lapNumber,
        last_lap_ms: car.lastLapMs,
        gap_ahead_ms: i === 0 ? null : gapAhead,
        gap_leader_ms: i === 0 ? 0 : gapLeader,
        compound_visual: car.compound,
        tyre_age_laps: car.tyreAge,
        pit_status: car.pitStatus,
        penalties_s: car.penalties,
        result_status: car.resultStatus
      };
    });
  }

  #forecast(): ForecastSample[] {
    return [
      { offset_min: 5, weather: this.#weather, rain_pct: 10 },
      { offset_min: 15, weather: 2, rain_pct: 35 },
      { offset_min: 30, weather: 3, rain_pct: 60 }
    ];
  }

  sessionInfo(): SessionInfo {
    return {
      session_uid: '1846273615049283746',
      segment: 0,
      packet_format: 2026,
      session_type: this.kind === 'race' ? 10 : this.kind === 'quali' ? 6 : 1,
      session_kind: this.kind,
      track_id: TRACK_ID,
      track_name: TRACK_NAME,
      time_left_s: this.kind === 'time_trial' ? null : Math.round(this.#timeLeft),
      duration_s: this.kind === 'time_trial' ? null : 3600,
      total_laps: this.kind === 'race' ? 53 : null,
      safety_car: this.#safetyCar,
      fia_flag: this.#flag,
      weather: this.#weather,
      track_temp_c: this.#trackTemp,
      air_temp_c: this.#airTemp,
      forecast: this.#forecast(),
      stalled: this.#stalled,
      joined_in_progress: false
    };
  }

  #slowFrame(): SlowFrame {
    const paceAvg =
      this.#lapTimes.length > 0
        ? Math.round(this.#lapTimes.reduce((a, b) => a + b, 0) / this.#lapTimes.length)
        : null;

    const tower = this.#tower();
    const playerPos = tower.findIndex((r) => r.is_player);
    const aheadCar = playerPos > 0 ? tower[playerPos - 1] : undefined;
    const behindCar = playerPos >= 0 && playerPos < tower.length - 1 ? tower[playerPos + 1] : undefined;

    const round1 = (n: number): number => Math.round(n * 10) / 10;
    const wear = this.#wear.map(round1) as Wheels;
    const surface = this.#surfaceTemp.map((v) => Math.round(v)) as Wheels;

    return {
      type: 'slow',
      session: this.sessionInfo(),
      tower,
      tyres: {
        surface_temp_c: surface,
        inner_temp_c: surface.map((v) => v + 5) as Wheels,
        pressure_psi: [21.5, 21.6, 23.0, 23.1],
        wear_pct: wear,
        wear_rate_pct_per_lap: this.#lapNumber > 2 ? [0.95, 1.02, 0.66, 0.71] : null,
        projected_wear_end_pct:
          this.kind === 'race' && this.#lapNumber > 2
            ? (wear.map((w, i) => round1(w + (53 - this.#lapNumber) * [0.95, 1.02, 0.66, 0.71][i]!)) as Wheels)
            : null,
        compound_actual: this.#cars[PLAYER_INDEX]?.compound === 18 ? 18 : 17,
        compound_visual: this.#cars[PLAYER_INDEX]?.compound ?? 17,
        age_laps: this.#cars[PLAYER_INDEX]?.tyreAge ?? 0
      },
      fuel: {
        in_tank_kg: round1(this.#fuelKg),
        remaining_laps: round1(this.#fuelKg / Math.max(0.1, this.#burnLastLap)),
        laps_left_in_session: this.kind === 'race' ? Math.max(0, 53 - this.#lapNumber + 1) : null,
        delta_laps:
          this.kind === 'race'
            ? round1(this.#fuelKg / Math.max(0.1, this.#burnLastLap) - (53 - this.#lapNumber + 1))
            : null,
        burn_last_lap_kg: round1(this.#burnLastLap)
      },
      energy: {
        store_j: Math.round(this.#energyPct * 40_000),
        store_pct: round1(this.#energyPct),
        deploy_mode: this.#deployMode,
        harvested_lap_j: null,
        deployed_lap_j: null
      },
      damage: { ...this.#damage },
      pace: {
        last_3_avg_ms: paceAvg,
        ahead_last_3_avg_ms: aheadCar?.last_lap_ms ?? null,
        behind_last_3_avg_ms: behindCar?.last_lap_ms ?? null
      },
      sectors: {
        current_lap: [...this.#sectorsDone] as SectorTriple,
        best_lap: [...this.#bestLapSectors] as SectorTriple,
        session_best: [...this.#sessionBestSectors] as SectorTriple,
        last_lap_valid: this.#lapValid
      },
      timetrial:
        this.kind === 'time_trial'
          ? {
              pb_ms: this.#bestLapMs ?? 91_200,
              rival_ms: 91_550,
              pb_sectors: this.#bestLapSectors[0] !== null ? this.#bestLapSectors : [20_900, 35_200, 35_100],
              rival_sectors: [21_000, 35_300, 35_250]
            }
          : null,
      health: {
        packets_per_sec: this.#stalled ? 0 : 480,
        parse_errors_total: this.#parseErrors,
        kernel_drops_total: this.#kernelDrops,
        last_packet_age_ms: this.#stalled ? 6_200 : 40,
        ws_clients: 1
      }
    };
  }

  snapshot(): ServerMessage {
    return {
      type: 'snapshot',
      protocol_version: PROTOCOL_VERSION,
      session: this.sessionInfo(),
      fast: this.#fastFrame(),
      slow: this.#slowFrame(),
      recent_events: []
    };
  }
}

// ── driver ───────────────────────────────────────────────────────────────────

/** Tick interval, matching the backend's 10 Hz fast channel. */
const TICK_MS = 100;

/**
 * Start feeding `store` from a synthetic session. Returns a stop function.
 */
export function startMock(store: RaceStore, opts: MockOptions = {}): () => void {
  const engine = new MockEngine(opts);

  // Run the warp period at the real tick rate but discard its messages: the
  // snapshot below carries the resulting state, exactly as a client joining a
  // race in progress would receive it.
  const warpTicks = Math.max(0, Math.round((opts.warpSeconds ?? 0) * 10));
  for (let i = 0; i < warpTicks; i++) engine.advance(TICK_MS);

  store.reset();
  store.socketState = 'open';
  store.handleMessage(engine.snapshot());

  const timer = setInterval(() => {
    for (const msg of engine.advance(TICK_MS)) store.handleMessage(msg);
  }, TICK_MS);

  return () => {
    clearInterval(timer);
    store.socketState = 'closed';
  };
}

const VALID_KINDS: readonly SessionKind[] = ['race', 'quali', 'practice', 'time_trial'];

/**
 * Decide whether to run mock mode, and in which session kind.
 *
 * Enabled by `npm run dev:mock` (which sets `VITE_MOCK`) or by `?mock=...` on
 * any build. `?mock=1` implies a race; `?mock=quali` picks the layout directly,
 * which is how the layout screenshots are taken.
 */
export function mockRequest(search?: string): {
  enabled: boolean;
  kind: SessionKind;
  warpSeconds: number;
} {
  const envFlag = import.meta.env['VITE_MOCK'];
  let enabled = envFlag === '1' || envFlag === 'true';
  let kind: SessionKind = 'race';

  const query = search ?? (typeof location === 'undefined' ? '' : location.search);
  const params = new URLSearchParams(query);

  const raw = params.get('mock');
  if (raw !== null) {
    if (raw === '0' || raw === 'false') return { enabled: false, kind, warpSeconds: 0 };
    enabled = true;
    const match = VALID_KINDS.find((k) => k === raw);
    if (match) kind = match;
  }

  // `?warp=600` jumps 10 minutes into the session before the first frame.
  // Capped so a typo cannot lock the tab up in a simulation loop.
  const warpRaw = Number(params.get('warp'));
  const warpSeconds =
    Number.isFinite(warpRaw) && warpRaw > 0 ? Math.min(warpRaw, 3_600) : 0;

  return { enabled, kind, warpSeconds };
}

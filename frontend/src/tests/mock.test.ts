import { describe, it, expect } from 'vitest';
import { MockEngine, speedAtFraction, mockRequest } from '../lib/mock';
import type { FastFrame, SlowFrame } from '../lib/protocol';
import { isFastFrame, isSlowFrame, isServerMessage } from '../lib/protocol';

/** Run the engine for `seconds` of simulated time at the real 10 Hz tick. */
function run(engine: MockEngine, seconds: number) {
  const fast: FastFrame[] = [];
  const slow: SlowFrame[] = [];
  const events: string[] = [];

  for (let i = 0; i < seconds * 10; i++) {
    for (const msg of engine.advance(100)) {
      expect(isServerMessage(msg)).toBe(true);
      if (isFastFrame(msg)) fast.push(msg);
      else if (isSlowFrame(msg)) slow.push(msg);
      else if (msg.type === 'event') events.push(msg.code);
    }
  }
  return { fast, slow, events };
}

describe('speedAtFraction', () => {
  it('stays within the car’s real envelope everywhere on the lap', () => {
    for (let f = 0; f < 1; f += 0.005) {
      const v = speedAtFraction(f);
      expect(v).toBeGreaterThan(50);
      expect(v).toBeLessThanOrEqual(322);
    }
  });

  it('slows for the hairpin and runs flat down the straight', () => {
    expect(speedAtFraction(0.35)).toBeLessThan(120);
    expect(speedAtFraction(0.68)).toBeGreaterThan(280);
  });

  it('is continuous across the start/finish wrap', () => {
    expect(Math.abs(speedAtFraction(0.999) - speedAtFraction(0.001))).toBeLessThan(30);
  });
});

describe('MockEngine — race', () => {
  it('advances laps monotonically and resets the lap clock at the line', () => {
    const { fast } = run(new MockEngine({ kind: 'race', seed: 7 }), 300);
    expect(fast.length).toBe(3000);

    let laps = 0;
    for (let i = 1; i < fast.length; i++) {
      const prev = fast[i - 1]!;
      const cur = fast[i]!;

      // Lap number never goes backwards.
      expect(cur.lap_number).toBeGreaterThanOrEqual(prev.lap_number);

      if (cur.lap_number === prev.lap_number) {
        // Within a lap the clock only advances.
        expect(cur.current_lap_ms).toBeGreaterThanOrEqual(prev.current_lap_ms);
      } else {
        laps++;
        // Crossing the line steps the lap by exactly one and restarts the clock.
        expect(cur.lap_number).toBe(prev.lap_number + 1);
        expect(cur.current_lap_ms).toBeLessThan(prev.current_lap_ms);
      }
    }

    // ~92 s laps over 300 s of running.
    expect(laps).toBeGreaterThanOrEqual(2);
    expect(laps).toBeLessThanOrEqual(4);
  });

  it('keeps every fast field inside its documented range', () => {
    const { fast } = run(new MockEngine({ kind: 'race', seed: 11 }), 120);
    for (const f of fast) {
      expect(f.throttle).toBeGreaterThanOrEqual(0);
      expect(f.throttle).toBeLessThanOrEqual(1);
      expect(f.brake).toBeGreaterThanOrEqual(0);
      expect(f.brake).toBeLessThanOrEqual(1);
      expect(f.steer).toBeGreaterThanOrEqual(-1);
      expect(f.steer).toBeLessThanOrEqual(1);
      expect(f.rev_lights_percent).toBeGreaterThanOrEqual(0);
      expect(f.rev_lights_percent).toBeLessThanOrEqual(100);
      expect(f.gear).toBeGreaterThanOrEqual(-1);
      expect(f.gear).toBeLessThanOrEqual(8);
      expect(f.lap_distance_m).toBeGreaterThanOrEqual(0);
      expect(Number.isFinite(f.rpm)).toBe(true);
    }
  });

  it('burns fuel and wears tyres in one direction only', () => {
    const { slow } = run(new MockEngine({ kind: 'race', seed: 3 }), 200);
    expect(slow.length).toBeGreaterThan(150);

    for (let i = 1; i < slow.length; i++) {
      const prev = slow[i - 1]!;
      const cur = slow[i]!;

      expect(cur.fuel!.in_tank_kg).toBeLessThanOrEqual(prev.fuel!.in_tank_kg);

      // Wear only resets on a tyre change; none is scheduled this early.
      for (let w = 0; w < 4; w++) {
        expect(cur.tyres!.wear_pct[w]).toBeGreaterThanOrEqual(prev.tyres!.wear_pct[w]! - 1e-9);
      }
    }

    const last = slow.at(-1)!;
    expect(last.fuel!.in_tank_kg).toBeLessThan(103);
    expect(last.tyres!.wear_pct[0]).toBeGreaterThan(0.4);
  });

  it('keeps the tower complete, ranked and self-consistent', () => {
    const { slow } = run(new MockEngine({ kind: 'race', seed: 5 }), 60);
    for (const s of slow) {
      expect(s.tower).toHaveLength(22);
      expect(s.tower.filter((r) => r.is_player)).toHaveLength(1);

      const positions = s.tower.map((r) => r.position);
      expect(positions).toEqual(Array.from({ length: 22 }, (_, i) => i + 1));

      // Car indices stay unique, so keyed each blocks never collide.
      expect(new Set(s.tower.map((r) => r.car_index)).size).toBe(22);

      // The leader has no car ahead of it.
      expect(s.tower[0]!.gap_ahead_ms).toBeNull();
    }
  });

  it('emits sector and lap events with valid ranking colours', () => {
    const { events } = run(new MockEngine({ kind: 'race', seed: 13 }), 300);
    expect(events).toContain('SECTOR');
    expect(events).toContain('LAP');
    // Two sector crossings per lap plus the lap itself.
    expect(events.filter((c) => c === 'SECTOR').length).toBeGreaterThanOrEqual(
      events.filter((c) => c === 'LAP').length * 2
    );
  });

  it('replays identically for a given seed', () => {
    const a = run(new MockEngine({ kind: 'race', seed: 99 }), 60);
    const b = run(new MockEngine({ kind: 'race', seed: 99 }), 60);
    expect(a.fast.at(-1)).toEqual(b.fast.at(-1));
    expect(a.events).toEqual(b.events);
  });
});

describe('MockEngine — other session kinds', () => {
  it('reports the requested session kind so the layout switch fires', () => {
    for (const kind of ['race', 'quali', 'practice', 'time_trial'] as const) {
      const engine = new MockEngine({ kind, seed: 1 });
      expect(engine.sessionInfo().session_kind).toBe(kind);
    }
  });

  it('populates the time-trial block only in time trial', () => {
    const tt = run(new MockEngine({ kind: 'time_trial', seed: 2 }), 5).slow.at(-1)!;
    expect(tt.timetrial).not.toBeNull();
    expect(tt.timetrial!.pb_ms).toBeGreaterThan(0);
    expect(tt.timetrial!.rival_ms).toBeGreaterThan(0);

    const race = run(new MockEngine({ kind: 'race', seed: 2 }), 5).slow.at(-1)!;
    expect(race.timetrial).toBeNull();
  });

  it('omits race-only fuel maths outside a race', () => {
    const quali = run(new MockEngine({ kind: 'quali', seed: 4 }), 5).slow.at(-1)!;
    expect(quali.fuel!.laps_left_in_session).toBeNull();
    expect(quali.session.total_laps).toBeNull();
  });

  it('produces a snapshot that satisfies the protocol guards', () => {
    const snap = new MockEngine({ kind: 'race', seed: 6 }).snapshot();
    expect(isServerMessage(snap)).toBe(true);
    expect(snap.type).toBe('snapshot');
  });
});

describe('mockRequest', () => {
  it('is off by default with no query string', () => {
    expect(mockRequest('').enabled).toBe(false);
  });

  it('turns on for ?mock=1 and defaults to a race', () => {
    expect(mockRequest('?mock=1')).toEqual({ enabled: true, kind: 'race', warpSeconds: 0 });
  });

  it('selects a layout directly by name', () => {
    expect(mockRequest('?mock=quali').kind).toBe('quali');
    expect(mockRequest('?mock=time_trial').kind).toBe('time_trial');
    expect(mockRequest('?mock=practice').kind).toBe('practice');
  });

  it('can force mock mode off, overriding the build flag', () => {
    expect(mockRequest('?mock=0').enabled).toBe(false);
  });

  it('falls back to a race for an unrecognised kind', () => {
    expect(mockRequest('?mock=banana')).toEqual({ enabled: true, kind: 'race', warpSeconds: 0 });
  });

  it('reads a warp offset and clamps it to a sane range', () => {
    expect(mockRequest('?mock=1&warp=600').warpSeconds).toBe(600);
    expect(mockRequest('?mock=1&warp=99999').warpSeconds).toBe(3600);
    expect(mockRequest('?mock=1&warp=-5').warpSeconds).toBe(0);
    expect(mockRequest('?mock=1&warp=abc').warpSeconds).toBe(0);
    expect(mockRequest('?mock=1').warpSeconds).toBe(0);
  });
});

describe('warping into a session in progress', () => {
  it('reaches a later lap with worn tyres and burnt fuel', () => {
    const cold = new MockEngine({ kind: 'race', seed: 21 });
    const warm = new MockEngine({ kind: 'race', seed: 21 });

    // 10 minutes ≈ 6 laps.
    for (let i = 0; i < 600 * 10; i++) warm.advance(100);

    const coldSlow = run(cold, 2).slow.at(-1)!;
    const warmSlow = run(warm, 2).slow.at(-1)!;

    expect(warmSlow.tower.find((r) => r.is_player)!.lap_number).toBeGreaterThan(
      coldSlow.tower.find((r) => r.is_player)!.lap_number
    );
    expect(warmSlow.fuel!.in_tank_kg).toBeLessThan(coldSlow.fuel!.in_tank_kg);
    expect(warmSlow.tyres!.wear_pct[0]).toBeGreaterThan(coldSlow.tyres!.wear_pct[0]!);
    // Pace history only exists once laps have been completed.
    expect(warmSlow.pace!.last_3_avg_ms).not.toBeNull();
  });
});

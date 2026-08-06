import { describe, it, expect } from 'vitest';
import { smooth, DEFAULT_HALF_LIFE_MS } from '../lib/tween';

describe('smooth', () => {
  it('halves the remaining distance in exactly one half-life', () => {
    expect(smooth(0, 1, DEFAULT_HALF_LIFE_MS)).toBeCloseTo(0.5, 10);
    expect(smooth(0, 1, DEFAULT_HALF_LIFE_MS * 2)).toBeCloseTo(0.75, 10);
    expect(smooth(0, 1, DEFAULT_HALF_LIFE_MS * 3)).toBeCloseTo(0.875, 10);
  });

  it('is frame-rate independent: one big step equals several small ones', () => {
    const target = 1;
    const oneStep = smooth(0, target, 48);

    let split = 0;
    for (let i = 0; i < 3; i++) split = smooth(split, target, 16);

    expect(split).toBeCloseTo(oneStep, 10);
  });

  it('reaches ~95% within the ~100 ms the protocol asks for', () => {
    const v = smooth(0, 1, 100);
    expect(v).toBeGreaterThan(0.9);
    expect(v).toBeLessThan(1);
  });

  it('never overshoots, in either direction', () => {
    for (const dt of [1, 16, 100, 1000, 10_000]) {
      const up = smooth(0, 1, dt);
      expect(up).toBeGreaterThanOrEqual(0);
      expect(up).toBeLessThanOrEqual(1);

      const down = smooth(1, 0, dt);
      expect(down).toBeGreaterThanOrEqual(0);
      expect(down).toBeLessThanOrEqual(1);
    }
  });

  it('approaches but never passes the target', () => {
    let v = 0;
    for (let i = 0; i < 500; i++) {
      const next = smooth(v, 1, 16);
      expect(next).toBeGreaterThanOrEqual(v);
      expect(next).toBeLessThanOrEqual(1);
      v = next;
    }
    expect(v).toBeCloseTo(1, 6);
  });

  it('is a no-op when already at the target', () => {
    expect(smooth(0.42, 0.42, 16)).toBe(0.42);
  });

  it('holds still for a non-positive time step', () => {
    expect(smooth(0.3, 1, 0)).toBe(0.3);
    expect(smooth(0.3, 1, -16)).toBe(0.3);
  });

  it('snaps rather than stalling when the half-life is zero', () => {
    expect(smooth(0, 1, 16, 0)).toBe(1);
  });

  it('recovers from a non-finite current value instead of propagating NaN', () => {
    expect(smooth(NaN, 0.5, 16)).toBe(0.5);
    expect(smooth(Infinity, 0.5, 16)).toBe(0.5);
  });

  it('ignores a non-finite target rather than corrupting the current value', () => {
    expect(smooth(0.5, NaN, 16)).toBe(0.5);
  });

  it('handles negative ranges, as the delta bar requires', () => {
    const v = smooth(-1, 1, DEFAULT_HALF_LIFE_MS);
    expect(v).toBeCloseTo(0, 10);
  });
});

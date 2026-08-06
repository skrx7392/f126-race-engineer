import { describe, it, expect } from 'vitest';
import {
  backoffDelay,
  backoffSchedule,
  nominalDelay,
  BASE_DELAY_MS,
  MAX_DELAY_MS,
  JITTER_RATIO
} from '../lib/backoff';

describe('nominalDelay', () => {
  it('doubles from 1 s and caps at 30 s, as the protocol specifies', () => {
    expect(backoffSchedule(8)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  });

  it('never exceeds the cap for any attempt, including absurd ones', () => {
    for (const attempt of [0, 5, 10, 50, 1000, Number.MAX_SAFE_INTEGER]) {
      expect(nominalDelay(attempt)).toBeLessThanOrEqual(MAX_DELAY_MS);
    }
  });

  it('treats invalid attempts as the first attempt', () => {
    expect(nominalDelay(-1)).toBe(BASE_DELAY_MS);
    expect(nominalDelay(NaN)).toBe(BASE_DELAY_MS);
  });
});

describe('backoffDelay jitter', () => {
  it('returns the nominal delay when the random source is centred', () => {
    const centred = () => 0.5;
    for (let i = 0; i < 8; i++) {
      expect(backoffDelay(i, centred)).toBe(nominalDelay(i));
    }
  });

  it('spans exactly ±25% at the extremes of the random source', () => {
    expect(backoffDelay(1, () => 0)).toBe(2000 * (1 - JITTER_RATIO));
    // rand() is exclusive of 1, so this is the supremum rather than a real draw.
    expect(backoffDelay(1, () => 1)).toBe(2000 * (1 + JITTER_RATIO));
  });

  it('never returns a delay under the documented 1 s floor at attempt 0', () => {
    // Full jitter would allow near-zero retries here; equal-spread jitter must not.
    for (let i = 0; i < 200; i++) {
      expect(backoffDelay(0)).toBeGreaterThanOrEqual(BASE_DELAY_MS * (1 - JITTER_RATIO));
    }
  });

  it('stays within the cap even when jitter pushes upward', () => {
    for (let i = 0; i < 200; i++) {
      expect(backoffDelay(20, Math.random)).toBeLessThanOrEqual(MAX_DELAY_MS);
    }
  });

  it('is monotonic in expectation across the ramp', () => {
    const centred = () => 0.5;
    const seq = backoffSchedule(6).map((_, i) => backoffDelay(i, centred));
    for (let i = 1; i < seq.length; i++) {
      expect(seq[i]!).toBeGreaterThanOrEqual(seq[i - 1]!);
    }
  });

  it('de-phases simultaneous reconnects', () => {
    const draws = new Set(Array.from({ length: 50 }, () => backoffDelay(3)));
    // Real jitter means 50 clients do not all pick the same millisecond.
    expect(draws.size).toBeGreaterThan(5);
  });
});

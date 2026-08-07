import { describe, it, expect } from 'vitest';
import { displayStintRanges } from '../lib/analysis-format';

/**
 * The stint strip is read as "how long was I on each tyre", so its ranges have
 * to behave like a partition of the race: contiguous, non-overlapping, and
 * summing to the laps actually run. The recorder stores the pit lap as both the
 * end of one stint and the start of the next — honest about the stop, wrong as
 * a caption — so these assertions are about the display convention that resolves
 * it: the pit lap belongs to the stint it started on.
 */

describe('displayStintRanges', () => {
  it('hands back untouched ranges when the stints already do not overlap', () => {
    expect(
      displayStintRanges([
        { lap_start: 1, lap_end: 11 },
        { lap_start: 12, lap_end: 20 }
      ])
    ).toEqual([
      { from: 1, to: 11, laps: 11 },
      { from: 12, to: 20, laps: 9 }
    ]);
  });

  it('gives the shared pit lap to the stint it started on', () => {
    const ranges = displayStintRanges([
      { lap_start: 1, lap_end: 6 },
      { lap_start: 6, lap_end: 13 }
    ]);

    expect(ranges).toEqual([
      { from: 1, to: 6, laps: 6 },
      { from: 7, to: 13, laps: 7 }
    ]);

    // Contiguous, non-overlapping, and adding up to the race distance.
    expect(ranges[1].from).toBe(ranges[0].to + 1);
    expect(ranges[0].laps + ranges[1].laps).toBe(13);
  });

  it('keeps three stints separated when every boundary is shared', () => {
    const ranges = displayStintRanges([
      { lap_start: 1, lap_end: 10 },
      { lap_start: 10, lap_end: 20 },
      { lap_start: 20, lap_end: 30 }
    ]);

    expect(ranges.map((r) => [r.from, r.to])).toEqual([
      [1, 10],
      [11, 20],
      [21, 30]
    ]);
    expect(ranges.reduce((sum, r) => sum + r.laps, 0)).toBe(30);
  });

  it('falls back to the session lap span for an open-ended final stint', () => {
    expect(
      displayStintRanges(
        [
          { lap_start: 1, lap_end: 8 },
          { lap_start: 8, lap_end: null }
        ],
        18
      )
    ).toEqual([
      { from: 1, to: 8, laps: 8 },
      { from: 9, to: 18, laps: 10 }
    ]);
  });

  it('never emits a range that ends before it starts', () => {
    // A stint recorded entirely inside the previous one's last lap: pathological,
    // but it must still render as one lap rather than as "8–7".
    const ranges = displayStintRanges([
      { lap_start: 1, lap_end: 7 },
      { lap_start: 5, lap_end: 7 }
    ]);

    for (const r of ranges) {
      expect(r.to).toBeGreaterThanOrEqual(r.from);
      expect(r.laps).toBeGreaterThanOrEqual(1);
    }
    expect(ranges[1]).toEqual({ from: 8, to: 8, laps: 1 });
  });

  it('starts at lap 1, and after the previous stint, when bounds are missing', () => {
    expect(
      displayStintRanges([
        { lap_start: null, lap_end: 4 },
        { lap_start: null, lap_end: 9 }
      ])
    ).toEqual([
      { from: 1, to: 4, laps: 4 },
      { from: 5, to: 9, laps: 5 }
    ]);
  });

  it('returns nothing for no stints', () => {
    expect(displayStintRanges([])).toEqual([]);
  });
});

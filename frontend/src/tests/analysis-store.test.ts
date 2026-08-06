import { describe, it, expect, beforeEach } from 'vitest';
import { AnalysisStore, sameLap } from '../lib/analysis.svelte';

/**
 * The selection rules are the one piece of genuine interaction logic on the
 * analysis side, and they are not obvious: A is sticky, B rotates, deselecting
 * A promotes B. Getting them wrong makes the compare page feel haunted, and it
 * is the kind of wrongness that a screenshot never shows.
 */

const lap = (sessionId: number, l: number) => ({ sessionId, lap: l });

describe('sameLap', () => {
  it('compares by value, and treats null as its own thing', () => {
    expect(sameLap(lap(3, 12), lap(3, 12))).toBe(true);
    expect(sameLap(lap(3, 12), lap(3, 13))).toBe(false);
    expect(sameLap(lap(3, 12), lap(4, 12))).toBe(false);
    expect(sameLap(null, null)).toBe(true);
    expect(sameLap(null, lap(3, 12))).toBe(false);
    expect(sameLap(lap(3, 12), null)).toBe(false);
  });
});

describe('selection', () => {
  let store: AnalysisStore;

  beforeEach(() => {
    store = new AnalysisStore();
  });

  it('starts empty and unpaired', () => {
    expect(store.a).toBeNull();
    expect(store.b).toBeNull();
    expect(store.paired).toBe(false);
  });

  it('fills A first, then B', () => {
    store.toggle(lap(3, 12));
    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toBeNull();
    expect(store.paired).toBe(false);

    store.toggle(lap(3, 15));
    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toEqual(lap(3, 15));
    expect(store.paired).toBe(true);
  });

  it('keeps A sticky and rotates B once both slots are full', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    store.toggle(lap(3, 18));

    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toEqual(lap(3, 18));

    store.toggle(lap(3, 20));
    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toEqual(lap(3, 20));
  });

  it('deselects B when B is clicked again', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    store.toggle(lap(3, 15));

    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toBeNull();
  });

  it('promotes B into A when A is clicked again, so the survivor is the subject', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    store.toggle(lap(3, 12));

    expect(store.a).toEqual(lap(3, 15));
    expect(store.b).toBeNull();
  });

  it('empties completely when the only selection is clicked again', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 12));
    expect(store.a).toBeNull();
    expect(store.b).toBeNull();
  });

  it('distinguishes the same lap number in different sessions', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(4, 12));
    expect(store.paired).toBe(true);
    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toEqual(lap(4, 12));
  });

  it('reports which slot a lap occupies', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    expect(store.slotOf(lap(3, 12))).toBe('a');
    expect(store.slotOf(lap(3, 15))).toBe('b');
    expect(store.slotOf(lap(3, 99))).toBeNull();
  });

  it('swaps subject and reference', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    store.swap();
    expect(store.a).toEqual(lap(3, 15));
    expect(store.b).toEqual(lap(3, 12));
  });

  it('clears both slots', () => {
    store.toggle(lap(3, 12));
    store.toggle(lap(3, 15));
    store.clearSelection();
    expect(store.paired).toBe(false);
  });
});

describe('zoom window', () => {
  let store: AnalysisStore;

  beforeEach(() => {
    store = new AnalysisStore();
  });

  it('starts unzoomed', () => {
    expect(store.window).toBeNull();
    expect(store.focusedCorner).toBeNull();
  });

  it('sets and clears a window', () => {
    store.setWindow(100, 400);
    expect(store.window).toEqual({ min: 100, max: 400 });
    store.clearWindow();
    expect(store.window).toBeNull();
  });

  it('refuses a window that is inverted, empty, or not a number', () => {
    store.setWindow(400, 100);
    expect(store.window).toBeNull();
    store.setWindow(100, 100);
    expect(store.window).toBeNull();
    store.setWindow(Number.NaN, 100);
    expect(store.window).toBeNull();
    store.setWindow(0, Number.POSITIVE_INFINITY);
    expect(store.window).toBeNull();
  });

  it('pads a corner window so the braking zone and exit are both in frame', () => {
    store.focusCorner(8, 1000, 1200);
    expect(store.focusedCorner).toBe(8);
    // 200 m corner, 60% padding = 120 m each side.
    expect(store.window).toEqual({ min: 880, max: 1320 });
  });

  it('applies a minimum padding for a short corner', () => {
    store.focusCorner(3, 1000, 1020);
    // 20 m corner: 60% would be 12 m, so the 60 m floor applies.
    expect(store.window).toEqual({ min: 940, max: 1080 });
  });

  it('never pads below the start line', () => {
    store.focusCorner(1, 10, 40);
    expect(store.window?.min).toBe(0);
  });

  it('forgets the focused corner when the window is cleared', () => {
    store.focusCorner(8, 1000, 1200);
    store.clearWindow();
    expect(store.focusedCorner).toBeNull();
  });
});

describe('URL reconciliation', () => {
  let store: AnalysisStore;

  beforeEach(() => {
    store = new AnalysisStore();
  });

  it('adopts both laps from a shared link', () => {
    store.adoptFromQuery(new URLSearchParams('sa=3&la=12&sb=4&lb=9'));
    expect(store.a).toEqual(lap(3, 12));
    expect(store.b).toEqual(lap(4, 9));
  });

  it('leaves a slot alone when the URL does not name it', () => {
    store.setA(lap(3, 12));
    store.setB(lap(3, 15));
    store.adoptFromQuery(new URLSearchParams('sa=7&la=2'));
    expect(store.a).toEqual(lap(7, 2));
    // B survives the navigation, which is what makes page-to-page links work.
    expect(store.b).toEqual(lap(3, 15));
  });

  it('ignores half-specified and malformed lap references', () => {
    store.adoptFromQuery(new URLSearchParams('sa=3'));
    expect(store.a).toBeNull();
    store.adoptFromQuery(new URLSearchParams('sa=abc&la=12'));
    expect(store.a).toBeNull();
  });

  it('adopts a car index when present', () => {
    store.adoptFromQuery(new URLSearchParams('car=21'));
    expect(store.carIndex).toBe(21);
  });

  it('builds a compare link carrying the whole selection', () => {
    store.setA(lap(3, 12));
    store.setB(lap(4, 9));
    expect(store.compareHref()).toBe('#/compare?sa=3&la=12&sb=4&lb=9');
  });

  it('builds a compare link for a half selection without inventing the other half', () => {
    store.setA(lap(3, 12));
    expect(store.compareHref()).toBe('#/compare?sa=3&la=12');
  });

  it('points the corner link at B when both laps share a session', () => {
    store.setA(lap(3, 12));
    store.setB(lap(3, 9));
    expect(store.cornersHref()).toBe('#/corners?session=3&lap=12&ref=9');
  });

  it('falls back to the session best when B is from another session', () => {
    store.setA(lap(3, 12));
    store.setB(lap(4, 9));
    expect(store.cornersHref()).toBe('#/corners?session=3&lap=12&ref=best');
  });

  it('round-trips a selection through its own link', () => {
    store.setA(lap(3, 12));
    store.setB(lap(4, 9));
    const query = new URLSearchParams(store.compareHref().split('?')[1] ?? '');

    const other = new AnalysisStore();
    other.adoptFromQuery(query);
    expect(other.a).toEqual(store.a);
    expect(other.b).toEqual(store.b);
  });
});

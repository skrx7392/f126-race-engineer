import { describe, it, expect } from 'vitest';
import {
  CHANNEL,
  CHART_SURFACE,
  CSS_VARIABLES,
  DELTA,
  LAP,
  installChartPalette,
  withAlpha
} from '../lib/chart-theme';

/**
 * The chart palette is defined once, in TypeScript, because uPlot paints to a
 * canvas and needs literal colour strings there. Everything drawn *around* the
 * canvas — swatches, chips, the corner time-loss bars — is CSS, and reads the
 * same values through custom properties published at start-up.
 *
 * These tests hold that bridge: every colour the CSS refers to is published,
 * every published value is the constant it claims to be, and the palette itself
 * stays well-formed. The alternative — declaring the hues in `app.css` too —
 * would put two copies of every colour in the repo, and the first re-step would
 * silently leave a swatch disagreeing with the trace it labels.
 */

describe('installChartPalette', () => {
  it('publishes every chart colour onto the element', () => {
    const el = document.createElement('div');
    installChartPalette(el);

    for (const [name, value] of Object.entries(CSS_VARIABLES)) {
      expect(el.style.getPropertyValue(name), name).toBe(value);
    }
  });

  it('publishes the exact constants the canvas draws with', () => {
    const el = document.createElement('div');
    installChartPalette(el);

    const pairs: Array<[string, string]> = [
      ['--chart-speed', CHANNEL.speed],
      ['--chart-throttle', CHANNEL.throttle],
      ['--chart-brake', CHANNEL.brake],
      ['--chart-gear', CHANNEL.gear],
      ['--chart-lap-a', LAP.a],
      ['--chart-lap-b', LAP.b],
      ['--chart-ahead', DELTA.ahead],
      ['--chart-behind', DELTA.behind]
    ];

    for (const [name, expected] of pairs) {
      expect(el.style.getPropertyValue(name), name).toBe(expected);
    }
  });

  it('defaults to the document root and does not throw', () => {
    expect(() => installChartPalette()).not.toThrow();
    expect(document.documentElement.style.getPropertyValue('--chart-lap-a')).toBe(LAP.a);
  });

  it('covers every variable the stylesheets reference', () => {
    // Kept in step by hand with the `var(--chart-*)` uses in app.css and the
    // component styles; a missing entry here is an unresolved colour on screen.
    const referenced = [
      '--chart-lap-a',
      '--chart-lap-b',
      '--chart-ahead',
      '--chart-behind',
      '--chart-speed',
      '--chart-throttle',
      '--chart-brake',
      '--chart-gear'
    ];
    for (const name of referenced) {
      expect(Object.keys(CSS_VARIABLES), name).toContain(name);
    }
  });
});

describe('palette hygiene', () => {
  it('keeps every chart colour a full six-digit hex', () => {
    for (const hex of Object.values(CSS_VARIABLES)) {
      expect(hex).toMatch(/^#[0-9A-Fa-f]{6}$/);
    }
  });

  it('gives the two lap slots genuinely different colours', () => {
    expect(LAP.a).not.toBe(LAP.b);
  });

  it('keeps the diverging poles distinct from each other', () => {
    expect(DELTA.ahead).not.toBe(DELTA.behind);
  });

  it('never paints a lap in a delta colour, which would confuse the panes', () => {
    expect([DELTA.ahead, DELTA.behind]).not.toContain(LAP.a);
    expect([DELTA.ahead, DELTA.behind]).not.toContain(LAP.b);
  });

  it('records the surface the palette was validated against', () => {
    // The validator was run with --surface #131519, which is --surface-1. If the
    // panel surface moves, the contrast results in chart-theme.ts are stale.
    expect(CHART_SURFACE).toBe('#131519');
  });
});

describe('withAlpha', () => {
  it('appends an 8-bit alpha channel', () => {
    expect(withAlpha('#112233', 1)).toBe('#112233ff');
    expect(withAlpha('#112233', 0)).toBe('#11223300');
    expect(withAlpha('#112233', 0.22)).toBe('#11223338');
  });

  it('clamps out-of-range alpha rather than emitting a broken colour', () => {
    expect(withAlpha('#112233', 5)).toBe('#112233ff');
    expect(withAlpha('#112233', -1)).toBe('#11223300');
  });
});

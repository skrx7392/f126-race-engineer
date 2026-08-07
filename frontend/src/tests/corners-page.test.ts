import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import UPlotChart from '../components/UPlotChart.svelte';
import { analysis } from '../lib/analysis.svelte';
import {
  installAnalysisMock,
  mockCorners,
  mockTelemetry,
  DUPLICATE_DISTANCE_LAP
} from '../lib/analysis-mock';
import type { CornersResponse } from '../lib/analysis-api';

/**
 * The corners page against a lap compared with itself.
 *
 * This is the shape that took the pane down in the real app. `ref=best` resolved to the
 * subject lap, so the compare payload carried two series with identical labels ("S3 L5"
 * twice, and "S3 L5 throttle" twice in the pedals pane). The chart legend was a keyed
 * `{#each}` keyed on that label, and Svelte threw `each_key_duplicate`, killing the whole
 * route rather than one legend row.
 */

let restore: (() => void) | null = null;

function goto(hash: string): void {
  window.location.hash = hash;
}

beforeEach(() => {
  analysis.clearSelection();
  analysis.clearWindow();
});

afterEach(() => {
  cleanup();
  restore?.();
  restore = null;
  vi.restoreAllMocks();
  window.location.hash = '';
});

const HEADLINE_SESSION = 3;

describe('chart legend keying', () => {
  /**
   * The exact crash. Keying the legend on `spec.label` threw
   * `each_key_duplicate: Keyed each block has duplicate key 'S3 L5' at indexes 1 and 2`
   * and took the whole pane down. Two series legitimately share a label whenever a lap
   * is charted against itself, and the pedals pane repeats each label twice over.
   */
  it('renders one row per series even when two share a label', () => {
    const { container } = render(UPlotChart, {
      props: {
        data: [
          [0, 1, 2],
          [10, 11, 12],
          [20, 21, 22]
        ],
        series: [{}, { label: 'S3 L5', stroke: '#f00' }, { label: 'S3 L5', stroke: '#00f' }],
        legend: true
      }
    });
    expect(container.querySelectorAll('.legend li').length).toBe(2);
  });

  it('survives four series sharing two labels, as the pedals pane does', () => {
    const { container } = render(UPlotChart, {
      props: {
        data: [[0, 1], [1, 1], [0, 0], [1, 1], [0, 0]],
        series: [
          {},
          { label: 'S3 L5 throttle' },
          { label: 'S3 L5 brake' },
          { label: 'S3 L5 throttle' },
          { label: 'S3 L5 brake' }
        ],
        legend: true
      }
    });
    expect(container.querySelectorAll('.legend li').length).toBe(4);
  });
});

describe('mock reference resolution mirrors the backend', () => {
  it('never resolves ref=best to the subject lap', () => {
    // Find the session-best lap by asking any other lap what its reference is.
    const probe = mockCorners(HEADLINE_SESSION, 2, 'best') as CornersResponse;
    const bestLap = probe.ref_lap_number as number;

    const onTheBestLap = mockCorners(HEADLINE_SESSION, bestLap, 'best') as CornersResponse;
    expect(onTheBestLap.ref_lap_number).not.toBe(bestLap);
    expect(onTheBestLap.self_reference).toBe(false);
  });

  it('flags an explicit self-reference instead of pretending it is a comparison', () => {
    const payload = mockCorners(HEADLINE_SESSION, 5, 5) as CornersResponse;
    expect(payload.ref_lap_number).toBe(5);
    expect(payload.self_reference).toBe(true);
    expect(payload.total_delta_ms).toBe(0);
  });
});

describe('telemetry fixtures carry real-world duplicate distances', () => {
  it('repeats distance samples on the crawl lap, the way a real capture does', () => {
    const t = mockTelemetry(HEADLINE_SESSION, DUPLICATE_DISTANCE_LAP);
    expect(t).not.toBeNull();
    const d = t!.distance_m;
    expect(new Set(d).size).toBeLessThan(d.length);
    // Still non-decreasing: duplicates, not disorder.
    for (let i = 1; i < d.length; i++) {
      expect(d[i]).toBeGreaterThanOrEqual(d[i - 1] as number);
    }
    // Every channel stays parallel with the distance axis.
    expect(t!.speed_kmh.length).toBe(d.length);
    expect(t!.points).toBe(d.length);
  });

  it('leaves the other laps strictly increasing', () => {
    const t = mockTelemetry(HEADLINE_SESSION, DUPLICATE_DISTANCE_LAP + 2);
    const d = t!.distance_m;
    expect(new Set(d).size).toBe(d.length);
  });
});

describe('#/corners with a self-reference', () => {
  it('renders the page instead of crashing on duplicate series labels', async () => {
    restore = installAnalysisMock();
    const errors: unknown[] = [];
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errors.push(args[0]);
    });

    goto(`#/corners?session=${HEADLINE_SESSION}&lap=5&ref=5`);
    render(App);

    // The corner table is the page's answer; if the route had thrown, it never appears.
    await waitFor(() => {
      expect(screen.getByTestId('corners-table')).toBeTruthy();
    });

    const message = errors.map((e) => String(e)).join('\n');
    expect(message).not.toContain('each_key_duplicate');
  });

  it('says why there are no reference deltas rather than showing zeros', async () => {
    restore = installAnalysisMock();
    goto(`#/corners?session=${HEADLINE_SESSION}&lap=5&ref=5`);
    render(App);

    await waitFor(() => {
      expect(screen.getByTestId('self-reference-note')).toBeTruthy();
    });
    expect(screen.getByTestId('self-reference-note').textContent).toContain('Only one lap');
  });
});

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import {
  installAnalysisMock,
  mockCareerTrack,
  BAHRAIN_TRACK_ID,
  BAHRAIN_TT_SESSION_ID,
  MIAMI_TRACK_ID,
  MONTREAL_TRACK_ID
} from '../lib/analysis-mock';
import { DASH, formatLapTime, formatSectorTime } from '../lib/format';
import { visitLabel } from '../lib/analysis-format';
import type { CareerTrackResponse } from '../lib/analysis-api';

/**
 * The per-track career page, at the fetch boundary.
 *
 * The chart's two series are *derived from the visits* — the payload does not
 * duplicate them — so the assertions compare the page against the same visits
 * the table shows. The single-visit circuit is exercised on purpose: a career
 * one weekend old must render a one-point chart, not crash or apologise. And
 * the theoretical best must be captioned as a derived time everywhere it
 * appears, because it sits beside laps that were actually driven.
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
  window.location.hash = '';
});

describe('#/career/tracks/{track_id}', () => {
  it('shows a loading state before the data lands', () => {
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    goto(`#/career/tracks/${MIAMI_TRACK_ID}`);
    const { container } = render(App);
    expect(container.querySelector('[data-page="careertrack"]')).not.toBeNull();
    expect(container.querySelector('[data-state="loading"]')).not.toBeNull();
  });

  it('shows the PB with its three sectors and where each was set', async () => {
    restore = installAnalysisMock();
    goto(`#/career/tracks/${BAHRAIN_TRACK_ID}`);
    render(App);

    const card = await screen.findByTestId('pb-card');
    const expected = mockCareerTrack(BAHRAIN_TRACK_ID) as CareerTrackResponse;

    expect(card.textContent).toContain(formatLapTime(expected.pb!.best_lap_ms));
    // Set in the time trial: the PB credits whatever actually drove it.
    expect(card.textContent).toContain('Time Trial');

    const sectors = screen.getByTestId('pb-sectors');
    const s = expected.pb!.sectors!;
    for (const [ms, sid, lap] of [
      [s.s1_ms, s.s1_session_id, s.s1_lap_number],
      [s.s2_ms, s.s2_session_id, s.s2_lap_number],
      [s.s3_ms, s.s3_session_id, s.s3_lap_number]
    ] as const) {
      expect(sectors.textContent).toContain(formatSectorTime(ms));
      expect(sectors.textContent).toContain(`set S${sid} L${lap}`);
    }

    // The sum is shown, and captioned as the derived time it is.
    const theoretical = screen.getByTestId('pb-theoretical');
    expect(theoretical.textContent).toContain(formatLapTime(expected.pb!.theoretical_ms));
    expect(theoretical.textContent).toContain('Theoretical');
    expect(card.textContent).toContain('never driven as one lap');
  });

  it('derives the evolution chart from the visits, labelled "S1 R2"-style', async () => {
    restore = installAnalysisMock();
    goto(`#/career/tracks/${MIAMI_TRACK_ID}`);
    const { container } = render(App);

    await screen.findByTestId('pb-card');
    const expected = mockCareerTrack(MIAMI_TRACK_ID) as CareerTrackResponse;

    // The pane exists, with both derived series named in its legend.
    const pane = container.querySelector('[data-pane="career-evolution"]');
    expect(pane).not.toBeNull();
    expect(pane!.textContent).toContain('Best lap');
    expect(pane!.textContent).toContain('Median clean lap');

    // The visit table shows the same visits the chart is drawn from.
    const table = screen.getByTestId('visit-table');
    const rows = table.querySelectorAll('tbody tr[data-visit]');
    expect(rows.length).toBe(expected.visits.length);
    expected.visits.forEach((visit, i) => {
      expect(rows[i]!.textContent).toContain(visitLabel(visit.season, visit.round));
      expect(rows[i]!.textContent).toContain(formatLapTime(visit.best_lap_ms));
    });
  });

  it('lists every session at the circuit with its own consistency', async () => {
    restore = installAnalysisMock();
    goto(`#/career/tracks/${BAHRAIN_TRACK_ID}`);
    render(App);

    const table = await screen.findByTestId('session-table');
    const expected = mockCareerTrack(BAHRAIN_TRACK_ID) as CareerTrackResponse;
    await waitFor(() => {
      expect(table.querySelectorAll('tbody tr').length).toBe(expected.sessions.length);
    });

    // The time trial is in the list (it is a session here) even though the
    // visit table above excludes it (it is not a round).
    const tt = table.querySelector(`tr[data-session-id="${BAHRAIN_TT_SESSION_ID}"]`);
    expect(tt).not.toBeNull();
    expect(screen.getByTestId('visit-table').textContent).not.toContain('Time Trial');

    for (const session of expected.sessions) {
      const row = table.querySelector(`tr[data-session-id="${session.id}"]`)!;
      expect(row.textContent).toContain(formatLapTime(session.best_lap_ms));
      expect(row.textContent).toContain(formatLapTime(session.median_ms));
      // Each row is a real link into the session detail page.
      expect(row.querySelector('a.row-link')?.getAttribute('href')).toBe(
        `#/sessions/${session.id}`
      );
    }
  });

  it('renders a single-visit circuit gracefully, saying so instead of breaking', async () => {
    restore = installAnalysisMock();
    goto(`#/career/tracks/${MONTREAL_TRACK_ID}`);
    const { container } = render(App);

    await screen.findByTestId('pb-card');
    const expected = mockCareerTrack(MONTREAL_TRACK_ID) as CareerTrackResponse;
    expect(expected.visits.length).toBe(1);

    // The chart still mounts — one point per series — and the page says why
    // there is no line yet.
    expect(container.querySelector('[data-pane="career-evolution"]')).not.toBeNull();
    expect(screen.getByTestId('single-visit-note').textContent).toContain('One visit');
    expect(screen.getByTestId('visit-table').querySelectorAll('tbody tr[data-visit]').length).toBe(1);
  });

  it('renders a visit without results as em-dashes', async () => {
    restore = installAnalysisMock();
    // Monza: one practice-only weekend — no quali, no race, no consistency.
    goto('#/career/tracks/11');
    render(App);

    const table = await screen.findByTestId('visit-table');
    const row = table.querySelector('tr[data-visit="1-3"]')!;
    const cells = [...row.querySelectorAll('td')].map((td) => td.textContent!.trim());
    // Quali, sprint, race and consistency are all unknown, and all dashes.
    expect(cells[3]).toBe(DASH);
    expect(cells[4]).toBe(DASH);
    expect(cells[5]).toBe(DASH);
    expect(cells[6]).toBe(DASH);
    // The best lap is real — laps were driven, just never raced.
    expect(cells[2]).toMatch(/\d:\d\d\.\d\d\d/);
  });

  it('reports an unknown circuit as not found, not as a broken page', async () => {
    restore = installAnalysisMock();
    goto('#/career/tracks/999');
    const { container } = render(App);

    await waitFor(() => {
      expect(container.querySelector('[data-error-kind="notfound"]')).not.toBeNull();
    });
    expect(container.textContent).toContain('no sessions recorded at this track');
  });

  it('refuses a career tracks URL without an id at the router, before any fetch', () => {
    restore = installAnalysisMock();
    goto('#/career/tracks');
    const { container } = render(App);
    expect(container.querySelector('[data-page="notfound"]')).not.toBeNull();
  });

  it('reaches the database-unavailable panel like every other analysis page', async () => {
    restore = installAnalysisMock({ failStatus: 503 });
    goto(`#/career/tracks/${MIAMI_TRACK_ID}`);
    const { container } = render(App);

    await waitFor(() => {
      expect(container.querySelector('[data-error-kind="unavailable"]')).not.toBeNull();
    });
  });

  it('keeps the Career nav link lit on a track page', async () => {
    restore = installAnalysisMock();
    goto(`#/career/tracks/${MIAMI_TRACK_ID}`);
    const { container } = render(App);
    await screen.findByTestId('pb-card');

    const current = container.querySelector('[aria-current="page"]');
    expect(current?.textContent?.trim()).toBe('Career');
  });
});

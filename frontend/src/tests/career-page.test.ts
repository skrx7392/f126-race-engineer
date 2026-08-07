import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import {
  installAnalysisMock,
  mockCareerOverview,
  BAHRAIN_TRACK_ID,
  MIAMI_TRACK_ID
} from '../lib/analysis-mock';
import { DASH, formatLapTime } from '../lib/format';
import type { CareerOverview, CareerTotals } from '../lib/analysis-api';

/**
 * The career page, at the fetch boundary — the same component that will face
 * the real backend, with only the answerer swapped.
 *
 * The assertions lean on the three ways this page could lie. A season or round
 * number is a *derived* figure, so the derivation notes must be on the page,
 * not in a tooltip; a pinned tag and a tag conflict must be visible marks, not
 * silently-resolved numbers; and a race whose classification never landed must
 * come out as em-dashes — a missing packet can never become a zero, a position,
 * or a fabricated points total.
 */

let restore: (() => void) | null = null;

function goto(hash: string): void {
  window.location.hash = hash;
}

beforeEach(() => {
  analysis.clearSelection();
  analysis.clearWindow();
  goto('#/career');
});

afterEach(() => {
  cleanup();
  restore?.();
  restore = null;
  window.location.hash = '';
});

describe('#/career', () => {
  it('shows a loading state before the data lands', () => {
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    const { container } = render(App);
    expect(container.querySelector('[data-page="career"]')).not.toBeNull();
    expect(container.querySelector('[data-state="loading"]')).not.toBeNull();
  });

  it('sums the career totals strip from the same weekends it lists', async () => {
    restore = installAnalysisMock();
    render(App);

    const card = await screen.findByTestId('career-totals');
    const expected = mockCareerOverview();
    expect(card.textContent).toContain(String(expected.career_totals.points));
    expect(card.textContent).toContain(`${expected.career_totals.races} grands prix raced`);
    for (const label of ['Points', 'Wins', 'Podiums', 'Poles', 'Fastest laps', 'Sprint wins']) {
      expect(card.textContent).toContain(label);
    }

    // Each season carries its own strip, with its own numbers.
    for (const season of expected.seasons) {
      const strip = screen.getByTestId(`season-totals-${season.season}`);
      expect(strip.textContent).toContain(String(season.totals.points));
    }
  });

  it('renders one weekend row per round, linking each to its track page', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);

    await screen.findByTestId('career-totals');
    const expected = mockCareerOverview();

    for (const season of expected.seasons) {
      const table = screen.getByTestId(`weekend-table-${season.season}`);
      await waitFor(() => {
        expect(table.querySelectorAll('tbody tr').length).toBe(season.weekends.length);
      });
      for (const weekend of season.weekends) {
        const row = container.querySelector(`[data-weekend="${weekend.season}-${weekend.round}"]`);
        expect(row, `weekend S${weekend.season} R${weekend.round}`).not.toBeNull();
        const link = row!.querySelector('a.row-link');
        expect(link?.getAttribute('href')).toBe(`#/career/tracks/${weekend.track_id}`);
      }
    }
  });

  it('badges the sprint weekend and shows its three results side by side', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('career-totals');

    const expected = mockCareerOverview();
    const sprint = expected.seasons[0]!.weekends.find((w) => w.format === 'sprint')!;
    const row = container.querySelector(`[data-weekend="${sprint.season}-${sprint.round}"]`)!;

    expect(row.querySelector('.tier-format-sprint')).not.toBeNull();
    expect(row.querySelector('td.quali')!.textContent).toContain(`P${sprint.quali!.position}`);
    expect(row.querySelector('td.sprint')!.textContent).toContain(`P${sprint.sprint!.position}`);
    expect(row.querySelector('td.race')!.textContent).toContain(`P${sprint.race!.position}`);
    // The fastest-lap marker, and the weekend's summed points.
    expect(row.querySelector('td.race')!.textContent).toContain('FL');
    expect(row.querySelector('td.points')!.textContent).toContain(String(sprint.points));
    // A standard weekend is badged too — the format is never colour alone.
    const standard = container.querySelector('[data-weekend="1-2"]')!;
    expect(standard.textContent).toContain('standard');
  });

  it('renders a race with no classification as em-dashes, never a number', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('career-totals');

    // Season 2's Miami race recorded no classification packet.
    const row = container.querySelector('[data-weekend="2-1"]')!;
    expect(row.querySelector('td.race')!.textContent!.trim()).toContain(DASH);
    expect(row.querySelector('td.race')!.textContent).not.toMatch(/P\d/);
    expect(row.querySelector('td.points')!.textContent!.trim()).toBe(DASH);

    // A practice-only weekend shows dashes for quali and race, and 0 points —
    // nothing was raced, which is a known fact rather than a gap.
    const monza = container.querySelector('[data-weekend="1-3"]')!;
    expect(monza.querySelector('td.quali')!.textContent!.trim()).toBe(DASH);
    expect(monza.querySelector('td.race')!.textContent!.trim()).toBe(DASH);
    expect(monza.querySelector('td.points')!.textContent!.trim()).toBe('0');
  });

  it('marks the pinned weekend and warns about its tag conflict', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('career-totals');

    const pinned = await screen.findByTestId('tag-pinned');
    expect(pinned.textContent).toContain('pinned');
    expect(pinned.getAttribute('title')).toContain('pinned by hand');

    const conflict = screen.getByTestId('tag-conflict');
    expect(conflict.textContent).toContain('tag conflict');
    // Both sit on the same weekend row, the one the tags actually pin.
    expect(pinned.closest('[data-weekend]')).toBe(conflict.closest('[data-weekend]'));
    expect(pinned.closest('[data-weekend]')!.getAttribute('data-weekend')).toBe('2-1');
    // No other row carries either mark.
    expect(container.querySelectorAll('[data-testid="tag-pinned"]').length).toBe(1);
    expect(container.querySelectorAll('[data-testid="tag-conflict"]').length).toBe(1);
  });

  it('prints the derivation rules on the page, not behind anything', async () => {
    restore = installAnalysisMock();
    render(App);

    const notes = await screen.findByTestId('career-notes');
    const expected = mockCareerOverview();
    for (const text of Object.values(expected.notes)) {
      expect(notes.textContent).toContain(text);
    }
    // The excluded sessions are counted, and said to still count for PBs.
    const untracked = screen.getByTestId('untracked');
    expect(untracked.textContent).toContain(String(expected.untracked_sessions));
    expect(untracked.textContent).toContain('personal bests');

    // And the table itself says its numbers are derived.
    const caption = screen.getByTestId('weekend-table-1').querySelector('caption');
    expect(caption!.textContent).toContain('Derived season and round');
  });

  it('lists one PB per circuit with the theoretical best captioned as such', async () => {
    restore = installAnalysisMock();
    render(App);

    const board = await screen.findByTestId('pb-board');
    const expected = mockCareerOverview();
    await waitFor(() => {
      expect(board.querySelectorAll('tbody tr').length).toBe(expected.pbs.length);
    });

    const bahrain = board.querySelector(`[data-pb-track="${BAHRAIN_TRACK_ID}"]`)!;
    const pb = expected.pbs.find((p) => p.track_id === BAHRAIN_TRACK_ID)!;
    expect(bahrain.textContent).toContain(formatLapTime(pb.best_lap_ms));
    expect(bahrain.textContent).toContain(formatLapTime(pb.theoretical_ms));
    expect(bahrain.textContent).toContain('theoretical');
    // The time trial that set it is credited by name.
    expect(bahrain.textContent).toContain('Time Trial');
    // Rows link into the track pages.
    expect(bahrain.querySelector('a.row-link')?.getAttribute('href')).toBe(
      `#/career/tracks/${BAHRAIN_TRACK_ID}`
    );
    expect(board.querySelector(`[data-pb-track="${MIAMI_TRACK_ID}"]`)).not.toBeNull();
    // The caption states what "theoretical" means.
    expect(board.querySelector('caption')!.textContent).toContain('never driven as one lap');
  });

  it('renders a null-heavy payload as em-dashes rather than invented numbers', async () => {
    const zeros: CareerTotals = {
      points: 0,
      wins: 0,
      podiums: 0,
      poles: 0,
      fastest_laps: 0,
      sprint_wins: 0,
      races: 1
    };
    const payload: CareerOverview = {
      seasons: [
        {
          season: 1,
          rounds: 1,
          totals: zeros,
          weekends: [
            {
              season: 1,
              round: 1,
              track_id: 5,
              track_name: null,
              format: 'standard',
              started_at_wall: null,
              ended_at_wall: null,
              session_ids: [42],
              sessions: [
                {
                  id: 42,
                  session_type: 15,
                  session_type_name: null,
                  started_at_wall: null,
                  best_lap_ms: null
                }
              ],
              quali: null,
              sprint: null,
              race: {
                session_id: 42,
                position: null,
                grid_position: null,
                points: null,
                pit_stops: null,
                best_lap_ms: null,
                fastest_lap: null,
                status: null
              },
              points: null,
              consistency: null,
              tags: null,
              tag_conflict: false
            }
          ]
        }
      ],
      career_totals: zeros,
      pbs: [
        {
          track_id: 5,
          track_name: null,
          best_lap_ms: 91000,
          session_id: 42,
          session_label: null,
          lap_number: null,
          compound_visual: null,
          compound_name: null,
          set_at_wall: null,
          theoretical_ms: null,
          top_speed_kmh: null
        }
      ],
      untracked_sessions: 0,
      notes: {}
    };

    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    const { container } = render(App);
    const row = await waitFor(() => {
      const found = container.querySelector('[data-weekend="1-1"]');
      expect(found).not.toBeNull();
      return found!;
    });

    // An unnamed circuit still gets an honest label, and every unknown a dash.
    expect(row.textContent).toContain('Track 5');
    expect(row.querySelector('td.race')!.textContent!.trim()).toBe(DASH);
    expect(row.querySelector('td.points')!.textContent!.trim()).toBe(DASH);
    expect(row.querySelector('td.consistency')!.textContent!.trim()).toBe(DASH);

    const board = screen.getByTestId('pb-board');
    const pbRow = board.querySelector('[data-pb-track="5"]')!;
    // The one known number renders; the compound, theoretical and speed do not.
    expect(pbRow.textContent).toContain(formatLapTime(91000));
    const cells = [...pbRow.querySelectorAll('td')].map((td) => td.textContent!.trim());
    expect(cells[2]).toBe(DASH); // compound
    expect(cells[3]).toBe(DASH); // theoretical
    expect(cells[4]).toBe(DASH); // top speed
    expect(cells[5]).toBe(DASH); // set in
  });

  it('renders an empty state for an empty archive, not a bare page', async () => {
    const empty: CareerOverview = {
      seasons: [],
      career_totals: {
        points: 0,
        wins: 0,
        podiums: 0,
        poles: 0,
        fastest_laps: 0,
        sprint_wins: 0,
        races: 0
      },
      pbs: [],
      untracked_sessions: 0,
      notes: {}
    };
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(empty), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    const { container } = render(App);
    await waitFor(() => {
      expect(container.querySelector('[data-state="empty"]')).not.toBeNull();
    });
    expect(container.textContent).toContain('No career sessions recorded yet');
  });

  it('reaches the database-unavailable panel like every other analysis page', async () => {
    restore = installAnalysisMock({ failStatus: 503 });
    const { container } = render(App);

    await waitFor(() => {
      expect(container.querySelector('[data-error-kind="unavailable"]')).not.toBeNull();
    });
    expect(container.textContent).toContain('Database unavailable');
  });

  it('lights the Career link in the analysis nav', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('career-totals');

    const current = container.querySelector('[aria-current="page"]');
    expect(current?.textContent?.trim()).toBe('Career');
  });
});

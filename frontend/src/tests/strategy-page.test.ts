import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import { installAnalysisMock, mockStrategy, BAHRAIN_TRACK_ID } from '../lib/analysis-mock';
import { inGameWear, formatSecondsDelta } from '../lib/analysis-format';
import type { StrategyResponse } from '../lib/analysis-api';

/**
 * The strategy page, at the fetch boundary.
 *
 * Mocked where the network is, never inside the page, so what is under test is the same
 * component that will face the real backend. Two things get asserted harder than the rest,
 * because they are the two ways this page could lie: an untested compound must still be
 * *shown* (a missing row reads as "no such tyre" rather than "never measured"), and a
 * number that could not be computed must appear as a stated omission rather than as a zero.
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

const BAHRAIN = BAHRAIN_TRACK_ID;
const MONZA_TRACK_ID = 11;

describe('the strategy fixture behaves like the engine', () => {
  it('plans only within the wear ceiling and only on two compounds', () => {
    const payload = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    expect(payload.plans.length).toBeGreaterThan(0);
    for (const plan of payload.plans) {
      expect(new Set(plan.compounds).size).toBeGreaterThanOrEqual(2);
      expect(plan.stints.reduce((n, s) => n + s.laps, 0)).toBe(20);
      for (const stint of plan.stints) {
        expect(stint.projected_end_wear_pct).toBeLessThanOrEqual(payload.wear_cliff_pct);
      }
      for (const window of plan.pit_windows) {
        expect(window.earliest_lap).toBeLessThanOrEqual(window.planned_lap);
        expect(window.planned_lap).toBeLessThanOrEqual(window.latest_lap);
      }
    }
  });

  it('ranks by projected time, with rank 1 at zero delta', () => {
    const payload = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    const times = payload.plans.map((p) => p.total_time_ms);
    expect(times).toEqual([...times].sort((a, b) => a - b));
    expect(payload.plans[0]?.delta_to_best_ms).toBe(0);
    expect(payload.plans.every((p) => p.delta_to_best_ms >= 0)).toBe(true);
  });

  it('refuses to guess a race length for a circuit that has never held one', () => {
    const failure = mockStrategy(MONZA_TRACK_ID, null);
    expect(failure).toMatchObject({ status: 422 });
  });

  it('404s for a circuit with nothing recorded at it', () => {
    expect(mockStrategy(999, 20)).toMatchObject({ status: 404 });
  });
});

describe('#/strategy', () => {
  it('shows the fuel load in kilograms and in slider laps', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const card = await screen.findByTestId('fuel-card');
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    expect(expected.fuel).not.toBeNull();
    // Both units, because the game asks for laps and the setup screen shows kilograms.
    expect(card.textContent).toContain(expected.fuel!.recommended_kg.toFixed(1));
    expect(card.textContent).toContain(expected.fuel!.slider_laps.toFixed(2));
    expect(card.textContent).toContain('kg/lap measured over');
  });

  it('draws one card per ranked plan, with its pit window and safety-car note', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    await screen.findByTestId('fuel-card');
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    await waitFor(() => {
      expect(document.querySelectorAll('.plan').length).toBe(expected.plans.length);
    });

    const best = document.querySelector('[data-plan="1"]');
    expect(best).toBeTruthy();
    expect(best!.textContent).toContain('fastest');
    // The stint bars are the plan: one per stint, laid out at true lap width.
    expect(best!.querySelectorAll('.bar').length).toBe(expected.plans[0]!.stints.length);
    expect(best!.textContent).toContain('Box 1');
    expect(best!.textContent).toContain('Safety car');

    const second = document.querySelector('[data-plan="2"]');
    expect(second!.textContent).toContain(formatSecondsDelta(expected.plans[1]!.delta_to_best_ms));
  });

  it('lists every compound, marking the untested one instead of dropping it', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const table = await screen.findByTestId('compound-table');
    const rows = [...table.querySelectorAll('tbody tr')];
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    expect(rows.length).toBe(expected.compounds.length);

    for (const compound of expected.compounds) {
      const row = table.querySelector(`tr[data-compound="${compound.compound_visual}"]`);
      expect(row, `compound ${compound.compound_visual} is missing from the table`).toBeTruthy();
      if (compound.untested) {
        expect(row!.textContent).toContain('untested');
        expect(row!.textContent).toContain('no stint on this compound');
      } else {
        expect(row!.textContent).toContain(compound.evidence as string);
      }
    }
  });

  it('says which evidence tier each compound was measured in', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const table = await screen.findByTestId('compound-table');
    // The soft was only ever run in qualifying, and the page has to say so rather than
    // presenting a quali degradation slope as race data.
    const soft = table.querySelector('tr[data-compound="16"]');
    expect(soft!.textContent).toContain('practice');
    const medium = table.querySelector('tr[data-compound="17"]');
    expect(medium!.textContent).toContain('race');
  });

  it('annotates wear with the in-game reading, marked as approximate', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const table = await screen.findByTestId('compound-table');
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    const medium = expected.compounds.find((c) => c.compound_visual === 17);
    const row = table.querySelector('tr[data-compound="17"]');
    expect(row!.textContent).toContain(inGameWear(medium!.wear!.pct_per_lap));
    // The conversion never replaces the telemetry figure; both are on the row.
    expect(row!.textContent).toContain(`${medium!.wear!.pct_per_lap!.toFixed(1)}%`);
  });

  it('names every session it was built from, and what each one gave', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const footer = await screen.findByTestId('provenance');
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    expect(footer.querySelectorAll('li').length).toBe(expected.sessions_used.length);
    expect(footer.textContent).toContain('fuel burn');
    expect(footer.textContent).toContain('pit-lane loss');
  });

  it('offers only circuits that have sessions recorded at them', async () => {
    restore = installAnalysisMock();
    goto('#/strategy');
    render(App);

    const picker = (await screen.findByTestId('track-picker')) as HTMLSelectElement;
    // The options come from the session list, which is a second request.
    await waitFor(() => {
      expect(picker.options.length).toBeGreaterThan(0);
    });
    const values = [...picker.options].map((o) => Number(o.value));
    expect(values).toContain(BAHRAIN);
    expect(values).toContain(MONZA_TRACK_ID);
    expect(values).not.toContain(-1);
  });

  it('reports a 422 as a request to name the race length, not as a broken page', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${MONZA_TRACK_ID}`);
    render(App);

    await waitFor(() => {
      expect(document.querySelector('[data-state="error"]')).toBeTruthy();
    });
    const panel = document.querySelector('[data-state="error"]');
    expect(panel!.getAttribute('data-error-kind')).toBe('invalid');
    expect(panel!.textContent).toContain('race_laps');
  });

  it('reaches the database-unavailable panel like every other analysis page', async () => {
    restore = installAnalysisMock({ failStatus: 503 });
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    await waitFor(() => {
      expect(document.querySelector('[data-error-kind="unavailable"]')).toBeTruthy();
    });
  });
});

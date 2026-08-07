import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import {
  installAnalysisMock,
  mockStrategy,
  BAHRAIN_TRACK_ID,
  MONTREAL_TRACK_ID
} from '../lib/analysis-mock';
import { inGameWear, formatSecondsDelta } from '../lib/analysis-format';
import { DASH } from '../lib/format';
import type { StrategyResponse } from '../lib/analysis-api';

/**
 * The strategy page, at the fetch boundary.
 *
 * Mocked where the network is, never inside the page, so what is under test is the same
 * component that will face the real backend. Three things get asserted harder than the rest,
 * because they are the three ways this page could lie: an untested compound must still be
 * *shown* (a missing row reads as "no such tyre" rather than "never measured"), a number
 * that could not be computed must appear as a stated omission rather than as a zero, and a
 * plan set ordered by a rule rather than by projected time must say so — with its time
 * columns empty — rather than presenting rank 1 as "fastest".
 *
 * Both circuits are exercised. Bahrain is the modelled weekend (plans ranked on pace, one
 * compound's slope derived from the circuit's wear-to-time coefficient); Montreal is the
 * practice-only weekend that produced this feature, where nothing ran long enough to fit
 * anything and the only honest answer is a feasibility list.
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
    expect(payload.plans_ranking).toBe('time');
    const times = payload.plans.map((p) => p.total_time_ms ?? 0);
    expect(times).toEqual([...times].sort((a, b) => a - b));
    expect(payload.plans[0]?.delta_to_best_ms).toBe(0);
    expect(payload.plans.every((p) => (p.delta_to_best_ms ?? 0) >= 0)).toBe(true);
  });

  it('derives the slope of a compound that never ran long enough to fit one', () => {
    const payload = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    const calibration = payload.wear_calibration;
    expect(calibration).not.toBeNull();

    const hard = payload.compounds.find((c) => c.compound_visual === 18);
    expect(hard?.pace?.source).toBe('derived');
    // The coefficient times this compound's own wear rate, and nothing else.
    expect(hard?.pace?.deg_ms_per_lap).toBeCloseTo(
      calibration!.ms_per_wear_pct * hard!.wear!.pct_per_lap!,
      1
    );
    // No r² of its own: the line was never fitted to these laps.
    expect(hard?.pace?.r2).toBeNull();
    // Both parents named.
    expect(hard?.pace?.derived_from?.calibration.session_id).toBe(calibration!.session_id);
    expect(hard?.pace?.derived_from?.wear?.session_id).toBe(hard?.wear?.session_id);

    // A compound with its own fit keeps it.
    const medium = payload.compounds.find((c) => c.compound_visual === 17);
    expect(medium?.pace?.source).toBe('fit');
    expect(medium?.pace?.derived_from).toBeUndefined();
  });

  it('falls back to a feasibility list, ranked by rule, when nothing was modelled', () => {
    const payload = mockStrategy(MONTREAL_TRACK_ID, null) as StrategyResponse;
    expect(payload.plans.length).toBeGreaterThan(0);
    expect(payload.plans_ranking).toBe('heuristic');
    expect(payload.wear_calibration).toBeNull();
    expect(payload.omitted['wear_calibration']).toBeTruthy();
    expect(payload.omitted['plans']).toBeUndefined();

    for (const plan of payload.plans) {
      // Never a projected time, because nothing measured one.
      expect(plan.total_time_ms).toBeNull();
      expect(plan.delta_to_best_ms).toBeNull();
      // Everything the wear rates do support is still real.
      expect(plan.stints.reduce((n, s) => n + s.laps, 0)).toBe(payload.race_laps);
      for (const stint of plan.stints) {
        expect(stint.projected_end_wear_pct).toBeLessThanOrEqual(payload.wear_cliff_pct);
      }
      expect(plan.pit_windows.length).toBe(plan.stops);
    }

    // Fewest stops first, then the softer compound earlier.
    const order = payload.plans.map((p) => [p.stops, p.compounds.join(',')] as const);
    expect(order).toEqual(
      [...order].sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1]))
    );
    expect(payload.plans[0]?.stops).toBe(1);
    expect(payload.plans[0]?.compounds).toEqual([16, 18]);

    // Every compound is feasible on wear alone and none of them is time-plannable.
    for (const compound of payload.compounds) {
      expect(compound.feasible).toBe(true);
      expect(compound.plannable).toBe(false);
      expect(compound.pace).toBeNull();
      expect(compound.wear?.pct_per_lap).toBeGreaterThan(0);
    }
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

  it('badges a time-ranked plan set as ranked on pace models', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const badge = await screen.findByTestId('plans-ranking');
    expect(badge.getAttribute('data-ranking')).toBe('time');
    expect(badge.textContent).toContain('ranked on pace models');

    // And rank 1 really does carry a time and a "fastest" delta.
    const best = document.querySelector('[data-plan="1"]');
    expect(best!.textContent).toContain('fastest');
    const expected = mockStrategy(BAHRAIN, 20) as StrategyResponse;
    expect(expected.plans[0]!.total_time_ms).not.toBeNull();
  });

  it('marks a derived slope in the degradation column, distinctly from a fitted one', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const table = await screen.findByTestId('compound-table');
    // The hard only ever did a short run: its slope came from the circuit's coefficient.
    const derived = await screen.findByTestId('derived-18');
    expect(derived.textContent).toContain('derived');
    expect(derived.getAttribute('title')).toContain('ms per % of wear');
    // A fitted compound carries no such marker.
    const medium = table.querySelector('tr[data-compound="17"]');
    expect(medium!.querySelector('[data-testid="derived-17"]')).toBeNull();
    // The derived row names the calibration stint and where its base pace came from.
    const hard = table.querySelector('tr[data-compound="18"]');
    expect(hard!.textContent).toContain('calibration:');
    expect(hard!.textContent).toContain('base: median of');
    // ...and shows an em-dash for r², because it never fitted one.
    expect(hard!.querySelectorAll('td')[3]!.textContent).toContain(DASH);
  });

  it('states the circuit coefficient and the assumption it rests on', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${BAHRAIN}&laps=20`);
    render(App);

    const line = await screen.findByTestId('wear-calibration');
    // Template line breaks are whitespace, not content.
    const text = (line.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).toContain('ms per % of wear');
    expect(text).toContain('from the Medium stint in Race (session 3)');
    expect(text).toContain('assumption, not a measurement');
  });

  it('badges a rule-ranked plan set and leaves every projected time empty', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${MONTREAL_TRACK_ID}`);
    render(App);

    const badge = await screen.findByTestId('plans-ranking');
    expect(badge.getAttribute('data-ranking')).toBe('heuristic');
    expect(badge.textContent).toContain('ranked by rule');
    expect(badge.textContent).toContain('times not projected');

    const note = screen.getByTestId('plans-ranking-note');
    expect(note.textContent).toContain('fewest stops first');

    const expected = mockStrategy(MONTREAL_TRACK_ID, null) as StrategyResponse;
    await waitFor(() => {
      expect(document.querySelectorAll('.plan').length).toBe(expected.plans.length);
    });

    // Rank 1 is first under a rule, not fastest, and the page must not claim otherwise.
    const best = document.querySelector('[data-plan="1"]')!;
    expect(best.textContent).not.toContain('fastest');
    expect(best.querySelector('.delta .clock')!.textContent!.trim()).toBe(DASH);
    // The stints, the box laps and the wear are all still drawn.
    expect(best.querySelectorAll('.bar').length).toBe(expected.plans[0]!.stints.length);
    expect(best.textContent).toContain('Box 1');
    expect(best.textContent).toContain('Safety car');
  });

  it('says why a practice-only weekend has no pace models at all', async () => {
    restore = installAnalysisMock();
    goto(`#/strategy?track=${MONTREAL_TRACK_ID}`);
    render(App);

    const table = await screen.findByTestId('compound-table');
    for (const compound of [16, 17, 18]) {
      const row = table.querySelector(`tr[data-compound="${compound}"]`)!;
      // Em-dash where the slope would be, no "derived" marker, and the reason in words.
      expect(row.querySelectorAll('td')[2]!.textContent).toContain(DASH);
      expect(row.querySelector(`[data-testid="derived-${compound}"]`)).toBeNull();
      expect(row.textContent).toContain('no pace model');
      expect(row.textContent).toContain('feasibility plans');
      // The wear rate, which is what makes the plans possible, is shown as a real number.
      expect(row.textContent).toMatch(/\d+\.\d%/);
    }
    expect(screen.getByTestId('wear-calibration-omitted').textContent).toContain(
      'no wear-to-time coefficient'
    );
  });
});

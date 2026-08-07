import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import { HEADLINE_SESSION_ID, installAnalysisMock, mockDebrief } from '../lib/analysis-mock';

/**
 * The debrief card on the session detail page.
 *
 * The bug shape this file exists to catch is a session with no debrief rendering as a
 * *failure*. A 404 from `/api/sessions/{id}/debrief` is the normal answer for a session
 * nobody has debriefed yet, and the page has to say so in its own words rather than putting
 * up the "Not found" error panel — which would read as though the session were broken.
 *
 * Driven through the real `fetch` boundary, like every other route test here: the page's own
 * loading path runs, with the mock answering instead of a backend.
 */

let restore: (() => void) | null = null;

function goto(hash: string): void {
  window.location.hash = hash;
}

beforeEach(() => {
  analysis.clearSelection();
  analysis.clearWindow();
  goto('#/sessions');
});

afterEach(() => {
  cleanup();
  restore?.();
  restore = null;
  window.location.hash = '';
});

/** A fixture session that deliberately has no debrief, so the absent state is reachable. */
const SESSION_WITHOUT_DEBRIEF = 2;

describe('the debrief card', () => {
  it('renders the stored prose, with the model and the time it was written', async () => {
    restore = installAnalysisMock();
    goto(`#/sessions/${HEADLINE_SESSION_ID}`);
    const { container } = render(App);

    const panel = await waitFor(() => {
      const found = container.querySelector('[data-panel="debrief"] details');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });

    const expected = mockDebrief(HEADLINE_SESSION_ID);
    const body = await screen.findByTestId('debrief-text');
    expect(body.textContent).toBe(expected?.text);

    // The caption is provenance, and it is the reason the panel can be trusted at a glance:
    // which model wrote this, and when.
    const summary = panel.querySelector('summary');
    expect(summary?.textContent).toContain('Debrief');
    expect(summary?.textContent).toContain('example-model:8b');
  });

  it('keeps the paragraph breaks the backend emitted', async () => {
    // The text is rendered as plain text with `white-space: pre-wrap` — there is no markdown
    // renderer and no `{@html}` anywhere near it, so the newlines have to survive verbatim.
    restore = installAnalysisMock();
    goto(`#/sessions/${HEADLINE_SESSION_ID}`);
    render(App);

    const body = await screen.findByTestId('debrief-text');
    expect(body.textContent).toContain('\n\n');
  });

  it('is collapsed until it is opened', async () => {
    restore = installAnalysisMock();
    goto(`#/sessions/${HEADLINE_SESSION_ID}`);
    const { container } = render(App);

    const details = await waitFor(() => {
      const found = container.querySelector<HTMLDetailsElement>(
        '[data-panel="debrief"] details'
      );
      expect(found).not.toBeNull();
      return found as HTMLDetailsElement;
    });
    // The lap table is why you opened this page; the debrief waits to be asked for.
    expect(details.open).toBe(false);
  });

  it('says a session has no debrief yet instead of reporting an error', async () => {
    restore = installAnalysisMock();
    goto(`#/sessions/${SESSION_WITHOUT_DEBRIEF}`);
    const { container } = render(App);

    const slot = await waitFor(() => {
      const found = container.querySelector('[data-panel="debrief"] [data-state="empty"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });

    expect(slot.textContent).toContain('No debrief yet');
    // It names the command that would produce one, with this session's own id.
    expect(slot.textContent).toContain(`f126 debrief ${SESSION_WITHOUT_DEBRIEF}`);
    // And emphatically NOT the error panel.
    expect(container.querySelector('[data-panel="debrief"] [data-state="error"]')).toBeNull();
  });

  it('still reports a real failure as a failure', async () => {
    // A 503 is the database being down, not an absent debrief, and collapsing the two would
    // tell the driver "no debrief yet" while the whole backend is unreachable.
    restore = installAnalysisMock({ failStatus: 503 });
    goto(`#/sessions/${HEADLINE_SESSION_ID}`);
    const { container } = render(App);

    await waitFor(() => {
      const errors = container.querySelectorAll('[data-error-kind="unavailable"]');
      expect(errors.length).toBeGreaterThan(0);
    });
  });
});

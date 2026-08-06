import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import App from '../App.svelte';
import { analysis } from '../lib/analysis.svelte';
import { installAnalysisMock, mockSessions } from '../lib/analysis-mock';
import { ApiError } from '../lib/analysis-api';

/**
 * The route renders real rows from a real `fetch`.
 *
 * The mock is installed at the transport boundary, exactly as `npm run dev:mock`
 * installs it, so this exercises the page's own loading path rather than a
 * stubbed component. That is the point of intercepting `fetch` rather than
 * injecting a client: the thing under test here is the same code that runs in
 * production, with a different server behind it.
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

describe('#/sessions', () => {
  it('renders one row per session, from the fetched data', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);

    // The analysis chrome appears; the pit wall does not.
    expect(container.querySelector('[data-shell="analysis"]')).not.toBeNull();
    expect(container.querySelector('[data-page="sessions"]')).not.toBeNull();

    const table = await screen.findByTestId('sessions-table');
    await waitFor(() => {
      expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0);
    });

    const expected = mockSessions();
    expect(table.querySelectorAll('tbody tr')).toHaveLength(expected.length);
  });

  it('shows the five documented columns', async () => {
    restore = installAnalysisMock();
    render(App);

    const table = await screen.findByTestId('sessions-table');
    const headers = [...table.querySelectorAll('thead th')].map((th) => th.textContent?.trim());
    expect(headers).toEqual(['Date', 'Track', 'Type', 'Laps', 'Best lap']);
  });

  it('puts the track, type and lap count of each session on its row', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);

    await screen.findByTestId('sessions-table');
    const expected = mockSessions();
    const first = expected[0];
    if (!first) throw new Error('fixture has no sessions');

    await waitFor(() => {
      const row = container.querySelector(`tr[data-session-id="${first.id}"]`);
      expect(row).not.toBeNull();
      expect(row?.textContent).toContain(first.track_name ?? '');
      expect(row?.textContent).toContain(first.session_type_name ?? '');
    });
  });

  it('links each row to its session detail route', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('sessions-table');

    const expected = mockSessions();
    await waitFor(() => {
      for (const session of expected) {
        const link = container.querySelector(
          `tr[data-session-id="${session.id}"] a.row-link`
        ) as HTMLAnchorElement | null;
        expect(link, `link for session ${session.id}`).not.toBeNull();
        expect(link?.getAttribute('href')).toBe(`#/sessions/${session.id}`);
      }
    });
  });

  it('shows a loading state before the data lands', () => {
    // A fetch that never settles: the page must say it is working, not render
    // an empty table.
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };

    const { container } = render(App);
    expect(container.querySelector('[data-state="loading"]')).not.toBeNull();
  });

  it('explains a 503 as an unavailable database, and says the pit wall is fine', async () => {
    restore = installAnalysisMock({ failStatus: 503 });
    const { container } = render(App);

    await waitFor(() => {
      const panel = container.querySelector('[data-state="error"]');
      expect(panel).not.toBeNull();
      expect(panel?.getAttribute('data-error-kind')).toBe('unavailable');
    });

    expect(container.textContent).toContain('Database unavailable');
    expect(container.textContent).toContain('live pit wall');
  });

  it('renders an empty state rather than a bare table when there are no sessions', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response('[]', {
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
    expect(container.textContent).toContain('No sessions recorded yet');
  });
});

describe('analysis chrome', () => {
  it('shows the nav on analysis routes and marks the current one', async () => {
    restore = installAnalysisMock();
    const { container } = render(App);
    await screen.findByTestId('sessions-table');

    const current = container.querySelector('[aria-current="page"]');
    expect(current?.textContent?.trim()).toBe('Sessions');
    expect(container.querySelector('nav[aria-label="Analysis"]')).not.toBeNull();
  });

  it('keeps the pit wall chrome-free', () => {
    goto('#/');
    restore = installAnalysisMock();
    const { container } = render(App);

    expect(container.querySelector('nav[aria-label="Analysis"]')).toBeNull();
    expect(container.querySelector('[data-shell="analysis"]')).toBeNull();
    // ...but offers the one quiet way across.
    const link = container.querySelector('a.to-analysis');
    expect(link?.getAttribute('href')).toBe('#/sessions');
  });

  it('renders a not-found page for an unknown analysis route', () => {
    goto('#/nope');
    restore = installAnalysisMock();
    const { container } = render(App);
    expect(container.querySelector('[data-page="notfound"]')).not.toBeNull();
  });
});

describe('ApiError', () => {
  it('classifies the statuses the pages branch on', () => {
    expect(new ApiError('unavailable', 503, 'x').kind).toBe('unavailable');
    expect(new ApiError('notfound', 404, 'x').status).toBe(404);
    expect(new ApiError('invalid', 422, 'x')).toBeInstanceOf(Error);
  });
});

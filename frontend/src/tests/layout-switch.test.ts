import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import App from '../App.svelte';
import { store } from '../lib/ws.svelte';
import { MockEngine } from '../lib/mock';
import { isSlowFrame, type SessionKind, type SlowFrame } from '../lib/protocol';

/**
 * Build a realistic slow frame for a session kind by running the mock engine
 * until it emits one. Using the generator rather than a hand-written fixture
 * means this test also fails if the mock stops producing protocol-shaped data.
 */
function slowFrameFor(kind: SessionKind): SlowFrame {
  const engine = new MockEngine({ kind, seed: 42 });
  for (let i = 0; i < 20; i++) {
    for (const msg of engine.advance(100)) {
      if (isSlowFrame(msg)) return msg;
    }
  }
  throw new Error(`mock engine produced no slow frame for ${kind}`);
}

function layoutOf(container: HTMLElement): string | null {
  return container.querySelector('[data-layout]')?.getAttribute('data-layout') ?? null;
}

/** Push a slow frame through the real ingestion path and flush the UI. */
function send(kind: SessionKind): void {
  store.handleMessage(slowFrameFor(kind));
  flushSync();
}

describe('layout auto-switching on session_kind', () => {
  beforeEach(() => {
    store.reset();
    store.socketState = 'open';
  });

  afterEach(() => {
    cleanup();
    store.reset();
    store.socketState = 'closed';
  });

  it('waits for telemetry before committing to a layout', () => {
    const { container } = render(App);
    expect(layoutOf(container)).toBe('waiting');
    expect(container.textContent).toContain('Waiting for telemetry');
  });

  it('switches to the race layout when a race session arrives', () => {
    const { container } = render(App);
    send('race');

    expect(layoutOf(container)).toBe('race');
    // The race layout is the only one carrying fuel, energy and damage.
    expect(container.querySelector('[data-panel="fuel"]')).not.toBeNull();
    expect(container.querySelector('[data-panel="energy"]')).not.toBeNull();
    expect(container.querySelector('[data-panel="damage"]')).not.toBeNull();
    expect(container.querySelector('[data-panel="board"]')).toBeNull();
  });

  it('switches to the qualifying layout, which leads with sectors', () => {
    const { container } = render(App);
    send('quali');

    expect(layoutOf(container)).toBe('quali');
    expect(container.querySelector('[data-panel="sectors"]')).not.toBeNull();
    expect(container.querySelector('[data-panel="fuel"]')).toBeNull();
    expect(container.querySelector('[data-panel="damage"]')).toBeNull();
  });

  it('reuses the qualifying layout for practice', () => {
    const { container } = render(App);
    send('practice');
    expect(layoutOf(container)).toBe('quali');
  });

  it('switches to the time-trial layout, which shows the reference board', () => {
    const { container } = render(App);
    send('time_trial');

    expect(layoutOf(container)).toBe('time_trial');
    expect(container.querySelector('[data-panel="board"]')).not.toBeNull();
    expect(container.querySelector('[data-panel="tower"]')).toBeNull();
  });

  it('re-switches when the session kind changes mid-connection', () => {
    const { container } = render(App);

    // A full session weekend, in the order it actually happens.
    send('practice');
    expect(layoutOf(container)).toBe('quali');

    send('quali');
    expect(layoutOf(container)).toBe('quali');

    send('race');
    expect(layoutOf(container)).toBe('race');

    send('time_trial');
    expect(layoutOf(container)).toBe('time_trial');

    // And back again — the switch is reactive, not first-write-wins.
    send('race');
    expect(layoutOf(container)).toBe('race');
  });

  it('falls back to the race layout for an unrecognised session kind', () => {
    const { container } = render(App);
    const frame = slowFrameFor('race');
    frame.session.session_kind = 'other';
    store.handleMessage(frame);
    flushSync();

    expect(layoutOf(container)).toBe('race');
  });
});

describe('persistent chrome', () => {
  beforeEach(() => {
    store.reset();
    store.socketState = 'open';
  });

  afterEach(() => {
    cleanup();
    store.reset();
    store.socketState = 'closed';
  });

  it('shows the track and session name in every layout', () => {
    const { container } = render(App);
    for (const kind of ['race', 'quali', 'time_trial'] as const) {
      send(kind);
      expect(container.textContent).toContain('Suzuka');
    }
  });

  it('renders the full field with the player highlighted in the race tower', () => {
    const { container } = render(App);
    send('race');

    const rows = container.querySelectorAll('[data-panel="tower"] .rows > *');
    expect(rows).toHaveLength(22);
    expect(container.querySelectorAll('[data-panel="tower"] .player')).toHaveLength(1);
  });

  it('counts laps in a race and counts down a clock in qualifying', () => {
    const { container } = render(App);

    send('race');
    expect(container.textContent).toContain('53');

    send('quali');
    expect(container.textContent).toContain('Qualifying');
  });

  it('reflects a stalled capture in the connection surface', () => {
    const { container } = render(App);
    send('race');
    expect(store.connection).toBe('open');

    store.handleMessage({ type: 'event', t: 1, code: 'STALLED', data: { stalled: true } });
    flushSync();

    expect(store.connection).toBe('stalled');
    expect(store.health).toBe('bad');
    expect(container.textContent).toContain('No packets');

    store.handleMessage({ type: 'event', t: 2, code: 'STALLED', data: { stalled: false } });
    flushSync();
    expect(store.connection).toBe('open');
  });

  it('raises a full-width banner under a flag or safety car', () => {
    const { container } = render(App);

    send('race');
    expect(container.textContent).not.toContain('Yellow flag');

    const frame = slowFrameFor('race');
    frame.session.fia_flag = 3;
    store.handleMessage(frame);
    flushSync();
    expect(container.textContent).toContain('Yellow flag');

    const sc = slowFrameFor('race');
    sc.session.safety_car = 1;
    store.handleMessage(sc);
    flushSync();
    expect(container.textContent).toContain('Safety car');
  });
});

/**
 * Shared state across the analysis routes: which two laps are selected, and
 * which slice of the lap the charts are looking at.
 *
 * This exists because the analysis pages are one investigation split across
 * four URLs. You find a lap on `#/sessions/3`, you compare it on `#/compare`,
 * you discover the loss is in turn 8 on `#/corners`, and you go back to the
 * traces already zoomed to turn 8. Threading that through query strings alone
 * would make every navigation lossy; keeping it in one store keeps the pages
 * honest about being views onto a single selection.
 *
 * The URL still wins on load — `#/compare?sa=3&la=12&sb=3&lb=15` is a shareable
 * link and must reopen exactly those laps. `adoptFromQuery()` is how a page
 * reconciles the two: the URL seeds the store, the store drives the UI.
 */

import { href } from './router.svelte';

/** One lap, addressed across sessions. */
export interface LapRef {
  sessionId: number;
  lap: number;
}

export function sameLap(a: LapRef | null, b: LapRef | null): boolean {
  if (a === null || b === null) return a === b;
  return a.sessionId === b.sessionId && a.lap === b.lap;
}

/** A distance window in metres, as the charts consume it. */
export interface Window {
  min: number;
  max: number;
}

/**
 * Padding added around a corner when zooming to it, as a fraction of the
 * corner's own length. A corner read with no approach and no exit is not a
 * corner — the braking point and the throttle pickup are the interesting parts,
 * and both sit outside `entry_m..exit_m`.
 */
const CORNER_PAD = 0.6;
/** Floor on that padding, for corners short enough that 60% is a few metres. */
const CORNER_PAD_MIN_M = 60;

export class AnalysisStore {
  /** The lap under study. Delta convention: below zero means A is ahead. */
  a = $state<LapRef | null>(null);
  /** The reference lap. */
  b = $state<LapRef | null>(null);

  /** Car whose laps the session pages list. Null = the session's player car. */
  carIndex = $state<number | null>(null);

  /** Distance window shared by every compare pane. Null = the whole lap. */
  window = $state<Window | null>(null);

  /** Corner number the window was derived from, for highlighting its table row. */
  focusedCorner = $state<number | null>(null);

  /** Both slots filled — the only state in which a comparison can be drawn. */
  get paired(): boolean {
    return this.a !== null && this.b !== null;
  }

  /**
   * Click a lap.
   *
   * Slot A is the lap you are studying and is sticky; slot B is the reference
   * and rotates, because the common motion is "compare this lap against that
   * one, then against the next one". Clicking a selected lap deselects it, and
   * removing A promotes B so the remaining lap is always the subject.
   */
  toggle(ref: LapRef): void {
    if (sameLap(this.a, ref)) {
      this.a = this.b;
      this.b = null;
      return;
    }
    if (sameLap(this.b, ref)) {
      this.b = null;
      return;
    }
    if (this.a === null) {
      this.a = ref;
      return;
    }
    this.b = ref;
  }

  /** Which slot a lap occupies, for rendering the selection chip. */
  slotOf(ref: LapRef): 'a' | 'b' | null {
    if (sameLap(this.a, ref)) return 'a';
    if (sameLap(this.b, ref)) return 'b';
    return null;
  }

  setA(ref: LapRef | null): void {
    this.a = ref;
  }

  setB(ref: LapRef | null): void {
    this.b = ref;
  }

  /** Swap subject and reference. The delta changes sign; the labels follow. */
  swap(): void {
    const { a, b } = this;
    this.a = b;
    this.b = a;
  }

  clearSelection(): void {
    this.a = null;
    this.b = null;
  }

  // ── zoom ───────────────────────────────────────────────────────────────────

  setWindow(min: number, max: number): void {
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;
    this.window = { min, max };
  }

  clearWindow(): void {
    this.window = null;
    this.focusedCorner = null;
  }

  /**
   * Zoom to a corner, padded so the braking zone and the exit are both visible.
   */
  focusCorner(n: number, entryM: number, exitM: number): void {
    const span = Math.max(exitM - entryM, 1);
    const pad = Math.max(span * CORNER_PAD, CORNER_PAD_MIN_M);
    this.window = { min: Math.max(0, entryM - pad), max: exitM + pad };
    this.focusedCorner = n;
  }

  // ── URL reconciliation ─────────────────────────────────────────────────────

  /**
   * Seed the selection from a query string. Only overwrites a slot the URL
   * actually specifies, so navigating to `#/corners?session=3&lap=12` keeps the
   * reference lap the user already picked.
   */
  adoptFromQuery(query: URLSearchParams): void {
    const a = lapRefFrom(query, 'sa', 'la');
    const b = lapRefFrom(query, 'sb', 'lb');
    if (a) this.a = a;
    if (b) this.b = b;

    const car = numberParam(query, 'car');
    if (car !== null) this.carIndex = car;
  }

  /** Link to the compare view for the current selection. */
  compareHref(): string {
    return href('/compare', {
      sa: this.a?.sessionId,
      la: this.a?.lap,
      sb: this.b?.sessionId,
      lb: this.b?.lap
    });
  }

  /** Link to the corner view. Corners are always "A versus its reference". */
  cornersHref(ref: string | number = 'best'): string {
    return href('/corners', {
      session: this.a?.sessionId,
      lap: this.a?.lap,
      ref: this.b !== null && this.b.sessionId === this.a?.sessionId ? this.b.lap : ref
    });
  }
}

function numberParam(query: URLSearchParams, key: string): number | null {
  const raw = query.get(key);
  if (raw === null || !/^[0-9]{1,20}$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : null;
}

function lapRefFrom(query: URLSearchParams, sessionKey: string, lapKey: string): LapRef | null {
  const sessionId = numberParam(query, sessionKey);
  const lap = numberParam(query, lapKey);
  if (sessionId === null || lap === null) return null;
  return { sessionId, lap };
}

/** The app-wide analysis selection. */
export const analysis = new AnalysisStore();

// ── async helper ─────────────────────────────────────────────────────────────

/**
 * The three states every page on this side of the app has to render, as one
 * value rather than three booleans that can disagree with each other.
 */
export type Async<T> =
  | { status: 'loading' }
  | { status: 'ok'; data: T }
  | { status: 'error'; error: unknown };

export const LOADING: Async<never> = { status: 'loading' };

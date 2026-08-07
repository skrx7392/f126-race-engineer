/**
 * A hash router in ~120 lines, because that is all this app needs.
 *
 * The pit wall is a kiosk: it is opened once and left running. Analysis is the
 * opposite — it is browsed, linked to, and reloaded. Hash routing is what lets
 * both live in one bundle served from `frontend/dist` at any mount path, with
 * no server rewrite rules and no history-API fallback to configure. A pasted
 * `#/compare?...` URL reopens the exact same two laps.
 *
 * Parsing is a pure function so it is testable without a DOM, and the reactive
 * surface is one `$state` object that swaps wholesale — routes are values, not
 * a bag of independently-mutating fields.
 */

export type RouteName =
  | 'pitwall'
  | 'sessions'
  | 'session'
  | 'compare'
  | 'corners'
  | 'stints'
  | 'strategy'
  | 'notfound';

export interface Route {
  name: RouteName;
  /** Normalised path with no leading `#` and no trailing slash: `/sessions/3`. */
  path: string;
  /** Named segments captured from the pattern, e.g. `{ id: '3' }`. */
  params: Readonly<Record<string, string>>;
  /** Everything after `?`. Always present, possibly empty. */
  query: URLSearchParams;
}

/** Routes that wear the analysis chrome. The pit wall stays bare. */
const ANALYSIS_ROUTES: ReadonlySet<RouteName> = new Set([
  'sessions',
  'session',
  'compare',
  'corners',
  'stints',
  'strategy',
  'notfound'
]);

export function isAnalysisRoute(name: RouteName): boolean {
  return ANALYSIS_ROUTES.has(name);
}

/** Session ids are bigints server-side; accept digits only and keep them as strings. */
const ID_RE = /^[0-9]{1,20}$/;

/**
 * Parse a location hash into a route.
 *
 * Accepts `#/sessions/3?car=21`, `/sessions/3`, `sessions/3`, `''` — all the
 * shapes `location.hash` actually takes across browsers and reloads.
 */
export function parseHash(hash: string): Route {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  const qIndex = raw.indexOf('?');
  const rawPath = qIndex === -1 ? raw : raw.slice(0, qIndex);
  const query = new URLSearchParams(qIndex === -1 ? '' : raw.slice(qIndex + 1));

  const segments = rawPath.split('/').filter((s) => s.length > 0);
  const path = '/' + segments.join('/');
  const empty: Record<string, string> = {};

  if (segments.length === 0) return { name: 'pitwall', path: '/', params: empty, query };

  const [head, second, ...rest] = segments;

  if (head === 'sessions') {
    if (second === undefined) return { name: 'sessions', path, params: empty, query };
    // `/sessions/3/anything` is not a route; refuse rather than guess.
    if (rest.length === 0 && ID_RE.test(second)) {
      return { name: 'session', path, params: { id: second }, query };
    }
    return { name: 'notfound', path, params: empty, query };
  }

  if (segments.length === 1) {
    if (head === 'compare') return { name: 'compare', path, params: empty, query };
    if (head === 'corners') return { name: 'corners', path, params: empty, query };
    if (head === 'stints') return { name: 'stints', path, params: empty, query };
    if (head === 'strategy') return { name: 'strategy', path, params: empty, query };
  }

  return { name: 'notfound', path, params: empty, query };
}

/** Build a hash URL from a path and query pairs, dropping null/undefined values. */
export function href(path: string, query: Record<string, string | number | null | undefined> = {}): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return `#${path}${qs ? `?${qs}` : ''}`;
}

/** Read a positive integer query param, or null when absent/malformed. */
export function intParam(query: URLSearchParams, key: string): number | null {
  const raw = query.get(key);
  if (raw === null || !ID_RE.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : null;
}

export class Router {
  route = $state<Route>(parseHash(''));
  #stop: (() => void) | null = null;

  constructor(initial?: string) {
    if (initial !== undefined) this.route = parseHash(initial);
  }

  /** Begin tracking `location.hash`. Returns a stop function. */
  start(): () => void {
    if (typeof window === 'undefined') return () => {};
    const sync = (): void => {
      this.route = parseHash(window.location.hash);
    };
    sync();
    window.addEventListener('hashchange', sync);
    this.#stop = () => window.removeEventListener('hashchange', sync);
    return this.#stop;
  }

  stop(): void {
    this.#stop?.();
    this.#stop = null;
  }

  /** Programmatic navigation. `to` is a hash URL such as `#/sessions/3`. */
  go(to: string): void {
    if (typeof window === 'undefined') {
      this.route = parseHash(to);
      return;
    }
    const next = to.startsWith('#') ? to : `#${to}`;
    if (window.location.hash === next) {
      // Assigning an identical hash fires no `hashchange`; sync by hand so a
      // repeated click on the same link is still idempotent rather than dead.
      this.route = parseHash(next);
      return;
    }
    window.location.hash = next;
  }
}

/** The app-wide router. */
export const router = new Router();

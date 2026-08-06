/**
 * The read-only analysis API, as TypeScript.
 *
 * Every shape here is transcribed from docs/analysis-api.md (frozen field
 * names, additive changes only) plus the Phase 1 endpoints it builds on. Where
 * the backend returns a raw database row, the optionality mirrors the column's
 * nullability rather than what a happy path happens to produce — a lap with no
 * sector data is a normal lap, not an error.
 *
 * The endpoints are all GET. The project invariant is that there is no mutating
 * HTTP surface, ever, so there is no verb parameter and no request body here.
 */

// ── Phase 1 shapes ───────────────────────────────────────────────────────────

/** A row from `GET /api/sessions`. */
export interface SessionSummary {
  id: number;
  session_uid: string;
  segment: number;
  packet_format: number | null;
  session_type: number | null;
  session_type_name: string | null;
  track_id: number | null;
  track_name: string | null;
  /** Epoch seconds, fractional. Null while a session is still being written. */
  started_at_wall: number | null;
  ended_at_wall: number | null;
  ended_reason: string | null;
  joined_in_progress: boolean | null;
  player_car_index: number | null;
  total_laps: number | null;
  session_duration_s: number | null;
  weather_json: unknown;
  final_classification_json: unknown;
  raw_file: string | null;
  /** Computed: `count(DISTINCT (car_index, lap_number))`. */
  lap_count: number;
  player_name: string | null;
  /**
   * Not part of the list contract today — `best_lap_ms` is computed only by the
   * detail endpoint. Typed optional so the session table can show the column
   * the moment the backend adds it (an additive change), and an em dash until
   * then, rather than forcing an N+1 detail fetch per row.
   */
  best_lap_ms?: number | null;
}

export interface Participant {
  car_index: number;
  name: string | null;
  team_id: number | null;
  race_number: number | null;
  is_ai: boolean | null;
  is_player: boolean | null;
}

export interface TyreStintRow {
  car_index: number;
  stint_no: number;
  compound_actual: number | null;
  compound_visual: number | null;
  lap_start: number | null;
  lap_end: number | null;
  wear_at_end_json: unknown;
  end_reason: string | null;
}

/** `GET /api/sessions/{id}` — the list row plus counts and nested collections. */
export interface SessionDetail extends SessionSummary {
  telemetry_sample_count: number;
  wear_sample_count: number;
  event_count: number;
  /** `min(lap_time_ms)` over valid laps; null when the session has none. */
  best_lap_ms: number | null;
  participants: Participant[];
  tyre_stints: TyreStintRow[];
}

/** A row from `GET /api/sessions/{id}/laps`. */
export interface Lap {
  session_id: number;
  car_index: number;
  lap_number: number;
  generation: number;
  lap_time_ms: number | null;
  s1_ms: number | null;
  s2_ms: number | null;
  s3_ms: number | null;
  valid: boolean | null;
  compound_actual: number | null;
  compound_visual: number | null;
  tyre_age_laps: number | null;
  fuel_start_kg: number | null;
  fuel_end_kg: number | null;
  ers_deployed_j: number | null;
  ers_harvested_j: number | null;
  top_speed_kmh: number | null;
  penalties_s: number | null;
  wall_ts: number | null;
}

// ── Phase 2 shapes ───────────────────────────────────────────────────────────

/** `GET /api/sessions/{id}/laps/{lap}/telemetry` — one lap at 20 Hz, undownsampled. */
export interface LapTelemetry {
  session_id: number;
  lap_number: number;
  car_index: number;
  points: number;
  distance_m: number[];
  speed_kmh: number[];
  throttle: number[];
  brake: number[];
  steer: number[];
  gear: number[];
  rpm: number[];
  drs_or_aero: number[];
  session_time_s: number[];
}

/** One side of a comparison, resampled onto the shared grid. */
export interface CompareSide {
  session_id: number;
  lap_number: number;
  lap_time_ms: number | null;
  speed_kmh: number[];
  throttle: number[];
  brake: number[];
  gear: number[];
}

/** `GET /api/analysis/compare` — two laps on a common ~5 m distance grid. */
export interface CompareResponse {
  track_id: number | null;
  track_name: string | null;
  grid_m: number[];
  a: CompareSide;
  b: CompareSide;
  /** Cumulative a-vs-b time delta along the grid. Negative = a ahead. */
  delta_ms: number[];
  sectors_a: Array<number | null>;
  sectors_b: Array<number | null>;
}

export type CornerKind = 'slow' | 'medium' | 'fast';

export interface Corner {
  n: number;
  entry_m: number;
  apex_m: number;
  exit_m: number;
  min_speed_kmh: number | null;
  ref_min_speed_kmh: number | null;
  brake_point_m: number | null;
  ref_brake_point_m: number | null;
  /** Positive = slower than the reference through this corner. */
  time_loss_ms: number;
  kind: CornerKind;
}

/** `GET /api/analysis/corners`. */
export interface CornersResponse {
  session_id: number;
  lap_number: number;
  ref_lap_number: number;
  corners: Corner[];
  straights_time_loss_ms: number;
  total_delta_ms: number;
}

export interface StintLap {
  lap_number: number;
  lap_time_ms: number | null;
  valid: boolean;
  excluded: boolean;
  /**
   * Why a lap was excluded from the fit. Not in the frozen contract — the doc
   * says excluded laps are "flagged, not hidden" but does not name the flag, so
   * this is read when present and degrades to a generic reason when absent.
   */
  exclude_reason?: string | null;
}

export interface StintFit {
  deg_ms_per_lap: number;
  base_ms: number;
  r2: number;
  n_used: number;
}

export interface Stint {
  stint_no: number;
  car_index: number;
  compound_visual: number | null;
  lap_start: number;
  lap_end: number;
  laps: StintLap[];
  fit: StintFit | null;
  /** `[rl, rr, fl, fr]`, matching the protocol's wheel order. */
  wear_end_pct: number[] | null;
}

/** `GET /api/analysis/stints`. */
export interface StintsResponse {
  session_id: number;
  stints: Stint[];
}

// ── transport ────────────────────────────────────────────────────────────────

/**
 * Why a request failed, in the vocabulary the UI actually branches on.
 *
 * `unavailable` is the one that gets its own panel: the backend collapses every
 * database problem into an opaque 503 (`{"detail": "database unavailable"}`)
 * because the endpoint is unauthenticated, so the client cannot say more than
 * "the archive is not reachable" — and should not pretend to.
 */
export type ApiErrorKind = 'unavailable' | 'notfound' | 'invalid' | 'network' | 'other';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;

  constructor(kind: ApiErrorKind, status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
  }
}

function kindForStatus(status: number): ApiErrorKind {
  if (status === 503) return 'unavailable';
  if (status === 404) return 'notfound';
  if (status === 422) return 'invalid';
  return 'other';
}

/** Pull FastAPI's `{"detail": ...}` out of a response body, tolerating anything. */
function detailOf(body: unknown, fallback: string): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    // 422 bodies carry an array of validation errors; the first message is the
    // only part a person can act on.
    if (Array.isArray(d) && d.length > 0) {
      const first = d[0] as { msg?: unknown };
      if (typeof first?.msg === 'string') return first.msg;
    }
  }
  return fallback;
}

/**
 * GET one JSON document, or throw an `ApiError`.
 *
 * Relative to the document base, matching the pit wall's same-origin
 * assumption: the backend serves this bundle and the API from one origin, so
 * there is no base URL to configure and nothing to get wrong in deployment.
 */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: 'application/json' }, signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError('network', 0, 'Could not reach the server');
  }

  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* an error response without a JSON body is still an error */
    }
    const kind = kindForStatus(res.status);
    throw new ApiError(kind, res.status, detailOf(body, `Request failed (${res.status})`));
  }

  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError('other', res.status, 'Malformed response');
  }
}

// ── endpoints ────────────────────────────────────────────────────────────────

const q = (params: Record<string, string | number | null | undefined>): string => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined) continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
};

export const fetchSessions = (limit = 50, signal?: AbortSignal): Promise<SessionSummary[]> =>
  apiGet<SessionSummary[]>(`/api/sessions${q({ limit })}`, signal);

export const fetchSession = (id: number, signal?: AbortSignal): Promise<SessionDetail> =>
  apiGet<SessionDetail>(`/api/sessions/${id}`, signal);

export const fetchLaps = (
  id: number,
  carIndex?: number | null,
  signal?: AbortSignal
): Promise<Lap[]> => apiGet<Lap[]>(`/api/sessions/${id}/laps${q({ car_index: carIndex })}`, signal);

export const fetchTelemetry = (
  id: number,
  lap: number,
  carIndex?: number | null,
  signal?: AbortSignal
): Promise<LapTelemetry> =>
  apiGet<LapTelemetry>(
    `/api/sessions/${id}/laps/${lap}/telemetry${q({ car_index: carIndex })}`,
    signal
  );

export const fetchCompare = (
  sessionA: number,
  lapA: number,
  sessionB: number,
  lapB: number,
  signal?: AbortSignal
): Promise<CompareResponse> =>
  apiGet<CompareResponse>(
    `/api/analysis/compare${q({ session_a: sessionA, lap_a: lapA, session_b: sessionB, lap_b: lapB })}`,
    signal
  );

export const fetchCorners = (
  sessionId: number,
  lap: number,
  ref: string | number = 'best',
  signal?: AbortSignal
): Promise<CornersResponse> =>
  apiGet<CornersResponse>(
    `/api/analysis/corners${q({ session_id: sessionId, lap, ref })}`,
    signal
  );

export const fetchStints = (sessionId: number, signal?: AbortSignal): Promise<StintsResponse> =>
  apiGet<StintsResponse>(`/api/analysis/stints${q({ session_id: sessionId })}`, signal);

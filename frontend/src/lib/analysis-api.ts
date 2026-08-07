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
  /** Computed: `count(DISTINCT (car_index, lap_number))`, across every car. */
  lap_count: number;
  /**
   * Computed: the player car's own distinct lap count.
   *
   * Optional because only the list endpoint computes it — the detail query is a
   * single row and does not — so a page that needs it there counts the laps it
   * has already fetched instead.
   */
  player_lap_count?: number | null;
  /**
   * How many recorder segments this row aggregates (pauses, process restarts).
   * List endpoint only, for the same reason as `player_lap_count`.
   */
  segments?: number | null;
  /** The fastest valid lap by *any* car; `best_lap_ms` is the player's own. */
  field_best_lap_ms: number | null;
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
  ref_lap_number: number | null;
  /** Which session the reference lap came from. May differ from `session_id`. */
  ref_session_id?: number | null;
  /** Caption for a cross-session reference, e.g. "One-Shot Qualifying · Aug 07". */
  ref_session_label?: string | null;
  /**
   * The subject lap was the only thing available to compare against. The reference
   * columns are meaningless (±0, or null) and the page says so rather than showing them.
   */
  self_reference?: boolean;
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

// ── Phase 2.6: strategy ──────────────────────────────────────────────────────

/**
 * Which kind of running a number was measured in.
 *
 * `race` beats `practice` and the difference is not cosmetic: practice is push laps on new
 * tyres with a light car, and its degradation and wear come out at roughly twice race trim.
 * The tier travels with every model so a practice-derived number can be read as one.
 */
export type EvidenceTier = 'race' | 'practice';

/** Where a wear rate came from. Samples beat the stint's end reading. */
export type WearSource = 'wear_samples' | 'stint_end_wear';

/** One compound's lap-time model: the `stints` fit, with the stint it was read from. */
export interface StrategyPace {
  base_ms: number | null;
  deg_ms_per_lap: number | null;
  /**
   * Present only when the measured slope was negative and the planner clamped it to zero.
   * A set does not get quicker with age; that is fuel and track evolution showing through
   * a short stint, and projecting it forward would recommend running to the flag.
   */
  deg_ms_per_lap_planned?: number;
  r2: number | null;
  laps_used: number;
  evidence: EvidenceTier | null;
  session_id: number;
  session_label: string;
  stint_no: number | null;
  /** `[lap_start, lap_end]` of the stint this was fitted over. */
  lap_range: Array<number | null>;
}

/** One compound's wear rate, in telemetry percent of the worst wheel per lap. */
export interface StrategyWear {
  pct_per_lap: number | null;
  source: WearSource | null;
  laps: number;
  evidence: EvidenceTier | null;
  session_id: number;
  session_label: string;
  stint_no: number | null;
}

export interface StrategyCompound {
  compound_visual: number;
  name: string;
  /** False for intermediates and wets, which are never planned with. */
  dry: boolean;
  /** Nothing at this circuit ran this compound. Every model field is null. */
  untested: boolean;
  stints_seen: number;
  evidence: EvidenceTier | null;
  pace: StrategyPace | null;
  wear: StrategyWear | null;
  /** Laps before projected wear reaches the cliff. Null when the wear rate is unknown. */
  max_stint_laps: number | null;
  projected_wear_at_max_pct?: number;
  plannable: boolean;
  /** Present whenever `plannable` is false, in the sheet's own words. */
  not_plannable_reason: string | null;
}

export interface StrategyPlanStint {
  compound_visual: number;
  name: string;
  lap_start: number;
  lap_end: number;
  laps: number;
  projected_end_wear_pct: number | null;
}

/** Where a stop can be taken and still finish the race inside the wear ceiling. */
export interface StrategyPitWindow {
  stop: number;
  planned_lap: number;
  earliest_lap: number;
  latest_lap: number;
  window_laps: number;
}

export interface StrategySafetyCar {
  flexibility: 'flexible' | 'tight' | 'none';
  note: string;
}

export interface StrategyPlan {
  rank: number;
  stops: number;
  compounds: number[];
  label: string;
  stints: StrategyPlanStint[];
  total_time_ms: number;
  /** Projected loss against the best-ranked plan. Zero for rank 1, never negative. */
  delta_to_best_ms: number;
  pit_windows: StrategyPitWindow[];
  safety_car: StrategySafetyCar;
}

export interface StrategyFuel {
  kg_per_lap: number;
  laps_measured: number;
  evidence: EvidenceTier;
  session_ids: number[];
  race_laps: number;
  margin_laps: number;
  /** Laps of fuel to load: race distance plus the margin. */
  slider_laps: number;
  recommended_kg: number;
}

export interface StrategyPitStop {
  session_id: number;
  stop_after_stint: number;
  laps: number[];
  loss_s: number;
}

export interface StrategyPitLoss {
  seconds: number;
  /** `measured` from this circuit's own stop; `default` from a named constant. */
  source: 'measured' | 'default';
  stops_measured: number;
  stops: StrategyPitStop[];
  detail: string;
}

export interface StrategySessionUsed {
  id: number;
  session_type: number | null;
  session_type_name: string | null;
  evidence: EvidenceTier | null;
  started_at_wall: number | null;
  /** What this session contributed, e.g. `["fuel burn", "soft pace"]`. */
  contributed: string[];
}

/** `GET /api/analysis/strategy`. */
export interface StrategyResponse {
  track_id: number;
  track_name: string | null;
  race_laps: number;
  /** How `race_laps` was arrived at: `"request"`, or the session that named it. */
  race_laps_source: string;
  wear_cliff_pct: number;
  compounds: StrategyCompound[];
  /** Ranked fastest first. Empty when no legal plan exists; `omitted.plans` says why. */
  plans: StrategyPlan[];
  plans_considered: number;
  /** Null when the weekend recorded no usable fuel data; `omitted.fuel` says so. */
  fuel: StrategyFuel | null;
  pit_loss_s: number;
  pit_loss: StrategyPitLoss;
  sessions_used: StrategySessionUsed[];
  /** Section name -> why it could not be computed. A missing input never becomes a zero. */
  omitted: Record<string, string>;
}

/**
 * `GET /api/sessions/{id}/debrief` — the post-session note. **404 when none exists**, which
 * is the normal state of a session that was just recorded, not a failure.
 *
 * `fact_sheet` is the deterministic dict the text was written from: every number in `text`
 * came out of it, so the two together are an auditable record rather than an assertion. It
 * is `unknown` here because nothing in the UI reads inside it — it is carried so the panel
 * can say the numbers exist, and so a future page can show them.
 */
export interface Debrief {
  session_id: number;
  /** Epoch seconds, fractional. When the debrief was generated. */
  created_at: number | null;
  model: string | null;
  prompt_version: number | null;
  text: string | null;
  fact_sheet: unknown;
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
  signal?: AbortSignal,
  refSession?: number | null
): Promise<CornersResponse> =>
  apiGet<CornersResponse>(
    `/api/analysis/corners${q({
      session_id: sessionId,
      lap,
      ref,
      ref_session: refSession ?? undefined
    })}`,
    signal
  );

export const fetchStints = (sessionId: number, signal?: AbortSignal): Promise<StintsResponse> =>
  apiGet<StintsResponse>(`/api/analysis/stints${q({ session_id: sessionId })}`, signal);

/**
 * The strategy sheet for one circuit. `raceLaps` omitted lets the backend default to the
 * most recent race there; a circuit with no such race answers 422, which is a real answer
 * ("tell me how long the race is") and reaches the page as an `invalid` ApiError.
 */
export const fetchStrategy = (
  trackId: number,
  raceLaps?: number | null,
  signal?: AbortSignal
): Promise<StrategyResponse> =>
  apiGet<StrategyResponse>(
    `/api/analysis/strategy${q({ track_id: trackId, race_laps: raceLaps })}`,
    signal
  );

/**
 * The stored debrief, or `null` when the session has none.
 *
 * The 404 is folded into `null` here rather than at every call site: a session without a
 * debrief is an ordinary session, and rendering the "Not found" error panel over it would
 * report a missing paragraph as a broken page. Every other status still throws.
 */
export const fetchDebrief = async (
  id: number,
  signal?: AbortSignal
): Promise<Debrief | null> => {
  try {
    return await apiGet<Debrief>(`/api/sessions/${id}/debrief`, signal);
  } catch (err) {
    if (err instanceof ApiError && err.kind === 'notfound') return null;
    throw err;
  }
};

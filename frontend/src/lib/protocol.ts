/**
 * Wire types for the `/ws` endpoint, hand-written from docs/ws-protocol.md.
 *
 * Field names are frozen by that document; changes there are additive only. All
 * times are milliseconds unless the name carries a different suffix, `null`
 * means unknown/not-applicable, and every wheel array is ordered
 * `[rear-left, rear-right, front-left, front-right]`.
 *
 * The guards at the bottom are deliberately shallow. A pit-wall display that
 * blanks itself because one optional field arrived as `null` is worse than one
 * that renders the fields it understands, so the guards check the discriminator
 * and the few fields the layout switch depends on, then trust the contract.
 */

export const PROTOCOL_VERSION = 1;

/** Wheel-ordered tuple: `[rl, rr, fl, fr]`. */
export type Wheels = [number, number, number, number];
/** Same order, but individual corners may be unknown. */
export type WheelsMaybe = [number | null, number | null, number | null, number | null];

export type SessionKind = 'practice' | 'quali' | 'race' | 'time_trial' | 'other';

/** Which reference lap `fast.delta_best_ms` is measured against. */
export type DeltaKind = 'session_best' | 'personal_best' | 'race_best';

/** Sector/lap ranking colour, using F1's broadcast convention. */
export type PaceColor = 'purple' | 'green' | 'yellow';

export interface ForecastSample {
  offset_min: number;
  weather: number;
  rain_pct: number;
}

export interface SessionInfo {
  session_uid: string;
  segment: number;
  packet_format: number;
  /** Raw game enum; `session_kind` is the normalised form the UI switches on. */
  session_type: number;
  session_kind: SessionKind;
  track_id: number;
  track_name: string;
  time_left_s: number | null;
  duration_s: number | null;
  total_laps: number | null;
  /** 0 none, 1 full, 2 virtual, 3 formation. */
  safety_car: number;
  /** -1 unknown, 0 none, 1 green, 2 blue, 3 yellow, 4 red. */
  fia_flag: number;
  weather: number;
  track_temp_c: number | null;
  air_temp_c: number | null;
  forecast: ForecastSample[];
  /** True when the capture has seen no packets for >5 s. */
  stalled: boolean;
  joined_in_progress: boolean;
}

export interface TowerEntry {
  car_index: number;
  position: number;
  name: string;
  team_id: number;
  is_player: boolean;
  lap_number: number;
  last_lap_ms: number | null;
  /** Interval to the car directly ahead. */
  gap_ahead_ms: number | null;
  gap_leader_ms: number | null;
  compound_visual: number | null;
  tyre_age_laps: number | null;
  /** 0 none, 1 pitting, 2 in pit area. */
  pit_status: number;
  penalties_s: number;
  /** 0 invalid, 1 inactive, 2 active, 3 finished, 4 DNF, 5 DSQ, 6 not classified, 7 retired. */
  result_status: number;
}

export interface TyresInfo {
  surface_temp_c: Wheels;
  inner_temp_c: Wheels;
  pressure_psi: Wheels;
  wear_pct: Wheels;
  /** Rolling estimate; null (or null corners) before enough laps. */
  wear_rate_pct_per_lap: WheelsMaybe | null;
  /** Race sessions only. */
  projected_wear_end_pct: WheelsMaybe | null;
  compound_actual: number | null;
  compound_visual: number | null;
  age_laps: number | null;
}

export interface FuelInfo {
  in_tank_kg: number;
  /** The game's own estimate of how many laps the remaining fuel covers. */
  remaining_laps: number;
  /** Null in timed sessions. */
  laps_left_in_session: number | null;
  /** `remaining_laps - laps_left_in_session`; positive means fuel to spare. */
  delta_laps: number | null;
  burn_last_lap_kg: number | null;
}

export interface EnergyInfo {
  store_j: number;
  store_pct: number;
  deploy_mode: number | null;
  /** Null on the 2026 packet format. */
  harvested_lap_j: number | null;
  deployed_lap_j: number | null;
}

export interface DamageInfo {
  front_left_wing_pct: number;
  front_right_wing_pct: number;
  rear_wing_pct: number;
  floor_pct: number;
  diffuser_pct: number;
  sidepod_pct: number;
  gearbox_pct: number;
  engine_pct: number;
}

export interface PaceInfo {
  last_3_avg_ms: number | null;
  ahead_last_3_avg_ms: number | null;
  behind_last_3_avg_ms: number | null;
}

export type SectorTriple = [number | null, number | null, number | null];

export interface SectorsInfo {
  /** s1..s3 of the lap in progress; trailing nulls until each is set. */
  current_lap: SectorTriple;
  /** Reference lap's sectors. */
  best_lap: SectorTriple;
  /** Best sector times seen this session, regardless of lap. */
  session_best: SectorTriple;
  last_lap_valid: boolean;
}

export interface TimeTrialInfo {
  pb_ms: number | null;
  rival_ms: number | null;
  pb_sectors: SectorTriple;
  rival_sectors: SectorTriple;
}

export interface HealthInfo {
  packets_per_sec: number;
  parse_errors_total: number;
  kernel_drops_total: number;
  last_packet_age_ms: number;
  ws_clients: number;
}

/** 10 Hz telemetry frame. */
export interface FastFrame {
  type: 'fast';
  /** Game sessionTime, seconds. */
  t: number;
  speed_kmh: number;
  /** -1 reverse, 0 neutral. */
  gear: number;
  rpm: number;
  /** 0..1 */
  throttle: number;
  /** 0..1 */
  brake: number;
  /** -1..1 */
  steer: number;
  /** Null on the 2026 packet format, which has no DRS. */
  drs_open: boolean | null;
  /** Null on the 2025 packet format. */
  aero_mode: number | null;
  ers_deploy_mode: number | null;
  rev_lights_percent: number;
  lap_number: number;
  lap_distance_m: number;
  current_lap_ms: number;
  /** Live delta against the reference lap; null when there is no reference. */
  delta_best_ms: number | null;
  delta_kind: DeltaKind | null;
}

/** 1 Hz aggregate frame. */
export interface SlowFrame {
  type: 'slow';
  session: SessionInfo;
  tower: TowerEntry[];
  tyres: TyresInfo | null;
  fuel: FuelInfo | null;
  energy: EnergyInfo | null;
  damage: DamageInfo | null;
  pace: PaceInfo | null;
  sectors: SectorsInfo | null;
  /** Null unless `session.session_kind === 'time_trial'`. */
  timetrial: TimeTrialInfo | null;
  health: HealthInfo | null;
}

// ── events ───────────────────────────────────────────────────────────────────

export type EventCode =
  | 'SECTOR'
  | 'LAP'
  | 'LAP_INVALID'
  | 'PIT_IN'
  | 'PIT_OUT'
  | 'PENALTY'
  | 'FLAG'
  | 'SC'
  | 'FLASHBACK'
  | 'SESSION_START'
  | 'SESSION_END'
  | 'CHEQUERED'
  | 'FASTEST_LAP'
  | 'DRS'
  | 'STALLED';

export const EVENT_CODES: readonly EventCode[] = [
  'SECTOR',
  'LAP',
  'LAP_INVALID',
  'PIT_IN',
  'PIT_OUT',
  'PENALTY',
  'FLAG',
  'SC',
  'FLASHBACK',
  'SESSION_START',
  'SESSION_END',
  'CHEQUERED',
  'FASTEST_LAP',
  'DRS',
  'STALLED'
] as const;

const EVENT_CODE_SET: ReadonlySet<string> = new Set<string>(EVENT_CODES);

export interface SectorEventData {
  sector: number;
  time_ms: number;
  color: PaceColor;
}
export interface LapEventData {
  lap_number: number;
  time_ms: number;
  valid: boolean;
  color: PaceColor;
  sectors: SectorTriple;
}
export interface LapInvalidEventData {
  lap_number: number;
}
export interface PitEventData {
  lap_number: number;
}
export interface PenaltyEventData {
  penalty_type: number;
  infringement_type: number;
  time_s: number;
  other_car: number | null;
}
export interface FlagEventData {
  flag: 'yellow' | 'red' | 'blue' | 'green' | 'clear';
}
export interface SafetyCarEventData {
  status: 'deployed' | 'virtual' | 'ending' | 'in';
}
export interface FlashbackEventData {
  to_session_time_s: number;
}
export interface FastestLapEventData {
  car_index: number;
  name: string;
  time_ms: number;
}
export interface DrsEventData {
  enabled: boolean;
}
export interface StalledEventData {
  stalled: boolean;
}

interface EventBase {
  type: 'event';
  /** Game sessionTime, seconds. */
  t: number;
}

/**
 * Discriminated on `code`, so a `switch` over an event narrows `data` without a
 * cast — which is what keeps the toast renderer honest.
 */
export type RaceEvent =
  | (EventBase & { code: 'SECTOR'; data: SectorEventData })
  | (EventBase & { code: 'LAP'; data: LapEventData })
  | (EventBase & { code: 'LAP_INVALID'; data: LapInvalidEventData })
  | (EventBase & { code: 'PIT_IN'; data: PitEventData })
  | (EventBase & { code: 'PIT_OUT'; data: PitEventData })
  | (EventBase & { code: 'PENALTY'; data: PenaltyEventData })
  | (EventBase & { code: 'FLAG'; data: FlagEventData })
  | (EventBase & { code: 'SC'; data: SafetyCarEventData })
  | (EventBase & { code: 'FLASHBACK'; data: FlashbackEventData })
  | (EventBase & { code: 'SESSION_START'; data: Record<string, never> })
  | (EventBase & { code: 'SESSION_END'; data: Record<string, never> })
  | (EventBase & { code: 'CHEQUERED'; data: Record<string, never> })
  | (EventBase & { code: 'FASTEST_LAP'; data: FastestLapEventData })
  | (EventBase & { code: 'DRS'; data: DrsEventData })
  | (EventBase & { code: 'STALLED'; data: StalledEventData });

export interface SnapshotMessage {
  type: 'snapshot';
  protocol_version: number;
  session: SessionInfo | null;
  fast: FastFrame | null;
  slow: SlowFrame | null;
  /** Last 20 events, oldest first. */
  recent_events: RaceEvent[];
}

export type ServerMessage = SnapshotMessage | FastFrame | SlowFrame | RaceEvent;

// ── guards ───────────────────────────────────────────────────────────────────

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

export function isSessionKind(v: unknown): v is SessionKind {
  return v === 'practice' || v === 'quali' || v === 'race' || v === 'time_trial' || v === 'other';
}

export function isEventCode(v: unknown): v is EventCode {
  return typeof v === 'string' && EVENT_CODE_SET.has(v);
}

export function isSnapshot(v: unknown): v is SnapshotMessage {
  return isRecord(v) && v['type'] === 'snapshot' && typeof v['protocol_version'] === 'number';
}

export function isFastFrame(v: unknown): v is FastFrame {
  return isRecord(v) && v['type'] === 'fast' && typeof v['t'] === 'number';
}

export function isSlowFrame(v: unknown): v is SlowFrame {
  if (!isRecord(v) || v['type'] !== 'slow') return false;
  const session = v['session'];
  // The layout switch keys off session_kind, so a slow frame without a usable
  // one is not a slow frame as far as this client is concerned.
  return isRecord(session) && isSessionKind(session['session_kind']);
}

export function isRaceEvent(v: unknown): v is RaceEvent {
  return isRecord(v) && v['type'] === 'event' && isEventCode(v['code']);
}

export function isServerMessage(v: unknown): v is ServerMessage {
  return isSnapshot(v) || isFastFrame(v) || isSlowFrame(v) || isRaceEvent(v);
}

/**
 * Parse a raw text frame. Returns `null` for malformed JSON or any payload that
 * does not match the contract, so callers can drop it without a try/catch.
 */
export function parseServerMessage(raw: string): ServerMessage | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  return isServerMessage(value) ? value : null;
}

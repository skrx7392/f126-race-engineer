/**
 * The single source of truth for the dashboard, and the WebSocket client that
 * fills it.
 *
 * The important structural decision: the socket and the mock generator both
 * funnel through `handleMessage()`. Mock mode is not a parallel implementation
 * of the UI's data path — it is the same path with a different producer, so a
 * layout that works in `?mock=1` works against the real backend.
 *
 * docs/ws-protocol.md: "a fresh `snapshot` arrives on reconnect, so clients hold
 * no cross-connection state." `reset()` enforces that rather than trusting it.
 */

import {
  parseServerMessage,
  type FastFrame,
  type RaceEvent,
  type ServerMessage,
  type SessionInfo,
  type SlowFrame
} from './protocol';
import { backoffDelay } from './backoff';

/** Ring capacity for the event log. */
export const EVENT_RING_SIZE = 50;

/**
 * Connection surface shown by the header dot.
 *
 * `stalled` is not a socket state — the socket is fine and frames may still be
 * arriving. It means the *capture* has gone quiet (the backend's `>5 s without
 * packets` rule, reported via `session.stalled` and the `STALLED` event), which
 * from the driver's seat is the same problem: the numbers are lying.
 */
export type ConnectionState = 'connecting' | 'open' | 'stalled' | 'closed';

/** Health dot: green / amber / red. */
export type HealthLevel = 'good' | 'warn' | 'bad';

/** An event plus the client-side metadata toasts need to key and expire on. */
export type StoredEvent = RaceEvent & {
  /** Monotonic per-session sequence number; stable key for keyed each blocks. */
  seq: number;
  /** `Date.now()` at arrival, for toast dismissal. */
  at: number;
};

/** Frames older than this while connected mean the feed has gone quiet. */
const CLIENT_STALL_AFTER_MS = 6_000;
/** `last_packet_age_ms` above this degrades the dot to amber. */
const PACKET_AGE_WARN_MS = 500;
/** Server's max-clients rejection (docs/ws-protocol.md). */
const CLOSE_MAX_CLIENTS = 1013;

export function defaultWsUrl(): string {
  const override = import.meta.env['VITE_WS_URL'];
  if (typeof override === 'string' && override.length > 0) return override;
  if (typeof location === 'undefined') return 'ws://localhost:8000/ws';
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

export class RaceStore {
  // ── protocol surface ───────────────────────────────────────────────────────
  session = $state<SessionInfo | null>(null);
  fast = $state<FastFrame | null>(null);
  slow = $state<SlowFrame | null>(null);
  events = $state<StoredEvent[]>([]);
  protocolVersion = $state<number | null>(null);

  // ── connection surface ─────────────────────────────────────────────────────
  /** Raw socket lifecycle, before the stall overlay. */
  socketState = $state<'connecting' | 'open' | 'closed'>('closed');
  /** Last close code, so the UI can explain a 1013 rejection. */
  lastCloseCode = $state<number | null>(null);
  /** Set by `STALLED` events; cleared by `STALLED {stalled:false}`. */
  stalledEvent = $state(false);
  /** Set by the client-side watchdog when frames stop arriving. */
  clientStalled = $state(false);
  /** Wall-clock time of the last frame of any kind. */
  lastMessageAt = $state(0);

  #errorsRising = $state(false);
  #prevParseErrors = 0;
  #prevKernelDrops = 0;
  #seq = 0;

  /** True when either the server or the watchdog says the capture is quiet. */
  stalled = $derived(this.session?.stalled === true || this.stalledEvent || this.clientStalled);

  connection: ConnectionState = $derived(
    this.socketState === 'open' ? (this.stalled ? 'stalled' : 'open') : this.socketState
  );

  /**
   * Health dot. The protocol document defines the inputs (`stalled`,
   * `last_packet_age_ms`, `parse_errors_total`, `kernel_drops_total`) but not
   * the thresholds, so these are the client's:
   *
   *   red    — no socket, or the capture is stalled: the numbers are stale.
   *   amber  — connecting, or frames are late (>500 ms, i.e. 5 fast frames
   *            missed), or parse/drop counters moved since the last slow frame.
   *   green  — socket open, frames current, counters flat.
   */
  health: HealthLevel = $derived.by(() => {
    if (this.socketState === 'closed') return 'bad';
    if (this.socketState === 'connecting') return 'warn';
    if (this.stalled) return 'bad';
    const h = this.slow?.health;
    if (h) {
      if (h.last_packet_age_ms > PACKET_AGE_WARN_MS) return 'warn';
      if (h.packets_per_sec <= 0) return 'warn';
    }
    if (this.#errorsRising) return 'warn';
    return 'good';
  });

  /** Human-readable reason for the current connection state, or null. */
  statusNote = $derived.by(() => {
    if (this.socketState === 'closed' && this.lastCloseCode === CLOSE_MAX_CLIENTS) {
      return 'Server full — retrying';
    }
    if (this.socketState === 'closed') return 'Reconnecting';
    if (this.socketState === 'connecting') return 'Connecting';
    if (this.stalled) return 'No packets — is the game sending telemetry?';
    return null;
  });

  /** Convenience: the player's row in the timing tower. */
  playerRow = $derived(this.slow?.tower.find((r) => r.is_player) ?? null);

  // ── ingestion ──────────────────────────────────────────────────────────────

  /**
   * Apply one server message. The only way data enters the store.
   */
  handleMessage(msg: ServerMessage): void {
    this.lastMessageAt = Date.now();
    this.clientStalled = false;

    switch (msg.type) {
      case 'snapshot': {
        this.protocolVersion = msg.protocol_version;
        this.session = msg.session;
        this.fast = msg.fast;
        this.slow = msg.slow;
        if (msg.slow?.session) this.session = msg.slow.session;
        this.#resetErrorTracking(msg.slow);
        // Snapshot replaces the ring rather than appending: a reconnect must not
        // replay events the user already saw as toasts.
        this.events = msg.recent_events
          .slice(-EVENT_RING_SIZE)
          .map((e) => this.#store(e, /* silent */ true));
        this.#applyStalledFromEvents(msg.recent_events);
        break;
      }
      case 'fast':
        this.fast = msg;
        break;
      case 'slow':
        this.slow = msg;
        this.session = msg.session;
        this.#trackErrors(msg);
        break;
      case 'event':
        this.#push(msg);
        break;
    }
  }

  /** Parse and apply a raw text frame; ignores anything malformed. */
  handleRaw(raw: string): void {
    const msg = parseServerMessage(raw);
    if (msg) this.handleMessage(msg);
  }

  #store(e: RaceEvent, silent: boolean): StoredEvent {
    // Snapshot backfill is timestamped in the past so the toast layer's
    // freshness window drops it instead of flashing 20 stale toasts on connect.
    return { ...e, seq: ++this.#seq, at: silent ? 0 : Date.now() };
  }

  #push(e: RaceEvent): void {
    if (e.code === 'STALLED') this.stalledEvent = e.data.stalled === true;
    const next = this.events.length >= EVENT_RING_SIZE ? this.events.slice(1) : this.events.slice();
    next.push(this.#store(e, false));
    this.events = next;
  }

  #applyStalledFromEvents(list: readonly RaceEvent[]): void {
    for (let i = list.length - 1; i >= 0; i--) {
      const e = list[i];
      if (e && e.code === 'STALLED') {
        this.stalledEvent = e.data.stalled === true;
        return;
      }
    }
  }

  #resetErrorTracking(slow: SlowFrame | null): void {
    this.#prevParseErrors = slow?.health?.parse_errors_total ?? 0;
    this.#prevKernelDrops = slow?.health?.kernel_drops_total ?? 0;
    this.#errorsRising = false;
  }

  #trackErrors(msg: SlowFrame): void {
    const h = msg.health;
    if (!h) return;
    this.#errorsRising =
      h.parse_errors_total > this.#prevParseErrors || h.kernel_drops_total > this.#prevKernelDrops;
    this.#prevParseErrors = h.parse_errors_total;
    this.#prevKernelDrops = h.kernel_drops_total;
  }

  /** Drop all session data. Called before each (re)connect. */
  reset(): void {
    this.session = null;
    this.fast = null;
    this.slow = null;
    this.events = [];
    this.protocolVersion = null;
    this.stalledEvent = false;
    this.clientStalled = false;
    this.#resetErrorTracking(null);
  }
}

/**
 * Owns the socket lifecycle and the reconnect schedule. Split from the store so
 * the store stays trivially testable — tests feed it messages directly, exactly
 * as mock mode does.
 */
export class WsClient {
  #store: RaceStore;
  #url: string;
  #socket: WebSocket | null = null;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #watchdog: ReturnType<typeof setInterval> | null = null;
  #attempt = 0;
  #closed = false;

  constructor(store: RaceStore, url: string = defaultWsUrl()) {
    this.#store = store;
    this.#url = url;
  }

  start(): void {
    this.#closed = false;
    this.#open();
    this.#watchdog ??= setInterval(() => this.#checkStall(), 1_000);
  }

  stop(): void {
    this.#closed = true;
    if (this.#retryTimer !== null) clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
    if (this.#watchdog !== null) clearInterval(this.#watchdog);
    this.#watchdog = null;
    const s = this.#socket;
    this.#socket = null;
    if (s) {
      s.onopen = s.onclose = s.onerror = s.onmessage = null;
      s.close();
    }
    this.#store.socketState = 'closed';
  }

  #open(): void {
    if (this.#closed) return;
    this.#store.reset();
    this.#store.socketState = 'connecting';

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.#url);
    } catch {
      this.#scheduleRetry();
      return;
    }
    this.#socket = socket;

    socket.onopen = () => {
      if (this.#socket !== socket) return;
      this.#store.socketState = 'open';
      this.#store.lastCloseCode = null;
      this.#store.lastMessageAt = Date.now();
    };

    socket.onmessage = (ev: MessageEvent) => {
      if (this.#socket !== socket) return;
      if (typeof ev.data !== 'string') return;
      // Reset the schedule on real data, not merely on `open`: a server at its
      // client cap accepts the socket and then closes it with 1013, and
      // resetting on `open` would turn that into a 1 s retry loop.
      this.#attempt = 0;
      this.#store.handleRaw(ev.data);
    };

    socket.onerror = () => {
      /* `close` always follows; retry is scheduled there. */
    };

    socket.onclose = (ev: CloseEvent) => {
      if (this.#socket !== socket) return;
      this.#socket = null;
      this.#store.socketState = 'closed';
      this.#store.lastCloseCode = ev.code;
      this.#scheduleRetry();
    };
  }

  #scheduleRetry(): void {
    if (this.#closed || this.#retryTimer !== null) return;
    const delay = backoffDelay(this.#attempt++);
    this.#retryTimer = setTimeout(() => {
      this.#retryTimer = null;
      this.#open();
    }, delay);
  }

  #checkStall(): void {
    if (this.#store.socketState !== 'open') return;
    const since = Date.now() - this.#store.lastMessageAt;
    this.#store.clientStalled = since > CLIENT_STALL_AFTER_MS;
  }
}

/** The app-wide store. Mock mode and the socket client both write to this. */
export const store = new RaceStore();

import { describe, it, expect } from 'vitest';
import {
  isSnapshot,
  isFastFrame,
  isSlowFrame,
  isRaceEvent,
  isServerMessage,
  isSessionKind,
  isEventCode,
  parseServerMessage,
  EVENT_CODES
} from '../lib/protocol';

/** A minimally valid slow frame — only the fields the guards actually inspect. */
const slowFrame = {
  type: 'slow',
  session: { session_kind: 'race', track_name: 'Suzuka' },
  tower: []
};

describe('isSessionKind', () => {
  it('accepts every kind the protocol defines', () => {
    for (const k of ['practice', 'quali', 'race', 'time_trial', 'other']) {
      expect(isSessionKind(k)).toBe(true);
    }
  });

  it('rejects near-misses and non-strings', () => {
    expect(isSessionKind('qualifying')).toBe(false);
    expect(isSessionKind('RACE')).toBe(false);
    expect(isSessionKind(null)).toBe(false);
    expect(isSessionKind(3)).toBe(false);
  });
});

describe('isEventCode', () => {
  it('accepts all 15 documented codes', () => {
    expect(EVENT_CODES).toHaveLength(15);
    for (const c of EVENT_CODES) expect(isEventCode(c)).toBe(true);
  });

  it('rejects unknown codes', () => {
    expect(isEventCode('OVERTAKE')).toBe(false);
    expect(isEventCode('sector')).toBe(false);
    expect(isEventCode(undefined)).toBe(false);
  });
});

describe('message guards', () => {
  it('identifies a snapshot', () => {
    const msg = { type: 'snapshot', protocol_version: 1, recent_events: [] };
    expect(isSnapshot(msg)).toBe(true);
    expect(isFastFrame(msg)).toBe(false);
    expect(isSlowFrame(msg)).toBe(false);
    expect(isRaceEvent(msg)).toBe(false);
  });

  it('requires a numeric protocol_version on a snapshot', () => {
    expect(isSnapshot({ type: 'snapshot', protocol_version: '1' })).toBe(false);
    expect(isSnapshot({ type: 'snapshot' })).toBe(false);
  });

  it('identifies a fast frame by its session time', () => {
    expect(isFastFrame({ type: 'fast', t: 123.456, speed_kmh: 287 })).toBe(true);
    expect(isFastFrame({ type: 'fast' })).toBe(false);
  });

  it('identifies a slow frame only when session_kind is usable', () => {
    expect(isSlowFrame(slowFrame)).toBe(true);
    // The layout switch keys off session_kind, so an unusable one is fatal.
    expect(isSlowFrame({ type: 'slow', session: { session_kind: 'bogus' } })).toBe(false);
    expect(isSlowFrame({ type: 'slow', session: null })).toBe(false);
    expect(isSlowFrame({ type: 'slow' })).toBe(false);
  });

  it('identifies an event by its code', () => {
    expect(isRaceEvent({ type: 'event', t: 1, code: 'SECTOR', data: {} })).toBe(true);
    expect(isRaceEvent({ type: 'event', t: 1, code: 'NOPE', data: {} })).toBe(false);
  });

  it('rejects arrays and primitives everywhere', () => {
    for (const v of [null, undefined, 42, 'slow', [], [{ type: 'slow' }]]) {
      expect(isServerMessage(v)).toBe(false);
    }
  });
});

describe('parseServerMessage', () => {
  it('parses each documented message type', () => {
    expect(parseServerMessage(JSON.stringify(slowFrame))?.type).toBe('slow');
    expect(parseServerMessage('{"type":"fast","t":1}')?.type).toBe('fast');
    expect(parseServerMessage('{"type":"snapshot","protocol_version":1}')?.type).toBe('snapshot');
    expect(parseServerMessage('{"type":"event","t":1,"code":"LAP","data":{}}')?.type).toBe('event');
  });

  it('returns null rather than throwing on malformed JSON', () => {
    expect(parseServerMessage('{')).toBeNull();
    expect(parseServerMessage('')).toBeNull();
    expect(parseServerMessage('not json at all')).toBeNull();
  });

  it('returns null for well-formed JSON that is not a server message', () => {
    expect(parseServerMessage('{"type":"hello"}')).toBeNull();
    expect(parseServerMessage('[1,2,3]')).toBeNull();
    expect(parseServerMessage('null')).toBeNull();
  });

  it('narrows a discriminated event so payload fields are reachable', () => {
    const msg = parseServerMessage(
      '{"type":"event","t":9,"code":"SECTOR","data":{"sector":2,"time_ms":35400,"color":"purple"}}'
    );
    expect(msg).not.toBeNull();
    if (msg && msg.type === 'event' && msg.code === 'SECTOR') {
      expect(msg.data.sector).toBe(2);
      expect(msg.data.color).toBe('purple');
    } else {
      throw new Error('expected a SECTOR event');
    }
  });
});

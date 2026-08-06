/**
 * Reconnect backoff, per the client-behaviour rules in docs/ws-protocol.md:
 * "exponential backoff (1 s -> 30 s cap, jitter)".
 *
 * Jitter is proportional (+/-25% around the nominal step) rather than the more
 * common "full jitter" (uniform over `[0, base]`). Full jitter can fire a retry
 * a few milliseconds after a drop, and the server caps clients at 20 with a
 * 1013 close on the overflow connection — so a herd of near-instant retries is
 * exactly the behaviour that keeps a client locked out. Staying near the
 * nominal step preserves the documented 1 s floor while still de-phasing
 * several tabs that dropped together.
 */

export const BASE_DELAY_MS = 1_000;
export const MAX_DELAY_MS = 30_000;
/** Jitter spread: the delay lands within +/-25% of the nominal step. */
export const JITTER_RATIO = 0.25;

/** Nominal (un-jittered) delay for a zero-based attempt index. */
export function nominalDelay(attempt: number): number {
  if (!Number.isFinite(attempt) || attempt < 0) return BASE_DELAY_MS;
  // 2**attempt overflows to Infinity well before this matters, and Math.min
  // handles Infinity correctly, so no extra clamp on the exponent is needed.
  return Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** Math.floor(attempt));
}

/**
 * Jittered delay for a zero-based attempt index.
 *
 * @param rand source of uniform randomness in `[0, 1)`; injectable for tests.
 */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const base = nominalDelay(attempt);
  const spread = 1 - JITTER_RATIO + 2 * JITTER_RATIO * rand();
  return Math.round(Math.min(MAX_DELAY_MS, base * spread));
}

/** The first `count` nominal delays — the schedule the docs describe. */
export function backoffSchedule(count: number): number[] {
  return Array.from({ length: Math.max(0, count) }, (_, i) => nominalDelay(i));
}

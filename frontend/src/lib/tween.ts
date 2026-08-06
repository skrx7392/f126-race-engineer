/**
 * Frame-rate-independent smoothing for the animated readouts.
 *
 * docs/ws-protocol.md: "Animated elements (delta bar, input bars, rev strip)
 * tween toward new `fast` values over ~100 ms via requestAnimationFrame;
 * numeric readouts render values as-is on arrival."
 *
 * Two deliberate choices:
 *
 * 1. Exponential smoothing, not a fixed-duration easing. `fast` frames land at
 *    10 Hz and a new target can arrive mid-tween; a duration-based tween would
 *    have to restart and would visibly stutter. Exponential decay just retargets.
 *
 * 2. The tween writes CSS custom properties straight to the DOM instead of
 *    going through Svelte state. Driving reactive state at 60 fps would
 *    re-run every effect that reads it; a style write touches one node.
 */

/** Half-life giving ~95% convergence in ~130 ms — the "~100 ms" the docs ask for. */
export const DEFAULT_HALF_LIFE_MS = 30;

/**
 * One smoothing step toward `target`.
 *
 * Uses half-life decay, so the result depends only on elapsed time and not on
 * how that time was sliced: one 32 ms step equals two 16 ms steps. It also
 * never overshoots, which matters for bars clamped to `[0, 1]`.
 */
export function smooth(
  current: number,
  target: number,
  dtMs: number,
  halfLifeMs: number = DEFAULT_HALF_LIFE_MS
): number {
  if (!Number.isFinite(current)) return target;
  if (!Number.isFinite(target)) return current;
  if (dtMs <= 0) return current;
  if (halfLifeMs <= 0) return target;
  const decay = 2 ** (-dtMs / halfLifeMs);
  return target + (current - target) * decay;
}

/** Below this the tween snaps, so entries can go idle instead of spinning rAF. */
const EPSILON = 1e-4;

interface Entry {
  value: number;
  target: number;
  halfLife: number;
  write: (v: number) => void;
  settled: boolean;
}

const entries = new Set<Entry>();
let frame = 0;
let lastTs = 0;

function reducedMotion(): boolean {
  return (
    typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

function tick(ts: number): void {
  // First frame after an idle period has no meaningful delta; assume 60 fps.
  const dt = lastTs === 0 ? 16.7 : Math.min(100, ts - lastTs);
  lastTs = ts;

  let active = false;
  for (const e of entries) {
    if (e.settled) continue;
    e.value = smooth(e.value, e.target, dt, e.halfLife);
    if (Math.abs(e.value - e.target) < EPSILON) {
      e.value = e.target;
      e.settled = true;
    } else {
      active = true;
    }
    e.write(e.value);
  }

  if (active) {
    frame = requestAnimationFrame(tick);
  } else {
    frame = 0;
    lastTs = 0;
  }
}

function wake(): void {
  if (frame === 0 && entries.size > 0) {
    lastTs = 0;
    frame = requestAnimationFrame(tick);
  }
}

export interface TweenHandle {
  set(target: number): void;
  dispose(): void;
}

/**
 * Register a value to be smoothed. `write` is called on each frame the value
 * moves, and once more when it settles.
 */
export function tween(
  write: (v: number) => void,
  initial = 0,
  halfLifeMs: number = DEFAULT_HALF_LIFE_MS
): TweenHandle {
  const entry: Entry = {
    value: initial,
    target: initial,
    halfLife: halfLifeMs,
    write,
    settled: true
  };
  entries.add(entry);
  write(initial);

  return {
    set(target: number): void {
      if (!Number.isFinite(target) || target === entry.target) return;
      entry.target = target;
      if (reducedMotion()) {
        entry.value = target;
        entry.settled = true;
        write(target);
        return;
      }
      entry.settled = false;
      wake();
    },
    dispose(): void {
      entries.delete(entry);
    }
  };
}

export interface TweenVarParams {
  /** CSS custom property to write, e.g. `--fill`. */
  name: string;
  value: number;
  halfLifeMs?: number;
  /** Decimal places written to the property. */
  precision?: number;
}

/**
 * Svelte action: smooth a number into a CSS custom property on the node.
 *
 * ```svelte
 * <div use:tweenVar={{ name: '--fill', value: throttle }}></div>
 * ```
 */
export function tweenVar(node: HTMLElement, params: TweenVarParams) {
  const precision = params.precision ?? 4;
  const handle = tween(
    (v) => node.style.setProperty(params.name, v.toFixed(precision)),
    params.value,
    params.halfLifeMs
  );
  return {
    update(next: TweenVarParams): void {
      handle.set(next.value);
    },
    destroy(): void {
      handle.dispose();
    }
  };
}

/** Test seam: drop every registered tween and stop the loop. */
export function resetTweens(): void {
  entries.clear();
  if (frame !== 0) cancelAnimationFrame(frame);
  frame = 0;
  lastTs = 0;
}

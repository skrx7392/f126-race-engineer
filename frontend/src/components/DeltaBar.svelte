<script lang="ts">
  /**
   * Live delta against the reference lap — the one number worth a mid-corner
   * glance, so it gets the most deliberate treatment in the interface.
   *
   * This is a diverging encoding, and it obeys the rules that come with one: two
   * hues, a neutral centre, and a hard datum line at zero. Green and red are a
   * weak pair for red-green colourblindness, so colour is never doing the work
   * alone — which side of the datum the bar grows on, and the explicit sign on
   * the numeral, both carry the same information independently.
   *
   * Direction follows the number line: negative (faster) grows left, positive
   * (slower) grows right. Ticks are labelled so the scale is never a guess.
   *
   * The bar tweens; the numeral does not. docs/ws-protocol.md is explicit —
   * animated elements ease toward new values, "numeric readouts render values
   * as-is on arrival" — and a smoothed numeral would also be a lying one.
   */
  import { tweenVar } from '../lib/tween';
  import { formatDelta, deltaKindLabel } from '../lib/format';
  import type { DeltaKind } from '../lib/protocol';

  interface Props {
    deltaMs: number | null;
    deltaKind: DeltaKind | null;
    /** Half-range in seconds: the bar spans ±scale. */
    scale?: number;
    /** `dominant` is the full-width qualifying treatment. */
    size?: 'normal' | 'dominant';
    label?: string;
    /**
     * Right-hand caption naming the reference. Defaults to the protocol's
     * `delta_kind`; pass an explicit one where the caller knows better (time
     * trial names the actual reference lap times).
     */
    refLabel?: string;
    /** Grid area in the parent layout; time trial places two of these. */
    area?: string;
  }

  let {
    deltaMs,
    deltaKind,
    scale = 1,
    size = 'normal',
    label,
    refLabel,
    area = 'delta'
  }: Props = $props();

  /*
   * Without this the time-trial bars printed their own title twice ("vs
   * personal best" on both sides), and the rival bar claimed "no reference"
   * when the rival is precisely the reference.
   */
  let refText = $derived(refLabel ?? (label === undefined ? deltaKindLabel(deltaKind) : null));

  let seconds = $derived(deltaMs == null ? null : deltaMs / 1000);

  /** Normalised to [-1, 1] for the fill geometry. */
  let normalized = $derived.by(() => {
    if (seconds == null) return 0;
    const n = seconds / scale;
    return n < -1 ? -1 : n > 1 ? 1 : n;
  });

  let tone = $derived.by(() => {
    if (seconds == null) return 'none';
    if (seconds < -0.02) return 'gain';
    if (seconds > 0.02) return 'loss';
    return 'level';
  });

  /*
   * A single ghost mark showing where the delta stood a few seconds ago. It is
   * the disciplined version of a sparkline: one extra mark, no extra chrome,
   * and it answers the question the bar alone cannot — am I trending toward
   * this number or away from it?
   */
  const TRAIL_MS = 3_000;

  /*
   * The sample buffer is deliberately a plain array rather than `$state`. It is
   * internal bookkeeping that nothing renders, and making it reactive would put
   * the effect below in a read-write cycle with itself. Only `ghost` — the one
   * value the template draws — is reactive.
   */
  let trail: Array<{ t: number; v: number }> = [];
  let ghost = $state(0);

  $effect(() => {
    const v = normalized;
    const now = Date.now();
    trail = [...trail, { t: now, v }].filter((p) => now - p.t <= TRAIL_MS);
    ghost = trail[0]?.v ?? v;
  });

  /** Tick positions in seconds, excluding the centre. */
  let ticks = $derived.by(() => {
    const step = scale >= 2 ? 1 : 0.5;
    const out: Array<{ at: number; value: number; major: boolean }> = [];
    for (let v = -scale; v <= scale + 1e-9; v += step) {
      if (Math.abs(v) < 1e-9) continue;
      out.push({ at: 50 + (v / scale) * 50, value: v, major: Math.abs(Math.abs(v) - scale) < 1e-9 });
    }
    return out;
  });
</script>

<section
  class="delta {size} {tone}"
  style="grid-area: {area}"
  use:tweenVar={{ name: '--d', value: normalized }}
>
  <div class="head">
    <span class="label">{label ?? 'Delta'}</span>
    {#if refText}<span class="label ref">{refText}</span>{/if}
  </div>

  <div class="track">
    {#each ticks as tick (tick.value)}
      <span class="tick" class:major={tick.major} style="left: {tick.at}%"></span>
    {/each}

    <span class="fill neg"></span>
    <span class="fill pos"></span>

    <span class="ghost" style="--g: {ghost}"></span>
    <span class="datum"></span>

    <span class="readout clock">{formatDelta(deltaMs)}</span>
  </div>

  <div class="scale-row">
    <span class="label">−{scale.toFixed(scale >= 2 ? 0 : 1)}s</span>
    <span class="label mid">faster ◂ ▸ slower</span>
    <span class="label">+{scale.toFixed(scale >= 2 ? 0 : 1)}s</span>
  </div>
</section>

<style>
  .delta {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    background: var(--surface-1);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 0.45rem 0.6rem 0.4rem;
    min-width: 0;
    min-height: 0;
  }

  .head {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .ref {
    color: var(--ink-3);
  }

  /*
   * `flex: 1` would zero the flex-basis and collapse this to nothing inside an
   * auto-sized grid row — which clipped the dominant readout. Growing from an
   * auto basis with an explicit floor keeps the bar at its intended height and
   * still lets it take up slack when the row has some.
   */
  .track {
    position: relative;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    overflow: hidden;
    flex: 1 1 auto;
    min-height: 3.5rem;
  }

  .dominant .track {
    min-height: 6.6rem;
  }

  .fill {
    position: absolute;
    top: 0;
    bottom: 0;
  }

  /* Negative delta grows leftward from the centre datum. */
  .fill.neg {
    right: 50%;
    width: calc(max(0, -1 * var(--d, 0)) * 50%);
    background: linear-gradient(
      to left,
      var(--green),
      color-mix(in srgb, var(--green) 55%, transparent)
    );
  }

  .fill.pos {
    left: 50%;
    width: calc(max(0, var(--d, 0)) * 50%);
    background: linear-gradient(
      to right,
      var(--red),
      color-mix(in srgb, var(--red) 55%, transparent)
    );
  }

  .tick {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--line-strong);
    opacity: 0.55;
  }

  .tick.major {
    opacity: 0.9;
  }

  /* Where the delta stood ~3 s ago. */
  .ghost {
    position: absolute;
    top: 12%;
    bottom: 12%;
    width: 2px;
    margin-left: -1px;
    left: calc(50% + var(--g, 0) * 50%);
    background: var(--ink-2);
    opacity: 0.5;
    border-radius: 1px;
  }

  .datum {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 50%;
    width: 2px;
    margin-left: -1px;
    background: var(--ink);
    opacity: 0.9;
  }

  /*
   * The numeral sits at the centre of the track, which is precisely where the
   * fill grows from — so it is always partly over its own colour. Colouring the
   * text to match put red on red and cost the most important number on screen
   * its contrast.
   *
   * The text is therefore near-white in every state, and the tone is carried by
   * a coloured glow behind it plus the fill it sits on. Legibility at distance
   * outranks a redundant encoding: the sign, the side, and the fill already say
   * which way this is going.
   */
  .readout {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 2.6rem;
    font-weight: 700;
    color: var(--ink);
    text-shadow:
      0 1px 4px rgba(0, 0, 0, 0.95),
      0 0 16px var(--glow, transparent);
    pointer-events: none;
  }

  .dominant .readout {
    font-size: 5rem;
    font-weight: 700;
  }

  .gain {
    --glow: color-mix(in srgb, var(--green) 70%, transparent);
  }
  .loss {
    --glow: color-mix(in srgb, var(--red) 70%, transparent);
  }

  .none .readout {
    color: var(--ink-3);
    font-size: 1.4rem;
  }

  .scale-row {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .mid {
    letter-spacing: 0.1em;
    color: var(--ink-3);
  }
</style>

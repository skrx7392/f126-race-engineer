<script lang="ts">
  /**
   * Four corners, laid out as the car sits on track — fronts on top.
   *
   * The protocol delivers wheel arrays as `[rl, rr, fl, fr]`; WHEEL_LAYOUT
   * reorders them so the grid matches the car rather than the wire format. A
   * driver reading "front-left is cooking" should not have to translate an
   * array index.
   *
   * Temperature is a diverging encoding around the 85–105 °C working window:
   * blue below, green inside, red above. Blue/green/red is a weak triple for
   * colourblind readers, so every corner also carries the number itself and an
   * explicit arrow for out-of-window state — the colour is the fast channel,
   * not the only one.
   */
  import type { TyresInfo } from '../lib/protocol';
  import {
    WHEEL_ORDER,
    WHEEL_LAYOUT,
    thermalState,
    compoundOf,
    actualCompoundName,
    TYRE_WINDOW_MIN,
    TYRE_WINDOW_MAX
  } from '../lib/enums';
  import { formatNumber, DASH, normalize } from '../lib/format';

  interface Props {
    tyres: TyresInfo | null;
    /** Qualifying wants temperature first; the race wants wear first. */
    emphasis?: 'wear' | 'temp';
  }

  let { tyres, emphasis = 'wear' }: Props = $props();

  let compound = $derived(compoundOf(tyres?.compound_visual));
  let actual = $derived(actualCompoundName(tyres?.compound_actual));

  interface Corner {
    key: string;
    temp: number | null;
    inner: number | null;
    pressure: number | null;
    wear: number | null;
    projected: number | null;
    state: 'cold' | 'working' | 'hot';
    /** Position of the temperature within the window, for the corner's meter. */
    windowPos: number;
  }

  let corners: Corner[] = $derived(
    WHEEL_LAYOUT.map((idx) => {
      const temp = tyres?.surface_temp_c?.[idx] ?? null;
      return {
        key: WHEEL_ORDER[idx] ?? String(idx),
        temp,
        inner: tyres?.inner_temp_c?.[idx] ?? null,
        pressure: tyres?.pressure_psi?.[idx] ?? null,
        wear: tyres?.wear_pct?.[idx] ?? null,
        projected: tyres?.projected_wear_end_pct?.[idx] ?? null,
        state: thermalState(temp),
        // Meter spans a little either side of the window so out-of-range still moves.
        windowPos: temp === null ? 0.5 : normalize(temp, TYRE_WINDOW_MIN - 25, TYRE_WINDOW_MAX + 25)
      };
    })
  );

  const ARROW = { cold: '↓', working: '●', hot: '↑' } as const;
</script>

<section class="tyres panel" data-panel="tyres">
  <div class="panel-head">
    <span class="label">Tyres</span>
    <span class="compound-tag">
      <span class="pill" style="background: {compound.color}; color: {compound.ink}"
        >{compound.code}</span
      >
      <span class="label">{actual ?? compound.label} · {tyres?.age_laps ?? DASH} laps</span>
    </span>
  </div>

  <div class="grid">
    {#each corners as corner (corner.key)}
      <div class="corner {corner.state}" class:temp-first={emphasis === 'temp'}>
        <span class="pos label">{corner.key}</span>

        <span class="temp">
          {formatNumber(corner.temp)}<span class="deg">°</span>
          <span class="arrow">{ARROW[corner.state]}</span>
        </span>

        <div class="meter" style="--p: {corner.windowPos}">
          <span class="window"></span>
          <span class="needle"></span>
        </div>

        <span class="wear">
          {formatNumber(corner.wear, 1)}<span class="pct">%</span>
          {#if corner.projected !== null}
            <span class="proj">→{formatNumber(corner.projected, 0)}</span>
          {/if}
        </span>

        <span class="sub label">
          core {formatNumber(corner.inner)}° · {formatNumber(corner.pressure, 1)} psi
        </span>
      </div>
    {/each}
  </div>
</section>

<style>
  .tyres {
    grid-area: tyres;
  }

  .compound-tag {
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.15rem;
    height: 1.15rem;
    min-width: 14px;
    min-height: 14px;
    border-radius: 50%;
    font-size: 0.7rem;
    font-weight: 800;
  }

  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1fr;
    gap: 0.3rem;
    flex: 1;
    min-height: 0;
  }

  .corner {
    display: grid;
    grid-template-areas:
      'pos temp'
      'meter meter'
      'wear wear'
      'sub sub';
    grid-template-columns: auto 1fr;
    /*
     * Distributes rather than centres, so a corner reads as deliberate whether
     * the cell is short (race) or tall (the qualifying prep view).
     */
    align-content: space-evenly;
    gap: 0.12rem 0.3rem;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    padding: 0.25rem 0.4rem;
    min-width: 0;
  }

  .pos {
    grid-area: pos;
    align-self: center;
  }

  .temp {
    grid-area: temp;
    font-size: 1.35rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.03em;
    text-align: right;
    white-space: nowrap;
  }

  .deg {
    font-size: 0.6em;
    color: var(--ink-3);
  }

  .arrow {
    font-size: 0.62em;
    margin-inline-start: 0.15em;
  }

  .wear {
    grid-area: wear;
    font-size: 0.95rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--ink-2);
    white-space: nowrap;
  }

  .pct {
    font-size: 0.7em;
    color: var(--ink-3);
  }

  .proj {
    font-size: 0.72em;
    color: var(--ink-3);
    margin-inline-start: 0.25em;
  }

  /* Core temperature and pressure: the slow-moving pair, kept deliberately quiet. */
  .sub {
    grid-area: sub;
    font-size: 0.58rem;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    opacity: 0.75;
    margin-top: 0.1rem;
  }

  /* Where this corner sits relative to the working window. */
  .meter {
    grid-area: meter;
    position: relative;
    height: 0.28rem;
    min-height: 3px;
    background: var(--surface-3);
    border-radius: 2px;
    margin: 0.1rem 0;
  }

  .window {
    position: absolute;
    top: 0;
    bottom: 0;
    /* 85–105 °C mapped onto the meter's 60–130 °C span. */
    left: 35.7%;
    right: 35.7%;
    background: color-mix(in srgb, var(--green) 30%, transparent);
    border-radius: 1px;
  }

  .needle {
    position: absolute;
    top: -1px;
    bottom: -1px;
    width: 2px;
    margin-left: -1px;
    left: calc(var(--p, 0.5) * 100%);
    background: var(--ink);
    border-radius: 1px;
  }

  .corner.cold .temp {
    color: var(--blue);
  }
  .corner.working .temp {
    color: var(--green);
  }
  .corner.hot .temp {
    color: var(--red);
  }

  .corner.cold .needle {
    background: var(--blue);
  }
  .corner.hot .needle {
    background: var(--red);
  }

  /* Qualifying: temperature is the number that matters on an out-lap. */
  .corner.temp-first .temp {
    font-size: 1.8rem;
  }
  .corner.temp-first .wear {
    font-size: 0.82rem;
  }
</style>

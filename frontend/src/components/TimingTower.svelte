<script lang="ts">
  /**
   * The timing tower.
   *
   * Two decisions worth stating:
   *
   * 1. The player's row and its immediate neighbours sit in a lifted "focus
   *    band" at full ink; the rest of the field is dimmed. What a driver
   *    actually needs is their own neighbourhood — the cars they can lose or
   *    take a place from — while the rest of the field is context. The band
   *    changes ink weight only, never type size, so a position swap never
   *    reflows the column. A fisheye tower would read better in a screenshot
   *    and worse at 200 km/h.
   *
   * 2. Constructor colour is a 3px chip beside the name, never the name's own
   *    colour. Real team colours are brand values, not a designed categorical
   *    ramp, and several pairs are indistinguishable to a colourblind reader
   *    (Haas grey against Mercedes teal). The name is what identifies the row;
   *    the chip is a redundant accent for people who read the grid by colour.
   */
  import type { TowerEntry } from '../lib/protocol';
  import {
    compoundOf,
    isKnownCompound,
    teamColor,
    teamName,
    pitState,
    PIT_LABEL,
    isOut,
    resultLabel
  } from '../lib/enums';
  import { formatGap, formatLapTime, DASH } from '../lib/format';

  interface Props {
    tower: TowerEntry[];
    /** Qualifying and practice rank on best lap, so the column header changes. */
    intervalLabel?: string;
  }

  let { tower, intervalLabel = 'Interval' }: Props = $props();

  let playerIndex = $derived(tower.findIndex((r) => r.is_player));

  function nearPlayer(index: number): boolean {
    return playerIndex >= 0 && Math.abs(index - playerIndex) <= 1;
  }
</script>

<section class="tower panel" data-panel="tower">
  <div class="row head">
    <span class="pos label">Pos</span>
    <span class="chip-space"></span>
    <span class="name label">Driver</span>
    <span class="gap label">{intervalLabel}</span>
    <span class="last label">Last lap</span>
    <span class="tyre label">Tyre</span>
    <span class="badge-space label">Sts</span>
  </div>

  <div class="rows">
    {#each tower as row, i (row.car_index)}
      {@const compound = compoundOf(row.compound_visual)}
      {@const knownCompound = isKnownCompound(row.compound_visual)}
      {@const pit = pitState(row.pit_status)}
      {@const out = isOut(row.result_status)}
      <div
        class="row"
        class:player={row.is_player}
        class:near={!row.is_player && nearPlayer(i)}
        class:out
      >
        <span class="pos gauge">{row.position}</span>
        <span class="chip" style="background: {teamColor(row.team_id)}" title={teamName(row.team_id)}
        ></span>
        <span class="name">{row.name}</span>
        <span class="gap clock">{i === 0 ? 'Leader' : formatGap(row.gap_ahead_ms)}</span>
        <span class="last clock">{formatLapTime(row.last_lap_ms)}</span>
        <span class="tyre">
          <!--
            The compound of a car that is not ours is not broadcast, so it is
            unknown for twenty-one of the twenty-two rows. A filled chip on
            every one of them would be a column of identical placeholders
            shouting for attention; unknown therefore drops its disc entirely
            and sits in the dimmest ink, which reads as "nothing to say here".
            The title still spells it out for anyone who hovers.
          -->
          <span
            class="compound"
            class:unknown={!knownCompound}
            style={knownCompound
              ? `background: ${compound.color}; color: ${compound.ink}`
              : undefined}
            title={compound.label}>{compound.code}</span
          >
          <span class="age">{row.tyre_age_laps ?? DASH}</span>
        </span>
        <span class="badges">
          {#if out}
            <span class="badge out-badge">{resultLabel(row.result_status)}</span>
          {:else if pit !== 'none'}
            <span class="badge pit">{PIT_LABEL[pit]}</span>
          {/if}
          {#if row.penalties_s > 0}
            <span class="badge pen">+{row.penalties_s}</span>
          {/if}
        </span>
      </div>
    {/each}
  </div>
</section>

<style>
  .tower {
    grid-area: tower;
    gap: 0.25rem;
  }

  .rows {
    display: grid;
    grid-auto-rows: minmax(0, 1fr);
    gap: 1px;
    flex: 1;
    min-height: 0;
    /* Nothing scrolls: if the field overflows, it clips rather than scrolls. */
    overflow: hidden;
  }

  .row {
    display: grid;
    grid-template-columns: 1.7rem 3px minmax(0, 1fr) 4.1rem 4.6rem 2.6rem 2.4rem;
    align-items: center;
    gap: 0.4rem;
    padding: 0 0.3rem;
    border-radius: var(--radius-sm);
    min-width: 0;
  }

  .row.head {
    padding-bottom: 0.2rem;
    border-bottom: 1px solid var(--line);
    border-radius: 0;
    flex: none;
  }

  .head .label {
    font-size: 0.6rem;
  }

  .pos {
    font-size: 1rem;
    color: var(--ink-2);
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .chip {
    width: 3px;
    height: 68%;
    min-height: 10px;
    border-radius: 2px;
    align-self: center;
  }

  .chip-space {
    width: 3px;
  }

  .name {
    font-size: 1.02rem;
    font-weight: 600;
    letter-spacing: 0.015em;
    font-stretch: 90%;
    color: var(--ink-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .gap,
  .last {
    font-size: 0.95rem;
    color: var(--ink-2);
    text-align: right;
    white-space: nowrap;
  }

  .tyre {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    justify-content: flex-end;
  }

  .compound {
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
    flex: none;
  }

  /* No disc, no fill, no weight: an unknown compound is absent, not an error. */
  .compound.unknown {
    background: none;
    color: var(--ink-3);
    font-weight: 500;
    opacity: 0.7;
  }

  .age {
    font-size: 0.8rem;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }

  .badges {
    display: flex;
    gap: 0.2rem;
    justify-content: flex-end;
  }

  .badge {
    font-size: 0.62rem;
    font-weight: 750;
    letter-spacing: 0.06em;
    padding: 0.1rem 0.24rem;
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }

  .badge.pit {
    background: var(--blue-dim);
    color: var(--blue);
  }

  .badge.pen {
    background: var(--red-dim);
    color: var(--red);
  }

  .badge.out-badge {
    background: var(--surface-3);
    color: var(--ink-3);
  }

  /* Focus band: the player's neighbourhood at full ink. */
  .row.near {
    background: var(--surface-2);
  }

  .row.near .name,
  .row.near .gap,
  .row.near .last {
    color: var(--ink);
  }

  /*
   * "You are the brightest row." Identity here needs no hue — pure white ink
   * on a lifted surface with a white edge outranks every constructor colour on
   * screen without competing with the semantic palette.
   */
  .row.player {
    background: var(--surface-3);
    box-shadow: inset 3px 0 0 var(--player);
  }

  .row.player .name {
    color: var(--player);
    font-weight: 750;
  }

  .row.player .pos,
  .row.player .gap,
  .row.player .last {
    color: var(--ink);
  }

  .row.out {
    opacity: 0.42;
  }
</style>

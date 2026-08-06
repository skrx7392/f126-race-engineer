<script lang="ts">
  /**
   * Lap invalidated.
   *
   * In qualifying this is the difference between a lap worth finishing and one
   * worth abandoning, and it needs to survive being seen at the edge of vision
   * — so it is the largest type in the interface and it overlays rather than
   * displaces, keeping the delta bar readable underneath.
   */
  interface Props {
    invalid: boolean;
  }

  let { invalid }: Props = $props();
</script>

{#if invalid}
  <div class="validity" role="alert">
    <span class="word">Invalid</span>
    <span class="why label">Lap deleted — track limits</span>
  </div>
{/if}

<style>
  .validity {
    grid-area: delta;
    align-self: center;
    justify-self: center;
    z-index: 5;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.1rem;
    padding: 0.5rem 2.4rem;
    background: color-mix(in srgb, var(--red) 92%, #000);
    border-radius: var(--radius);
    box-shadow: 0 6px 30px rgba(0, 0, 0, 0.6);
    animation: banner-throb 1s ease-in-out infinite;
    pointer-events: none;
  }

  .word {
    font-size: 3.6rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-indent: 0.12em;
    text-transform: uppercase;
    color: #ffffff;
    line-height: 1;
  }

  .why {
    color: rgba(255, 255, 255, 0.85);
  }
</style>

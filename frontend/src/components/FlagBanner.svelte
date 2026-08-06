<script lang="ts">
  /**
   * The word, in the full width of the screen.
   *
   * The shift strip already turns the flag colour, but colour alone cannot be
   * the whole message — this carries the text. Only renders for states that
   * change what the driver should do right now.
   */
  import { SAFETY_CAR_LABEL, type FlagState, type SafetyCarState } from '../lib/enums';

  interface Props {
    flag: FlagState;
    safetyCar: SafetyCarState;
  }

  let { flag, safetyCar }: Props = $props();

  type Banner = { tone: 'red' | 'yellow' | 'amber'; text: string } | null;

  let banner: Banner = $derived.by(() => {
    if (flag === 'red') return { tone: 'red', text: 'Red flag' };
    if (safetyCar === 'full') return { tone: 'amber', text: SAFETY_CAR_LABEL.full };
    if (safetyCar === 'virtual') return { tone: 'amber', text: SAFETY_CAR_LABEL.virtual };
    if (flag === 'yellow') return { tone: 'yellow', text: 'Yellow flag' };
    if (safetyCar === 'formation') return { tone: 'amber', text: SAFETY_CAR_LABEL.formation };
    return null;
  });
</script>

{#if banner}
  <div class="banner {banner.tone}" role="status">{banner.text}</div>
{/if}

<style>
  .banner {
    grid-area: banner;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.28rem;
    font-size: 1.35rem;
    font-weight: 800;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    text-indent: 0.24em; /* balance the trailing letter-space */
  }

  .banner.red {
    background: var(--red);
    color: #ffffff;
    animation: banner-throb 0.85s ease-in-out infinite;
  }
  .banner.yellow {
    background: var(--yellow);
    color: #14150f;
  }
  .banner.amber {
    background: var(--amber);
    color: #1a1204;
  }
</style>

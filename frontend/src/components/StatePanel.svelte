<script lang="ts" generics="T">
  /**
   * Loading, empty, and the several ways a request can fail — in one place, so
   * every analysis page answers "why is there nothing here?" the same way.
   *
   * The 503 gets its own treatment because it is the failure a user of this
   * project will actually hit. The backend collapses every database problem
   * into an opaque `{"detail": "database unavailable"}` on purpose (the
   * endpoint is unauthenticated and must not leak why), so the panel says what
   * is true — the archive is not reachable — and names the likely cause without
   * claiming to know it. Everything else the pit wall does keeps working, and
   * saying so is the difference between "broken" and "this part is offline".
   */
  import { ApiError } from '../lib/analysis-api';
  import type { Async } from '../lib/analysis.svelte';
  import type { Snippet } from 'svelte';

  interface Props {
    state: Async<T>;
    /** True when the request succeeded but there is nothing to show. */
    isEmpty?: (data: T) => boolean;
    /** What "nothing" means here, in the page's own words. */
    emptyMessage?: string;
    /** Noun for the loading line: "sessions", "laps", "the comparison". */
    loadingLabel?: string;
    children: Snippet<[T]>;
    /** Extra actions rendered inside the error panel — a retry, usually. */
    actions?: Snippet;
  }

  let {
    state,
    isEmpty,
    emptyMessage = 'Nothing to show.',
    loadingLabel = 'data',
    children,
    actions
  }: Props = $props();

  interface Explained {
    kind: string;
    title: string;
    detail: string;
    hint: string | null;
  }

  function explain(error: unknown): Explained {
    if (error instanceof ApiError) {
      switch (error.kind) {
        case 'unavailable':
          return {
            kind: 'unavailable',
            title: 'Database unavailable',
            detail: 'The session archive is not reachable, so nothing can be analysed.',
            hint: 'The live pit wall does not need the database and is unaffected.'
          };
        case 'notfound':
          return {
            kind: 'notfound',
            title: 'Not found',
            detail: error.message,
            hint: 'It may have been recorded under a different session.'
          };
        case 'invalid':
          return {
            kind: 'invalid',
            title: 'That comparison is not possible',
            detail: error.message,
            hint: 'Two laps can only be compared at the same track.'
          };
        case 'network':
          return {
            kind: 'network',
            title: 'No connection',
            detail: 'The request never reached the server.',
            hint: 'Check that the backend is running.'
          };
        default:
          return {
            kind: 'other',
            title: 'Request failed',
            detail: error.message,
            hint: null
          };
      }
    }
    return {
      kind: 'other',
      title: 'Something went wrong',
      detail: error instanceof Error ? error.message : String(error),
      hint: null
    };
  }
</script>

{#if state.status === 'loading'}
  <div class="state loading" data-state="loading">
    <span class="bar"></span>
    <p class="label">Loading {loadingLabel}</p>
  </div>
{:else if state.status === 'error'}
  {@const e = explain(state.error)}
  <div class="state error" data-state="error" data-error-kind={e.kind} role="alert">
    <p class="title">{e.title}</p>
    <p class="detail">{e.detail}</p>
    {#if e.hint}<p class="hint">{e.hint}</p>{/if}
    {#if actions}<div class="actions">{@render actions()}</div>{/if}
  </div>
{:else if isEmpty?.(state.data)}
  <div class="state empty" data-state="empty">
    <p class="detail">{emptyMessage}</p>
  </div>
{:else}
  {@render children(state.data)}
{/if}

<style>
  .state {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding: 1.4rem 1rem;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--surface-1);
    text-align: center;
    align-items: center;
  }

  /*
   * An indeterminate bar rather than a spinner: it occupies the width the
   * content will occupy, so the page does not jump when the data lands.
   */
  .bar {
    width: min(18rem, 60%);
    height: 2px;
    border-radius: 1px;
    background: linear-gradient(
      90deg,
      transparent,
      var(--line-strong) 35%,
      var(--ink-3) 50%,
      var(--line-strong) 65%,
      transparent
    );
    background-size: 220% 100%;
    animation: load-sweep 1.4s ease-in-out infinite;
  }

  @keyframes load-sweep {
    from {
      background-position: 120% 0;
    }
    to {
      background-position: -120% 0;
    }
  }

  .title {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--ink);
  }

  .detail {
    margin: 0;
    font-size: 0.85rem;
    color: var(--ink-2);
    max-width: 34rem;
  }

  .hint {
    margin: 0;
    font-size: 0.78rem;
    color: var(--ink-3);
    max-width: 34rem;
  }

  .actions {
    margin-top: 0.35rem;
    display: flex;
    gap: 0.4rem;
  }

  /*
   * The one place amber is spent on this side of the app. An unreachable
   * archive is a degraded-service state, which is exactly what amber means on
   * the pit wall's health dot — reusing it keeps one vocabulary across both
   * halves of the product.
   */
  .error[data-error-kind='unavailable'],
  .error[data-error-kind='network'] {
    border-color: color-mix(in srgb, var(--amber) 40%, var(--line));
    background: linear-gradient(var(--amber-dim), var(--surface-1) 70%);
  }

  .error[data-error-kind='unavailable'] .title,
  .error[data-error-kind='network'] .title {
    color: var(--amber);
  }

  .empty {
    color: var(--ink-3);
  }
</style>

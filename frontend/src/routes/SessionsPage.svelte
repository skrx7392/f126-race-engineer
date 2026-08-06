<script lang="ts">
  /**
   * The session browser: the front door to everything on this side of the app.
   *
   * Newest first, because the session you want is nearly always the one you
   * just drove. Five columns and no more — date, track, type, laps, best lap —
   * since the only job here is to let you recognise a session and open it.
   *
   * Rows navigate by containing a real link that is stretched over the row with
   * a pseudo-element. That is deliberately not a click handler on the `<tr>`:
   * a link is focusable, announces itself, works with the keyboard, and can be
   * opened in a new tab — all of which a div with an `onclick` gives up.
   */
  import { fetchSessions, type SessionSummary } from '../lib/analysis-api';
  import { LOADING, type Async } from '../lib/analysis.svelte';
  import { href } from '../lib/router.svelte';
  import { formatLapTime, DASH } from '../lib/format';
  import { formatSessionDate } from '../lib/analysis-format';
  import StatePanel from '../components/StatePanel.svelte';

  let sessions = $state<Async<SessionSummary[]>>(LOADING);
  /** Bumped to re-run the load effect after a failure. */
  let attempt = $state(0);

  $effect(() => {
    void attempt;
    const controller = new AbortController();
    sessions = LOADING;
    fetchSessions(50, controller.signal)
      .then((data) => {
        sessions = { status: 'ok', data };
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        sessions = { status: 'error', error };
      });
    return () => controller.abort();
  });
</script>

<section class="analysis-page" data-page="sessions">
  <header class="page-head">
    <div>
      <h1 class="page-title">Sessions</h1>
      <p class="page-sub">Everything the recorder has kept, newest first.</p>
    </div>
  </header>

  <StatePanel
    state={sessions}
    loadingLabel="sessions"
    isEmpty={(rows) => rows.length === 0}
    emptyMessage="No sessions recorded yet. Drive one with the recorder running and it will appear here."
  >
    {#snippet actions()}
      <button class="btn" type="button" onclick={() => attempt++}>Try again</button>
    {/snippet}

    {#snippet children(rows)}
      <div class="panel">
        <div class="table-scroll">
          <table class="data-table" data-testid="sessions-table">
            <caption class="label">{rows.length} sessions</caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Track</th>
                <th scope="col">Type</th>
                <th scope="col" class="num" title="Lap records stored, counted across every car">
                  Laps
                </th>
                <th scope="col" class="num">Best lap</th>
              </tr>
            </thead>
            <tbody>
              {#each rows as session (session.id)}
                <tr class="row" data-session-id={session.id}>
                  <td class="link-cell">
                    <a class="row-link" href={href(`/sessions/${session.id}`)}>
                      {formatSessionDate(session.started_at_wall)}
                    </a>
                  </td>
                  <td class="track">{session.track_name ?? DASH}</td>
                  <td class="type">{session.session_type_name ?? DASH}</td>
                  <td class="num">{session.lap_count}</td>
                  <!--
                    `best_lap_ms` is computed by the detail endpoint, not the
                    list one. Rather than firing a detail request per row, the
                    column renders it when the list happens to carry it and an
                    em dash when it does not — the same rule the rest of the app
                    uses for an unknown value.
                  -->
                  <td class="num clock">{formatLapTime(session.best_lap_ms)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/snippet}
  </StatePanel>
</section>

<style>
  .row {
    position: relative;
  }

  .row-link {
    color: var(--ink);
    text-decoration: none;
    font-weight: 600;
  }

  /* Stretch the link over the whole row without nesting interactive content. */
  .row-link::after {
    content: '';
    position: absolute;
    inset: 0;
  }

  .row:focus-within td {
    background: var(--surface-2);
  }

  .row-link:focus-visible::after {
    outline: 2px solid var(--line-strong);
    outline-offset: -2px;
  }

  .track {
    font-weight: 600;
  }

  .type {
    color: var(--ink-2);
  }
</style>

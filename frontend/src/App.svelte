<script lang="ts">
  /**
   * The route switch, and nothing else.
   *
   * Two halves of one product live behind this file. `#/` is the live pit wall:
   * a kiosk, no chrome, no navigation, exactly as it was before analysis
   * existed. Everything else is the analysis side: a scroll container, a thin
   * nav, and one page. They share the design tokens, the formatters and the
   * WebSocket store, and share no layout at all — because a screen you glance at
   * from 1.5 m and a screen you read numbers off are not the same screen.
   *
   * The pit wall is mounted only on its own route rather than hidden with CSS,
   * so an analysis page never carries the cost of a live dashboard rendering
   * behind it at 10 Hz.
   */
  import { analysis } from './lib/analysis.svelte';
  import { intParam, isAnalysisRoute, router } from './lib/router.svelte';
  import PitWall from './layouts/PitWall.svelte';
  import AnalysisNav from './components/AnalysisNav.svelte';
  import SessionsPage from './routes/SessionsPage.svelte';
  import SessionDetailPage from './routes/SessionDetailPage.svelte';
  import ComparePage from './routes/ComparePage.svelte';
  import CornersPage from './routes/CornersPage.svelte';
  import StintsPage from './routes/StintsPage.svelte';
  import StrategyPage from './routes/StrategyPage.svelte';
  import CareerPage from './routes/CareerPage.svelte';
  import TrackProgressPage from './routes/TrackProgressPage.svelte';

  $effect(() => router.start());

  let route = $derived(router.route);

  /**
   * The URL seeds the shared selection on every navigation. A pasted
   * `#/compare?sa=3&la=12&sb=3&lb=9` has to reopen those two laps, and a link
   * from the session page to the corner page has to carry its lap across.
   * Slots the URL does not mention are left alone, so navigating between
   * analysis pages never silently drops a selection.
   */
  $effect(() => {
    analysis.adoptFromQuery(route.query);
  });

  /* Corners and stints address a session through the query string; both fall
     back to the current selection so the nav links work with no parameters. */
  let cornerSession = $derived(intParam(route.query, 'session') ?? analysis.a?.sessionId ?? null);
  let cornerLap = $derived(intParam(route.query, 'lap') ?? analysis.a?.lap ?? null);
  let cornerRef = $derived(route.query.get('ref') ?? 'best');
  /** Read the reference lap out of another session at the same circuit. */
  let cornerRefSession = $derived(intParam(route.query, 'ref_session'));
  let stintSession = $derived(intParam(route.query, 'session') ?? analysis.a?.sessionId ?? null);

  /* Strategy addresses a *circuit*, not a session — a strategy sheet is built from the
     whole weekend at one track — so it takes its own two parameters and falls back to
     nothing rather than to the lap selection. */
  let strategyTrack = $derived(intParam(route.query, 'track'));
  let strategyLaps = $derived(intParam(route.query, 'laps'));
</script>

{#if route.name === 'pitwall'}
  <PitWall />
{:else if isAnalysisRoute(route.name)}
  <div class="analysis-shell" data-shell="analysis">
    <AnalysisNav current={route.name} />
    <div class="analysis-body">
      {#if route.name === 'sessions'}
        <SessionsPage />
      {:else if route.name === 'session'}
        <SessionDetailPage sessionId={Number(route.params['id'])} />
      {:else if route.name === 'compare'}
        <ComparePage />
      {:else if route.name === 'corners'}
        <CornersPage
          sessionId={cornerSession}
          lap={cornerLap}
          ref={cornerRef}
          refSession={cornerRefSession}
        />
      {:else if route.name === 'stints'}
        <StintsPage sessionId={stintSession} />
      {:else if route.name === 'strategy'}
        <StrategyPage trackId={strategyTrack} raceLaps={strategyLaps} />
      {:else if route.name === 'career'}
        <CareerPage />
      {:else if route.name === 'careertrack'}
        <TrackProgressPage trackId={Number(route.params['track_id'])} />
      {:else}
        <section class="analysis-page" data-page="notfound">
          <h1 class="page-title">No such page</h1>
          <p class="page-sub">
            <code>{route.path}</code> is not a route in this app.
          </p>
          <p><a class="btn" href="#/sessions">Back to sessions</a></p>
        </section>
      {/if}
    </div>
  </div>
{/if}

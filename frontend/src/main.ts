import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { store, WsClient } from './lib/ws.svelte';
import { mockRequest, startMock } from './lib/mock';
import { installAnalysisMock } from './lib/analysis-mock';
import { installChartPalette } from './lib/chart-theme';

const target = document.getElementById('app');
if (!target) throw new Error('#app mount point missing from index.html');

// The chart palette lives in TypeScript because the canvas needs it there; this
// publishes it to CSS so the chips and bars around the charts can use it too.
installChartPalette();

const app = mount(App, { target });

/*
 * Mock mode and live mode differ only in who produces messages — both write to
 * the same store through the same handler, so there is no second code path to
 * keep in sync.
 */
const mock = mockRequest();
if (mock.enabled) {
  startMock(store, { kind: mock.kind, warpSeconds: mock.warpSeconds });

  /*
   * The analysis pages talk to the archive over `fetch`, not the socket, so
   * mock mode has a second producer to install — at the transport boundary, for
   * the same reason: the pages stay identical between mock and live.
   *
   * `?fail=503` makes every analysis endpoint fail, which is the only practical
   * way to look at the "database unavailable" panel without taking a database
   * away. The small delay exists so the loading states are visible at all.
   */
  const params = new URLSearchParams(typeof location === 'undefined' ? '' : location.search);
  const failRaw = Number(params.get('fail'));
  installAnalysisMock({
    delayMs: 140,
    failStatus: Number.isFinite(failRaw) && failRaw >= 400 ? failRaw : null
  });
} else {
  new WsClient(store).start();
}

export default app;

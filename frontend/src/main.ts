import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { store, WsClient } from './lib/ws.svelte';
import { mockRequest, startMock } from './lib/mock';

const target = document.getElementById('app');
if (!target) throw new Error('#app mount point missing from index.html');

const app = mount(App, { target });

/*
 * Mock mode and live mode differ only in who produces messages — both write to
 * the same store through the same handler, so there is no second code path to
 * keep in sync.
 */
const mock = mockRequest();
if (mock.enabled) {
  startMock(store, { kind: mock.kind, warpSeconds: mock.warpSeconds });
} else {
  new WsClient(store).start();
}

export default app;

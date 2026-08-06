/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// The backend (src/f126/config.py) serves this build from `frontend/dist` and
// hosts the WebSocket at /ws on the same origin, so relative asset URLs keep the
// bundle mountable at any path.
export default defineConfig({
  base: './',
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    // One screen, no routes: a single chunk beats a waterfall on a pit-wall iPad.
    chunkSizeWarningLimit: 900
  },
  server: {
    port: 5173,
    // `npm run dev` against a live backend: proxy the socket so the page can
    // always talk to same-origin /ws without any env configuration.
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true, changeOrigin: true }
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.ts'],
    setupFiles: ['./src/tests/setup.ts'],
    restoreMocks: true
  },
  // Svelte 5 ships separate server/client entry points; under jsdom we need the
  // client build or component tests mount nothing.
  resolve: process.env.VITEST ? { conditions: ['browser'] } : {}
});

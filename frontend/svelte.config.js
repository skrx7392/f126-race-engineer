import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
  preprocess: vitePreprocess(),
  compilerOptions: {
    // Runes mode everywhere; no legacy reactive statements.
    runes: true
  }
};

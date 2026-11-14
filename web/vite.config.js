import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// No SvelteKit here on purpose. FastAPI already owns the routes, the auth and
// the read-model; a second server would only duplicate them. So this builds to
// static files that FastAPI serves from /assets, and the app is one bundle that
// picks a view from `location.pathname`.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  resolve: {
    alias: {
      // `shadcn-svelte` resolves `aliases.components` through this, so a block's
      // alias-relative target (`nb/AppShell.svelte`) lands in src/lib/components.
      $lib: "/src/lib",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  // `npm run dev` needs a server for the JSON: the pages fetch `/ui/*` and the
  // same-origin rule means a bare Vite server has nothing to answer with. In
  // production FastAPI serves both the bundle and the JSON, so the proxy is a
  // development-only stand-in -- and it points at the API port, not at a second
  // copy of the app.
  server: {
    proxy: {
      "/ui": "http://127.0.0.1:3000",
      "/balance": "http://127.0.0.1:3000",
      "/analysis": "http://127.0.0.1:3000",
    },
  },
});

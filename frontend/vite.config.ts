/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev only: Vite serves the SPA on 5173 and forwards API calls to FastAPI, so the
// browser sees one origin and the session cookie just works (tech-design §2.4).
// The app's port is 8765 (the OAuth loopback callback targets it); override with
// HISAAB_API=http://host:port if the backend runs elsewhere.
const api = process.env.HISAAB_API ?? "http://localhost:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": api, "/health": api },
  },
  build: {
    // Everything bundled into dist/: the app must work with no network (PRD §8.2).
    assetsInlineLimit: 0,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    // Stylesheets are blanked in tests, except tokens.css, which the contrast test reads.
    css: { include: [/tokens\.css/] },
  },
});

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Dev: the UI runs on :5173 and proxies /api to `driveguard api` on :8100 (same-origin in production,
// where the API serves web/dist itself). No CORS is ever enabled on the API.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { "/api": "http://127.0.0.1:8100" } },
  build: { outDir: "dist", sourcemap: false },
  test: { environment: "jsdom", globals: true },
});

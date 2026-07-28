/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const API_PROXY = {
  "/api": { target: process.env.E2E_API_URL ?? "http://127.0.0.1:8000", changeOrigin: true },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The API is proxied rather than called cross-origin, so there is no CORS
  // configuration to get wrong and the deployed build can sit behind a single
  // origin using the same paths it uses in development.
  //
  // `server` and `preview` need this separately - `server.proxy` applies only to
  // `vite dev`. Without the second block the E2E suite runs against the preview
  // build and every API call 404s, which looks like a broken app rather than a
  // missing proxy.
  server: { proxy: API_PROXY },
  preview: { proxy: API_PROXY },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// Vite dev server proxies the FastAPI backend so the SPA can hit
// /api/v1, /events, and the cookie-setting endpoints /switch,
// /theme/toggle, /agent/sidebar without CORS headaches.
const BACKEND =
  process.env.VITE_BACKEND_URL ?? "http://host.docker.internal:8100";

// Route prefixes that must pass through to FastAPI in dev; everything
// else is handled by Vite (and in prod, by the SPA catch-all).
const API_PATHS = [
  "/api",
  "/events",
  "/switch",
  "/theme",
  "/agent",
  "/healthz",
  "/readyz",
  "/metrics",
];

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      strategies: "generateSW",
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api/, /^\/events/],
        runtimeCaching: [
          {
            urlPattern: /\/api\/v1\/tasks.*$/,
            handler: "StaleWhileRevalidate",
            options: { cacheName: "tasks-cache" },
          },
        ],
      },
      manifest: {
        name: "miployees",
        short_name: "miployees",
        theme_color: "#3F6E3B",
        background_color: "#FAF7F2",
        display: "standalone",
        start_url: "/",
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: Object.fromEntries(
      API_PATHS.map((p) => [p, { target: BACKEND, changeOrigin: true, ws: true }]),
    ),
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
});

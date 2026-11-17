import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  build: {
    outDir: path.resolve(__dirname, "../src/digital_employee/web/static"),
    assetsDir: "assets",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8020",
      "/webhooks": "http://127.0.0.1:8020",
    },
  },
});

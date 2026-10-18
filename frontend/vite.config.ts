import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Panel jest serwowany przez bota pod /panel/ (bot/web.py). W trybie dev (npm run dev)
// zapytania /api idą do bota uruchomionego lokalnie na porcie 8080.
export default defineConfig({
  base: "/panel/",
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": process.env.BOT_URL ?? "http://localhost:8080" },
  },
});

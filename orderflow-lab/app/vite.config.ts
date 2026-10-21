/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps every asset path relative, so the build works from any folder or host.
// `vite build --mode artifact` is the private claude.ai artifact build: it alone includes the owner's
// Binance connector source (src/data/connector.ts, loaded behind import.meta.env.MODE === "artifact"),
// writes to dist/artifact and keeps everything in one script so it can be inlined into one page.
export default defineConfig(({ mode }) => ({
  base: "./",
  plugins: [react()],
  ...(mode === "artifact"
    ? { publicDir: false as const, build: { outDir: "dist/artifact", emptyOutDir: true, rollupOptions: { output: { inlineDynamicImports: true } } } }
    : {}),
  test: { environment: "node", include: ["src/**/*.test.ts"] },
}));

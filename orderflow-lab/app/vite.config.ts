/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps every asset path relative, so the build works from any folder or host
export default defineConfig({
  base: "./",
  plugins: [react()],
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});

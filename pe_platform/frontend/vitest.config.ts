import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest + React Testing Library substrate (LD-T10 / HG-TOOL Axis: frontend
// component tests). Stood up from zero — the repo had 0 component tests across
// 76 .tsx files. This config only WIRES the runner; the component tests + the
// 4-state matrix + MSW handlers land in Phase 4.3.
//
// The Playwright e2e specs live under frontend/e2e and run via `playwright test`
// (the `e2e` script), NOT vitest — so they are excluded here to keep the two
// runners from colliding.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["{app,components,lib}/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
    css: false,
  },
  resolve: {
    alias: {
      // Mirror tsconfig `@/* -> ./*` so test imports resolve like app code.
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
});

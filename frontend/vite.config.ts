import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // Tailwind v4 is CSS-first — this plugin plus the single
  // `@import "tailwindcss"` in `src/styles/app.css` is the entire mechanism.
  // No `tailwind.config.js`, no PostCSS config.
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      // the local-vs-oidc mode probe reads `GET /openapi.json`
      // directly. FastAPI serves the schema at the app root, outside every
      // `/api`-prefixed router (`main.create_app`), so it needs its own
      // proxy entry rather than falling under the rule above.
      "/openapi.json": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  // Vitest reuses this config and Vite's resolution — there is no second
  // toolchain (ADR 0021). `environment: "node"` stays the default at this
  // top level: the scope is decision logic, not rendering, so a component
  // cannot be mounted by an ordinary `*.test.ts` file by construction.
  // `src/test/setup.ts` stubs the one browser API the code under test
  // actually touches.
  //
  // ADR 0021 §Amendment 2026-08-30: `*.a11y.test.tsx` is the one
  // narrow exception — `include` widens to collect those files, but the
  // environment they run under is **not** set here. Deliberately no
  // `environmentMatchGlobs`: that would give the pattern jsdom with no
  // docblock at all, and the docblock is not decorative — the guard in
  // `src/test/jsdomEnvironmentPragma.test.ts` scans for exactly that per-file
  // `// @vitest-environment jsdom` comment, so a config-side shortcut here
  // would make that guard check a string nothing depends on.
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.a11y.test.tsx"],
    setupFiles: ["./src/test/setup.ts"],
  },
});

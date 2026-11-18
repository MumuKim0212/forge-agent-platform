/**
 * Vitest setup.
 *
 * At the top level, deliberately **no DOM**. ADR 0021 scopes the suite to
 * decision logic, and a `node` environment makes that a property of the
 * runner rather than a rule people have to remember: there is no `document`
 * to render into. The only browser API the code under test touches there is
 * `localStorage` (the dev-mode identity key in `api/client.ts`), so that is
 * the only thing stubbed — fresh per test, so no test can see another's
 * stored key.
 *
 * ADR 0021 §Amendment 2026-08-30: this file also runs, unchanged, as
 * `setupFiles` for the one narrow exception — `*.a11y.test.tsx` files carry
 * their own per-file `@vitest-environment` docblock naming jsdom, and Vitest
 * re-runs every `setupFiles` entry once per resolved environment. (Written
 * with a deliberate gap between the tag and the name here, not as a literal
 * comment — see `src/test/jsdomEnvironmentPragma.test.ts`'s file docblock
 * for why: that exact adjacency, anywhere in a file's own text, is what
 * Vitest's own environment grouping matches on, and this file is an
 * ordinary one.) So the two
 * additions below are guarded rather than split into a second setup file:
 * `localStorage` stubbing is identical either way (`vi.stubGlobal` shadows
 * jsdom's own real `Storage` the same way it shadows nothing under `node`),
 * and the `@testing-library/jest-dom` matcher extension has no DOM access at
 * import time (verified by reading `@testing-library/jest-dom/dist/vitest.mjs`
 * — it only calls `expect.extend`), so it is harmless to load under `node`
 * too. `cleanup()` is guarded on `typeof document` even though
 * `@testing-library/react`'s own implementation is a no-op with zero mounted
 * roots (nothing under `node` ever calls `render`) — the guard documents why,
 * at no cost, the same idiom `a11y/activation.ts` already uses.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

function createMemoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() {
      return entries.size;
    },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    removeItem: (key: string) => {
      entries.delete(key);
    },
    setItem: (key: string, value: string) => {
      entries.set(key, String(value));
    },
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", createMemoryStorage());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // jsdom-only: unmount anything an `*.a11y.test.tsx` file rendered, so the
  // next `it()` in the same file starts from an empty `document.body` rather
  // than accumulating every previous test's markup.
  if (typeof document !== "undefined") {
    cleanup();
  }
});

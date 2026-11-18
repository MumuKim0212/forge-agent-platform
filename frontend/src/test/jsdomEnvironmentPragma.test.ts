/**
 * ADR 0021 §Amendment 2026-08-30 permits jsdom in exactly one place:
 * files named `*.a11y.test.tsx`, opted in per file with a `// @vitest-
 * environment jsdom` docblock (the constants below spell it in two pieces —
 * see "Why this file never writes the tag next to an environment name"
 * further down). This is the test that makes that rule mechanical rather
 * than remembered — the same shape this project already uses twice on the
 * backend (`test_rbac.py`'s role-literal scan, `test_console.py`'s
 * settings-serialisation scan): one detector function, called by both the
 * real repo-wide scan and its own anti-vacuity tests, never re-implemented
 * on either side.
 *
 * Verified against the installed `vitest@3.2.7`, not assumed: its own
 * per-file environment grouping (`groupFilesByEnv`, vendored inside the
 * `vitest` package) reads each collected spec file's *raw text* and matches
 * the tag followed by whitespace and a bare environment name *anywhere* in
 * that text — not inside a comment block anchored at the top of the file.
 * A throwaway probe file confirmed this directly: putting the tag on the
 * *last* line of an ordinary `.test.ts` file still ran that file under
 * jsdom. `matchedEnvironment` below ports that exact behaviour, so this
 * test flags precisely what would make Vitest itself switch environment for
 * a file — nothing narrower, nothing invented.
 *
 * Why this file never writes the tag next to an environment name: because
 * the real match is whole-file-text, not comment-scoped, the byte sequence
 * "the tag, whitespace, then a bare environment name" appearing *anywhere*
 * on this file's own disk contents — including inside a quoted string meant
 * only as an in-memory fixture — would make Vitest pull *this ordinary
 * `.test.ts` file* into jsdom too, the one thing ADR 0021 §Amendment says
 * must not happen outside the pattern. (`src/test/setup.ts`'s first draft
 * did exactly this in an explanatory comment and was caught by this file's
 * own "flags no file outside the pattern" test — the guard working as
 * intended, on an unintended victim.) So every occurrence below is built
 * from two separate constants at runtime (`PRAGMA_TAG`, then `JSDOM_ENV`),
 * joined only inside a template literal — the joined bytes exist in memory
 * during a test run and never on disk. That is also why this file needs no
 * self-exemption in the scan below: it is never written as one contiguous
 * match in the first place, unlike the first version of this idea, where
 * exempting the enforcement test would have silently exempted the one file
 * most likely to be edited later.
 *
 * File discovery uses `import.meta.glob` (Vite's own static glob-import,
 * typed by the existing `vite/client` reference in `src/vite-env.d.ts`)
 * rather than Node's `fs` — this project has no `@types/node` dependency,
 * and reading every `src/**` file's raw text is exactly what `?raw` +
 * `eager: true` is for. Nothing here reaches outside `src/`.
 */
import { describe, expect, it } from "vitest";

const PRAGMA_TAG = "@vitest-environment";
const JSDOM_ENV = "jsdom";

const A11Y_FILE_RE = /\.a11y\.test\.tsx$/;

// Eager + `?raw` so every match is plain text at collection time, never
// parsed/executed as a module and never re-registering another file's
// tests — verified: the total below is the current file count, not double
// the suite's test count.
const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/**
 * The one detector both the repo-wide scan and the anti-vacuity tests below
 * call. Same pattern Vitest's own file-environment grouping applies to raw
 * file text (see the file docblock) — `PRAGMA_TAG` followed by whitespace
 * and a bare word, matched anywhere in the source, not just at the top.
 */
function matchedEnvironment(source: string): string | null {
  const pattern = new RegExp(`${PRAGMA_TAG}\\s+([\\w-]+)\\b`);
  return source.match(pattern)?.[1] ?? null;
}

describe("no file outside *.a11y.test.tsx carries the jsdom environment pragma (ADR 0021 §Amendment)", () => {
  it("the glob walk actually reaches a known file", () => {
    // Vacuity guard 1: a walk that globs nothing would make the "flags no
    // ordinary file" assertion below pass for the wrong reason.
    expect(Object.keys(SOURCE_FILES)).toContain("../main.tsx");
  });

  it("the detector actually fires on the two real *.a11y.test.tsx files this task adds", () => {
    // Vacuity guard 2: otherwise "offenders is empty" below could just as
    // easily mean the detector never matches anything, not that the repo is
    // clean. Read from the same glob as the real scan — these are the two
    // files c1/c2 add, carrying the pragma for real.
    const fixtures = [
      "../pages/admin/TemplatesPanel.a11y.test.tsx",
      "../components/ui/Field.a11y.test.tsx",
    ];
    for (const key of fixtures) {
      expect(Object.keys(SOURCE_FILES)).toContain(key);
      expect(matchedEnvironment(SOURCE_FILES[key])).toBe(JSDOM_ENV);
    }
  });

  it("flags no file outside the pattern", () => {
    const offenders: string[] = [];
    for (const [file, source] of Object.entries(SOURCE_FILES)) {
      if (A11Y_FILE_RE.test(file)) continue;
      if (matchedEnvironment(source) === JSDOM_ENV) {
        offenders.push(file);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("ADR's own non-vacuity requirement: planting the pragma into a real ordinary file's source turns the detector red", () => {
    // Vacuity guard 3, the ADR-mandated one: "adding the docblock to an
    // ordinary test file has to turn it red." Ported from `test_rbac.py`'s
    // `test_the_bootstrap_password_exemption_is_narrow` — plant into a
    // read copy of a real file's source, never the file on disk.
    const targetKey = "../auth/roles.test.ts";
    const realSource = SOURCE_FILES[targetKey];
    expect(realSource).toBeTypeOf("string");

    // Non-vacuous: the real file, unmodified, does not already carry it.
    expect(matchedEnvironment(realSource)).toBeNull();

    // Joined only here, in memory — see the file docblock. No file is
    // written; `plantedSource` never touches disk.
    const plantedLine = `// ${PRAGMA_TAG} ${JSDOM_ENV}\n`;
    const plantedSource = plantedLine + realSource;
    expect(plantedSource).not.toBe(realSource);

    expect(matchedEnvironment(plantedSource)).toBe(JSDOM_ENV);
  });
});

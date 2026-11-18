/**
 * ADR 0028: the trace viewer renders L3-authored
 * content in the platform origin, so the files that render it may not carry a raw-HTML
 * escape hatch or a live region. A plain-text scan of their raw source, the shape of
 * `src/test/targetBlankRel.test.ts`: one detector, called by the real scan and by its own
 * anti-vacuity test.
 */
import { describe, expect, it } from "vitest";

const FILES = import.meta.glob(["./BuildTracePanel.tsx", "./buildTraceView.ts", "./Timeline.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const FORBIDDEN: Array<{ name: string; pattern: RegExp }> = [
  { name: "raw HTML escape hatch", pattern: /dangerously[A-Za-z]*HTML/ },
  { name: "aria-live", pattern: /aria-live/ },
  { name: "role alert or status", pattern: /role\s*=\s*\{?\s*["'](alert|status)["']/ },
  { name: "a live Alert", pattern: /<Alert\b[^>]*\blive\b/ },
  { name: "markdown or html parser import", pattern: /from\s+["'](marked|markdown-it|remark[^"']*|react-markdown|dompurify|sanitize-html)["']/ },
  { name: "innerHTML", pattern: /\.innerHTML\s*=/ },
];

function violations(source: string): string[] {
  return FORBIDDEN.filter((f) => f.pattern.test(source)).map((f) => f.name);
}

describe("the files that render a build trace", () => {
  it("scans the three files it names (not an empty glob)", () => {
    expect(Object.keys(FILES).sort()).toEqual(["./BuildTracePanel.tsx", "./Timeline.tsx", "./buildTraceView.ts"]);
  });

  it("carry no raw-HTML escape hatch, live region or markdown parser", () => {
    for (const [file, source] of Object.entries(FILES)) {
      expect({ file, violations: violations(source) }).toEqual({ file, violations: [] });
    }
  });

  it("the detector sees each forbidden shape", () => {
    expect(violations("<div dangerouslySetInnerHTML={{ __html: x }} />")).toEqual(["raw HTML escape hatch"]);
    expect(violations('<p aria-live="polite" />')).toEqual(["aria-live"]);
    expect(violations('<p role="alert" />')).toEqual(["role alert or status"]);
    expect(violations('<Alert variant="error" live>x</Alert>')).toEqual(["a live Alert"]);
    expect(violations('import { marked } from "marked";')).toEqual(["markdown or html parser import"]);
    expect(violations("el.innerHTML = x;")).toEqual(["innerHTML"]);
    expect(violations('<Alert variant="error">x</Alert>')).toEqual([]);
  });
});

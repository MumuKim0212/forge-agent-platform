/**
 * ADR 0028 §Consequences (tab-nabbing).
 *
 * **The rule.** Anything in `frontend/src` that opens a new browsing context must sever
 * `window.opener`: a JSX element whose `target` is not one of `_self` / `_parent` / `_top` (so
 * `"_blank"`, a named window such as `"preview"`, or a value that is not a literal and could be
 * either) must carry a `rel` that is a plain string containing the token `noreferrer` or
 * `noopener`, and a `window.open(...)` call needs the same token in its features string (its third
 * argument). Without it even a sandboxed (ADR 0028 layer 1) preview could navigate the tab that
 * opened it. **Either token satisfies the rule**: `noopener` is what severs the opener, and
 * `noreferrer` implies it and also withholds the referrer, which is why ADR 0028 asks for it on
 * preview links and why both real links use it; this guard does not require the stricter one, because
 * the hazard it exists for is the opener.
 *
 * **Why a parser.** An earlier check was a regex over `<a\b[^>]*>`, which stops at the first `>`, and
 * `=>` has one: `<a onClick={(e) => ...} target="_blank">` was cut off before its `target` and
 * passed with no `rel` at all, while a `<Link target="_blank">` was never looked at. This reads each
 * file with the TypeScript compiler (already a dependency; nothing is executed or type-checked) and
 * asks the syntax tree, so an attribute is found wherever it sits and whatever the handler before it
 * contains. Every JSX element is checked, `<a>`, `<Link>`, `<NavLink>` or any component alike: a
 * component that forwards `target` to an anchor is the same hazard one level down.
 *
 * **What it does not see** (so a clean run is not read as more than it is): `target` or `rel` set
 * from a spread (`{...props}` is flagged on any element that also has a new-context `target`, but a
 * spread that merely *carries* a `target` onto an element with none is not looked for), an anchor
 * built with `document.createElement`, a `window` held in another variable, and an `<a>` in a file
 * outside `src/`.
 *
 * Same shape as `src/test/jsdomEnvironmentPragma.test.ts`: one detector, called by the repo-wide scan
 * and by the fixtures that prove it can fail, never re-implemented on either side.
 * The fixtures are strings in this file, never shipped code. File discovery uses
 * `import.meta.glob` (`?raw`, `eager: true`); nothing here reaches outside `src/`.
 */
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** A `target` that stays in the current tab (or its parent frames): no new context, no opener. */
const SAME_CONTEXT_TARGETS = ["_self", "_parent", "_top"];
/** Either severs `window.opener`; see the file docblock for why both are accepted. */
const SAFE_TOKENS = ["noopener", "noreferrer"];
/** Receivers of `.open(...)` that are the window. */
const WINDOW_NAMES = ["window", "globalThis", "self"];

interface NewContext {
  kind: "jsx" | "window.open";
  /** The element's tag name, or the call's callee as written. */
  tag: string;
  line: number;
  safe: boolean;
  /** Why it is not safe; empty when it is. */
  reason: string;
}

/** The text of an expression that is a plain string at compile time, else null. */
function staticText(node: ts.Node | undefined): string | null {
  let current = node;
  while (
    current !== undefined &&
    (ts.isParenthesizedExpression(current) ||
      ts.isAsExpression(current) ||
      ts.isNonNullExpression(current) ||
      ts.isSatisfiesExpression(current))
  ) {
    current = current.expression;
  }
  if (current === undefined) return null;
  if (ts.isStringLiteral(current) || ts.isNoSubstitutionTemplateLiteral(current)) return current.text;
  return null;
}

/** An attribute's value as a plain string, or null (no value, or not a literal). */
function attributeText(attribute: ts.JsxAttribute): string | null {
  const value = attribute.initializer;
  if (value === undefined) return null;
  if (ts.isJsxExpression(value)) return staticText(value.expression);
  return staticText(value);
}

function hasToken(value: string, separators: RegExp): boolean {
  return value
    .split(separators)
    .map((token) => token.trim().toLowerCase().split("=")[0])
    .some((token) => SAFE_TOKENS.includes(token));
}

function findAttribute(element: ts.JsxOpeningLikeElement, name: string): ts.JsxAttribute | undefined {
  for (const property of element.attributes.properties) {
    if (ts.isJsxAttribute(property) && ts.isIdentifier(property.name) && property.name.text === name) {
      return property;
    }
  }
  return undefined;
}

function inspectJsx(element: ts.JsxOpeningLikeElement, source: ts.SourceFile): NewContext | null {
  const target = findAttribute(element, "target");
  if (target === undefined) return null;
  const targetText = attributeText(target);
  if (targetText !== null && SAME_CONTEXT_TARGETS.includes(targetText.trim().toLowerCase())) return null;

  const base = {
    kind: "jsx" as const,
    tag: element.tagName.getText(source),
    line: source.getLineAndCharacterOfPosition(element.getStart(source)).line + 1,
  };
  if (element.attributes.properties.some((property) => ts.isJsxSpreadAttribute(property))) {
    return { ...base, safe: false, reason: "a spread attribute could override rel" };
  }
  const rel = findAttribute(element, "rel");
  if (rel === undefined) return { ...base, safe: false, reason: "no rel" };
  const relText = attributeText(rel);
  if (relText === null) return { ...base, safe: false, reason: "rel is not a plain string, so it cannot be checked" };
  if (!hasToken(relText, /\s+/)) return { ...base, safe: false, reason: `rel="${relText}" has neither noopener nor noreferrer` };
  return { ...base, safe: true, reason: "" };
}

/** `window.open`, `globalThis.open`, `self.open`, and `window["open"]`, as called. */
function isWindowOpen(callee: ts.Expression): boolean {
  if (ts.isPropertyAccessExpression(callee)) {
    return callee.name.text === "open" && ts.isIdentifier(callee.expression) && WINDOW_NAMES.includes(callee.expression.text);
  }
  if (ts.isElementAccessExpression(callee)) {
    return (
      staticText(callee.argumentExpression) === "open" &&
      ts.isIdentifier(callee.expression) &&
      WINDOW_NAMES.includes(callee.expression.text)
    );
  }
  return false;
}

function inspectCall(call: ts.CallExpression, source: ts.SourceFile): NewContext | null {
  if (!isWindowOpen(call.expression)) return null;
  const targetArgument = call.arguments[1];
  if (targetArgument !== undefined) {
    const named = staticText(targetArgument);
    if (named !== null && SAME_CONTEXT_TARGETS.includes(named.trim().toLowerCase())) return null;
  }
  const base = {
    kind: "window.open" as const,
    tag: call.expression.getText(source),
    line: source.getLineAndCharacterOfPosition(call.getStart(source)).line + 1,
  };
  const features = call.arguments[2];
  if (features === undefined) return { ...base, safe: false, reason: "no features argument, so no noopener/noreferrer" };
  const featuresText = staticText(features);
  if (featuresText === null) return { ...base, safe: false, reason: "features is not a plain string, so it cannot be checked" };
  if (!hasToken(featuresText, /[,\s]+/)) {
    return { ...base, safe: false, reason: `features "${featuresText}" has neither noopener nor noreferrer` };
  }
  return { ...base, safe: true, reason: "" };
}

/**
 * Every place in `source` that opens a new browsing context (safe or not), in source order. The
 * one detector both the repo-wide scan and the fixtures call. `fileName` only chooses how the text
 * is parsed: `.tsx` as TSX, anything else as plain TypeScript (where `<T>(x) => x` is a generic,
 * not an element).
 */
function newContexts(fileName: string, source: string): NewContext[] {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
  const found: NewContext[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const result = inspectJsx(node, file);
      if (result !== null) found.push(result);
    } else if (ts.isCallExpression(node)) {
      const result = inspectCall(node, file);
      if (result !== null) found.push(result);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

function unsafe(fileName: string, source: string): string[] {
  return newContexts(fileName, source)
    .filter((context) => !context.safe)
    .map((context) => `${fileName}:${context.line} <${context.tag}> ${context.reason}`);
}

describe('everything in frontend/src that opens a new tab severs the opener (ADR 0028 §Consequences)', () => {
  it("the glob walk actually reaches a known file", () => {
    // Vacuity guard 1: a walk that globs nothing would make "flags nothing" below pass for the
    // wrong reason.
    expect(Object.keys(SOURCE_FILES)).toContain("../main.tsx");
  });

  it("the detector actually sees the two real new-tab anchors this repo ships, and finds both safe", () => {
    // Vacuity guard 2: otherwise "no offenders" below could just as easily mean the detector never
    // recognises an element at all. These are the two anchors, read from the same
    // glob as the real scan.
    for (const key of ["../components/Timeline.tsx", "../pages/ApprovalConsolePage.tsx"]) {
      expect(Object.keys(SOURCE_FILES)).toContain(key);
      const seen = newContexts(key, SOURCE_FILES[key]);
      expect(seen.map((context) => `${context.kind}:${context.tag}:${context.safe}`), key).toEqual(["jsx:a:true"]);
    }
  });

  it("flags nothing in the repo today", () => {
    const offenders = Object.entries(SOURCE_FILES).flatMap(([file, source]) => unsafe(file, source));
    expect(offenders).toEqual([]);
  });

  it("mutation: dropping rel from Timeline.tsx's anchor turns it red", () => {
    // Non-vacuity on the real file, not only on a fixture: plant the mutation into a read copy
    // (never the file on disk).
    const key = "../components/Timeline.tsx";
    const real = SOURCE_FILES[key];
    expect(unsafe(key, real)).toEqual([]);

    const mutated = real.replace(/\s+rel="noreferrer"/, "");
    expect(mutated).not.toBe(real);
    expect(unsafe(key, mutated)).toHaveLength(1);
  });

  it("mutation: dropping rel from the approval console's anchor (which carries an arrow handler) turns it red", () => {
    const key = "../pages/ApprovalConsolePage.tsx";
    const real = SOURCE_FILES[key];
    expect(unsafe(key, real)).toEqual([]);

    const mutated = real.replace(/\s+rel="noreferrer"/, "");
    expect(mutated).not.toBe(real);
    expect(unsafe(key, mutated)).toHaveLength(1);
  });
});

describe("the detector on planted fixtures (strings in this file, never shipped code)", () => {
  const flagged = (source: string, file = "fixture.tsx") => unsafe(file, source);

  it("flags an anchor whose arrow-function handler precedes target, with no rel: the case the earlier regex passed", () => {
    expect(flagged(`const a = <a href={u} onClick={(e) => e.stopPropagation()} target="_blank">x</a>;`)).toHaveLength(1);
  });

  it("flags the arrow-function anchor whichever order the attributes come in", () => {
    expect(flagged(`const a = <a target="_blank" onClick={(e) => e.stopPropagation()}>x</a>;`)).toHaveLength(1);
    expect(flagged(`const a = <a onClick={(e) => { go(e); }} href={u} target="_blank">x</a>;`)).toHaveLength(1);
  });

  it("passes the same arrow-function anchor once it carries a rel, wherever the rel is", () => {
    expect(flagged(`const a = <a onClick={(e) => e.stopPropagation()} target="_blank" rel="noreferrer">x</a>;`)).toEqual([]);
    expect(flagged(`const a = <a target="_blank" onClick={(e) => e.stopPropagation()} rel="noopener">x</a>;`)).toEqual([]);
    expect(flagged(`const a = <a rel="noreferrer" onClick={(e) => e.stopPropagation()} target="_blank">x</a>;`)).toEqual([]);
  });

  it('flags <Link target="_blank"> without a rel, and passes it with one', () => {
    expect(flagged(`const l = <Link to="/x" target="_blank">x</Link>;`)).toHaveLength(1);
    expect(flagged(`const l = <Link to="/x" target="_blank" rel="noopener noreferrer">x</Link>;`)).toEqual([]);
  });

  it("flags any element, self-closing or a component, that has a new-tab target and no rel", () => {
    expect(flagged(`const l = <NavLink to="/x" target="_blank" />;`)).toHaveLength(1);
    expect(flagged(`const l = <ExternalLink href={u} target="_blank" />;`)).toHaveLength(1);
    expect(flagged(`const f = <form action={u} target="_blank"></form>;`)).toHaveLength(1);
  });

  it("flags an anchor nested in a map inside a component", () => {
    const source = `
      export function List({ rows }: { rows: { id: string; url: string }[] }) {
        return <ul>{rows.map((row) => <li key={row.id}><a href={row.url} target="_blank">go</a></li>)}</ul>;
      }`;
    expect(flagged(source)).toHaveLength(1);
  });

  it("flags a named window target, which opens a new window like _blank does", () => {
    expect(flagged(`const a = <a href={u} target="preview">x</a>;`)).toHaveLength(1);
  });

  it("leaves a same-tab target alone", () => {
    expect(flagged(`const a = <a href={u} target="_self">x</a>;`)).toEqual([]);
    expect(flagged(`const a = <a href={u} target={"_top"}>x</a>;`)).toEqual([]);
    expect(flagged(`const a = <a href={u}>x</a>;`)).toEqual([]);
  });

  it("treats a target that is not a literal as a possible new tab, and needs a rel for it", () => {
    expect(flagged(`const a = <a href={u} target={external ? "_blank" : undefined}>x</a>;`)).toHaveLength(1);
    expect(flagged(`const a = <a href={u} target={external ? "_blank" : undefined} rel="noreferrer">x</a>;`)).toEqual([]);
  });

  it("accepts neither a rel without a safe token nor one it cannot read", () => {
    expect(flagged(`const a = <a href={u} target="_blank" rel="nofollow">x</a>;`)).toHaveLength(1);
    expect(flagged(`const a = <a href={u} target="_blank" rel="noopenerx">x</a>;`)).toHaveLength(1);
    expect(flagged(`const a = <a href={u} target="_blank" rel={rel}>x</a>;`)).toHaveLength(1);
    expect(flagged(`const a = <a href={u} target="_blank" rel>x</a>;`)).toHaveLength(1);
  });

  it("reads a rel written as an expression, in any case, among other tokens", () => {
    expect(flagged(`const a = <a href={u} target="_blank" rel={"noreferrer"}>x</a>;`)).toEqual([]);
    expect(flagged("const a = <a href={u} target=\"_blank\" rel={`noopener`}>x</a>;")).toEqual([]);
    expect(flagged(`const a = <a href={u} target="_blank" rel="nofollow NoReferrer">x</a>;`)).toEqual([]);
  });

  it("flags a spread beside a new-tab target, since it can override the rel", () => {
    expect(flagged(`const a = <a target="_blank" rel="noreferrer" {...props}>x</a>;`)).toHaveLength(1);
  });

  it("flags window.open without noopener/noreferrer, in the forms it is called", () => {
    expect(flagged(`open_it(); window.open(url);`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`window.open(url, "_blank");`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`window.open(url, "_blank", "width=400");`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`window.open(url, "_blank", features);`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`globalThis.open(url);`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`self.open(url);`, "fixture.ts")).toHaveLength(1);
    expect(flagged(`window["open"](url);`, "fixture.ts")).toHaveLength(1);
  });

  it("passes window.open with a safe token in its features, and one that stays in the tab", () => {
    expect(flagged(`window.open(url, "_blank", "noopener,noreferrer");`, "fixture.ts")).toEqual([]);
    expect(flagged(`window.open(url, "_blank", "width=400, noopener");`, "fixture.ts")).toEqual([]);
    expect(flagged(`window.open(url, "_self");`, "fixture.ts")).toEqual([]);
  });

  it("ignores .open() on anything that is not the window", () => {
    expect(flagged(`dialog.open(url); xhr.open("GET", url);`, "fixture.ts")).toEqual([]);
  });

  it("reads a .ts file as TypeScript: a generic arrow is not an element", () => {
    expect(flagged(`const id = <T,>(x: T) => x; window.open(url);`, "fixture.ts")).toHaveLength(1);
  });

  it("names the file, the line and the reason", () => {
    const found = unsafe("pages/X.tsx", `const a = 1;\nconst b = <a href={u} target="_blank">x</a>;`);
    expect(found).toEqual(["pages/X.tsx:2 <a> no rel"]);
  });
});

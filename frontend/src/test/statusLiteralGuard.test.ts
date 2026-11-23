/**
 * Every role/ownership/status gate is a named predicate, never an
 * inline comparison in markup ... verify both directions.
 *
 * **The defect this guards.** `ProjectDetailPage` decided what to offer from the timeline with
 * `latestBuild.data.status === "SUCCEEDED"` and `deployments.find((d) => d.status === "ACTIVE")`;
 * `ApprovalConsolePage` with `selected.status === "PENDING"`; `Timeline` with
 * `entry.data.status === "NOT_IMPLEMENTED"`. A rule re-derived from a raw status inside JSX is not
 * reachable by a test (ADR 0021: only named predicates are), and the page's copy of the rule was
 * wrong against the backend's. So a status comparison belongs in a `.ts` module as a named
 * predicate; this guard keeps it out of every `.tsx`.
 *
 * **Direction 1: no inline comparison.** Every non-test `.tsx` under `src/` is parsed with the
 * TypeScript compiler (already a dependency; nothing is executed or type-checked), and a flagged
 * shape is any of
 *   - `x.status === "PENDING"` (any of `===`, `!==`, `==`, `!=`, either order), where one side
 *     NAMES a status (a property, element key or identifier ending in `status`, case-insensitive,
 *     also through `String(...)`) and the other is a status value: a string literal from the
 *     backend's status sets, or an upper-case constant (`BuildStatus.FAILED`, `PENDING_STATUS`),
 *     which is the same defect with a name on it;
 *   - `switch (x.status) { case "ACTIVE": ... }`;
 *   - `["QUEUED", "RUNNING"].includes(x.status)` (and `.indexOf`).
 * Lower-case words (`kind === "build"`, `decision === "approve"`), `status === undefined`, and
 * comparisons of two statuses with each other are not rules and are not flagged.
 *
 * **Direction 2: every predicate is actually called.** A predicate nothing calls is a gate that
 * gates nothing. The named gates are listed below, and each must be called (a call
 * expression, found in the syntax tree, not a mention in a comment) from a `.tsx` file.
 *
 * Both directions use one detector each, called by the repo-wide scan and by the fixtures that
 * prove it can fail, never re-implemented on either side. The fixtures are
 * strings in this file, never shipped code. The status sets below are the backend's
 * (`app/services/status.py`, `models/deployment.py`, `models/approval.py`), spelled out: a status
 * the backend adds is a line here.
 */
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Project, build, deployment and approval status values (a word in two sets is listed once). */
const STATUS_LITERALS = new Set([
  // ProjectStatus
  "DRAFT",
  "SUBMITTED",
  "BUILD_PENDING",
  "PREVIEW_READY",
  "AWAITING_APPROVAL",
  "ACTIVE",
  "ARCHIVED",
  // BuildStatus
  "NOT_IMPLEMENTED",
  "QUEUED",
  "RUNNING",
  "FAILED",
  "SUCCEEDED",
  // DeploymentStatus
  "DEPLOYING",
  "SUPERSEDED",
  "STOPPED",
  // ApprovalStatus
  "PENDING",
  "APPROVED",
  "REJECTED",
  "AUTO_APPROVED",
]);

const COMPARISON_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsEqualsToken,
  ts.SyntaxKind.EqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken,
]);

interface Found {
  line: number;
  text: string;
}

function strip(node: ts.Expression): ts.Expression {
  let current = node;
  while (
    ts.isParenthesizedExpression(current) ||
    ts.isAsExpression(current) ||
    ts.isNonNullExpression(current) ||
    ts.isSatisfiesExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}

function stringValue(node: ts.Expression): string | null {
  const inner = strip(node);
  if (ts.isStringLiteral(inner) || ts.isNoSubstitutionTemplateLiteral(inner)) return inner.text;
  return null;
}

/** The last name an expression ends in: `a.b.status` -> `status`, `x["status"]` -> `status`. */
function lastName(node: ts.Expression): string | null {
  const inner = strip(node);
  if (ts.isIdentifier(inner)) return inner.text;
  if (ts.isPropertyAccessExpression(inner)) return inner.name.text;
  if (ts.isElementAccessExpression(inner)) return stringValue(inner.argumentExpression);
  return null;
}

/** An expression that reads a status: `x.status`, `status`, `String(x.status)`, `x["status"]`. */
function namesStatus(node: ts.Expression): boolean {
  const inner = strip(node);
  if (
    ts.isCallExpression(inner) &&
    ts.isIdentifier(inner.expression) &&
    inner.expression.text === "String" &&
    inner.arguments.length === 1
  ) {
    return namesStatus(inner.arguments[0]);
  }
  const name = lastName(inner);
  return name !== null && /status$/i.test(name);
}

/** An expression that is a status VALUE: a status literal, or an upper-case constant. */
function isStatusValue(node: ts.Expression): boolean {
  const literal = stringValue(node);
  if (literal !== null) return STATUS_LITERALS.has(literal);
  const name = lastName(node);
  return name !== null && /^[A-Z][A-Z0-9_]*$/.test(name);
}

function comparesStatus(left: ts.Expression, right: ts.Expression): boolean {
  return (namesStatus(left) && isStatusValue(right)) || (namesStatus(right) && isStatusValue(left));
}

/** Every status comparison in `source`, in source order. `fileName` only chooses TSX or TS parsing. */
function statusComparisons(fileName: string, source: string): Found[] {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
  const found: Found[] = [];
  const note = (node: ts.Node) =>
    found.push({
      line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1,
      text: node.getText(file).replace(/\s+/g, " "),
    });

  const visit = (node: ts.Node) => {
    if (ts.isBinaryExpression(node) && COMPARISON_OPERATORS.has(node.operatorToken.kind)) {
      if (comparesStatus(node.left, node.right)) note(node);
    } else if (ts.isSwitchStatement(node) && namesStatus(node.expression)) {
      for (const clause of node.caseBlock.clauses) {
        if (ts.isCaseClause(clause) && isStatusValue(clause.expression)) note(clause);
      }
    } else if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text;
      const receiver = strip(node.expression.expression);
      if (
        (method === "includes" || method === "indexOf") &&
        ts.isArrayLiteralExpression(receiver) &&
        receiver.elements.some((element) => isStatusValue(element)) &&
        node.arguments.length > 0 &&
        namesStatus(node.arguments[0])
      ) {
        note(node);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

const isTest = (path: string) => /\.test\.tsx?$/.test(path);
const MARKUP_FILES = Object.entries(SOURCE_FILES).filter(([path]) => path.endsWith(".tsx") && !isTest(path));

function offenders(fileName: string, source: string): string[] {
  return statusComparisons(fileName, source).map((hit) => `${fileName}:${hit.line} ${hit.text}`);
}

describe("no .tsx file compares a status inline (ADR 0021)", () => {
  it("the glob walk reaches the pages this guard exists for", () => {
    // Vacuity guard 1: a walk that finds nothing would make "flags nothing" below pass for the wrong reason.
    const paths = MARKUP_FILES.map(([path]) => path);
    expect(paths).toContain("../pages/ProjectDetailPage.tsx");
    expect(paths).toContain("../pages/ApprovalConsolePage.tsx");
    expect(paths).toContain("../components/Timeline.tsx");
  });

  it("flags nothing in the repo today", () => {
    const found = MARKUP_FILES.flatMap(([path, source]) => offenders(path, source));
    expect(found).toEqual([]);
  });

  it("mutation on the real files: putting any of the old comparisons back turns the scan red", () => {
    // Vacuity guard 2, on real files and not only on fixtures: plant the mutation into a read copy
    // (never the file on disk). Each replaced text is a call the real file makes today.
    const cases: Array<[string, string, string]> = [
      ["../pages/ApprovalConsolePage.tsx", "isApprovalPending(a.status)", 'a.status === "PENDING"'],
      ["../components/Timeline.tsx", "isBuildNotImplemented(entry.data.status)", 'entry.data.status === "NOT_IMPLEMENTED"'],
      ["../components/Timeline.tsx", "isApprovalPending(status)", 'status === "PENDING"'],
      // the un-archive gate, which a page could just as well write as an inline comparison.
      [
        "../pages/ProjectDetailPage.tsx",
        "canUnarchiveProject(currentUser, page.data?.project ?? null)",
        'page.data?.project.status === "ARCHIVED"',
      ],
    ];
    for (const [key, original, planted] of cases) {
      const real = SOURCE_FILES[key];
      expect(offenders(key, real), key).toEqual([]);
      expect(real, `${key} no longer contains ${original}`).toContain(original);
      expect(offenders(key, real.replace(original, planted)), `${key}: ${planted}`).toHaveLength(1);
    }
  });
});

describe("the status detector on planted fixtures (strings in this file, never shipped code)", () => {
  const flagged = (source: string, file = "fixture.tsx") => offenders(file, source);

  it("flags the comparisons the page and the console used to make", () => {
    expect(flagged(`const ok = latestBuild.data.status === "SUCCEEDED" && latestBuild.data.deploy_eligible === true;`)).toHaveLength(1);
    expect(flagged(`const active = deployments.find((d) => d.status === "ACTIVE") ?? null;`)).toHaveLength(1);
    expect(flagged(`const pending = approvals.filter((a) => a.status === "PENDING");`)).toHaveLength(1);
    expect(flagged(`const decided = approvals.filter((a) => a.status !== "PENDING");`)).toHaveLength(1);
    expect(flagged(`const a = <p>{entry.data.status === "NOT_IMPLEMENTED" && <i />}</p>;`)).toHaveLength(1);
    expect(flagged(`const a = <p>{selected.status === "PENDING" && canDecide && <b />}</p>;`)).toHaveLength(1);
  });

  it("flags every comparison operator and either order", () => {
    expect(flagged(`const a = x.status == "ACTIVE";`)).toHaveLength(1);
    expect(flagged(`const a = x.status != "ACTIVE";`)).toHaveLength(1);
    expect(flagged(`const a = "ACTIVE" === x.status;`)).toHaveLength(1);
    expect(flagged(`const a = "ACTIVE" !== x.status;`)).toHaveLength(1);
  });

  it("flags a status read through an identifier, a key, String(), parentheses or a cast", () => {
    expect(flagged(`const a = status === "PENDING";`)).toHaveLength(1);
    expect(flagged(`const a = buildStatus === "FAILED";`)).toHaveLength(1);
    expect(flagged(`const a = x["status"] === "FAILED";`)).toHaveLength(1);
    expect(flagged(`const a = String(entry.data.status) === "NOT_IMPLEMENTED";`)).toHaveLength(1);
    expect(flagged(`const a = (x.status as string) === "RUNNING";`)).toHaveLength(1);
    expect(flagged(`const a = (x.status!) === "RUNNING";`)).toHaveLength(1);
  });

  it("flags a comparison with a named constant: the same defect with a name on it", () => {
    expect(flagged(`const a = x.status === BuildStatus.FAILED;`)).toHaveLength(1);
    expect(flagged(`const a = x.status === PENDING_APPROVAL_STATUS;`)).toHaveLength(1);
  });

  it("flags a switch on a status, and an array of statuses it is tested against", () => {
    expect(flagged(`switch (d.status) { case "ACTIVE": return 1; default: return 0; }`)).toHaveLength(1);
    expect(flagged(`const busy = ["QUEUED", "RUNNING"].includes(b.status);`)).toHaveLength(1);
    expect(flagged(`const busy = ["QUEUED", "RUNNING"].indexOf(b.status) !== -1;`)).toHaveLength(1);
  });

  it("flags each of several comparisons in one expression", () => {
    expect(flagged(`const a = x.status === "ACTIVE" || x.status === "DEPLOYING";`)).toHaveLength(2);
  });

  it("does not flag what is not a status rule", () => {
    expect(flagged(`const a = entry.kind === "build";`)).toEqual([]);
    expect(flagged(`const a = d.decision === "approve";`)).toEqual([]);
    expect(flagged(`const a = x.status === undefined || x.status === null;`)).toEqual([]);
    expect(flagged(`const a = x.status === y.status;`)).toEqual([]);
    expect(flagged(`const a = mode === "local";`)).toEqual([]);
    expect(flagged(`const a = x.status === "ok";`)).toEqual([]);
    expect(flagged(`const a = x.name === "PENDING";`)).toEqual([]);
    expect(flagged(`const a = isApprovalPending(x.status) && !isBuildNotImplemented(y.status);`)).toEqual([]);
    expect(flagged(`switch (kind) { case "ACTIVE": break; }`)).toEqual([]);
    expect(flagged(`const a = ["a", "b"].includes(x.status);`)).toEqual([]);
  });

  it("reads a .ts file as TypeScript: a generic arrow is not an element", () => {
    expect(flagged(`const id = <T,>(x: T) => x; const a = x.status === "ACTIVE";`, "fixture.ts")).toHaveLength(1);
  });

  it("names the file, the line and the text", () => {
    expect(offenders("pages/X.tsx", `const a = 1;\nconst b = x.status === "ACTIVE";`)).toEqual([
      'pages/X.tsx:2 x.status === "ACTIVE"',
    ]);
  });
});

/** The named gates: each must be called from markup, or it gates nothing. */
const NAMED_GATES = [
  // pages/deployControls.ts
  "canOfferConfirm",
  "canOfferRollback",
  "canOfferStop",
  "confirmTargetBuildId",
  "confirmBlockedMessage",
  "offersAnyDeployControl",
  "shouldPollProject",
  "shouldRefreshAfterDeployFailure",
  // the page calls the wrappers; `deployActionFailureMessage` is reached through the first
  "deployActionFailure",
  "deployFailureAfter",
  // auth/roles.ts: whether the approval console may be opened -- the nav entry (via
  // shellNav.ts) and the page refusal (ApprovalConsolePage.tsx) both read it
  "canOpenApprovalConsole",
  // auth/approvalGate.ts and pages/approvalMessages.ts
  "canApproveRequest",
  "canRejectRequest",
  "approveBlock",
  "rejectBlock",
  "decisionBlockMessage",
  "isApprovalPending",
  "approvalLoadFailureMessage",
  // pages/approvalConsoleState.ts: `approvalDecisionFailureMessage` and
  // `shouldRereadAfterDecisionFailure` are reached through `decisionRefusalFollowUp`
  "decisionRefusalFollowUp",
  "decisionStillChosen",
  "consoleView",
  "decisionLabel",
  // components/buildStatus.ts, pages/AdminConsolePage.tsx
  "isBuildNotImplemented",
  "isTabVisible",
  // pages/unarchive.ts: the archived-and-owner-or-admin gate, and what a refusal says/does
  "canUnarchiveProject",
  "unarchiveFailureMessage",
  "shouldRefreshAfterUnarchiveFailure",
  "shouldDismissConfirmation",
  // which failure alert survives the control going away
  "failureAfterControlWithdrawn",
  // pages/serviceLink.ts: whether the page offers the service link
  "serviceLinkHref",
];

/** Whether `source` contains a call of a plain identifier `name(...)`: a call expression in the
 * syntax tree, so a mention in a comment or a string does not count. */
function callsFunction(fileName: string, source: string, name: string): boolean {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
  let called = false;
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name) {
      called = true;
    }
    if (!called) ts.forEachChild(node, visit);
  };
  visit(file);
  return called;
}

describe("every named gate is actually called from a .tsx file (ADR 0021)", () => {
  it.each(NAMED_GATES)("%s", (name) => {
    const callers = MARKUP_FILES.filter(([path, source]) => callsFunction(path, source, name)).map(([path]) => path);

    expect(callers, `${name} is called from no .tsx file`).not.toEqual([]);
  });

  it("the call detector sees a real call, and does not see a comment, a string or a bare mention", () => {
    expect(callsFunction("a.tsx", `const a = canApproveRequest(u, r);`, "canApproveRequest")).toBe(true);
    expect(callsFunction("a.tsx", `const a = <b>{canApproveRequest(u, r) && <i />}</b>;`, "canApproveRequest")).toBe(true);
    expect(callsFunction("a.tsx", `// canApproveRequest(u, r)\nconst a = 1;`, "canApproveRequest")).toBe(false);
    expect(callsFunction("a.tsx", `const a = "canApproveRequest(u, r)";`, "canApproveRequest")).toBe(false);
    expect(callsFunction("a.tsx", `import { canApproveRequest } from "x"; const f = canApproveRequest;`, "canApproveRequest")).toBe(false);
    expect(callsFunction("a.tsx", `const a = obj.canApproveRequest(u, r);`, "canApproveRequest")).toBe(false);
  });

  it("mutation on the real file: if the console stops calling canApproveRequest, the check goes red", () => {
    const key = "../pages/ApprovalConsolePage.tsx";
    const real = SOURCE_FILES[key];
    expect(callsFunction(key, real, "canApproveRequest")).toBe(true);

    const mutated = real.replace("canApproveRequest(currentUser, selected)", "true");
    expect(mutated).not.toBe(real);
    expect(callsFunction(key, mutated, "canApproveRequest")).toBe(false);
  });
});

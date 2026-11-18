/**
 * No `api/*.ts` module may put an id in a request path without `pathSegment`.
 *
 * Two checks, because each one alone misses what the other catches:
 *
 * 1. **Behaviour.** Every wrapper that takes an id is called with a hostile one (`/`, `?`, `#`
 *    and `..`) and the URL that reaches `fetch` is compared with a literal. This is what a user's
 *    request actually is, but it only covers the wrappers listed here.
 * 2. **Shape.** Every source file under `api/` is parsed, and every `${...}` inside a template
 *    literal -- the shape the defect had: `` `/projects/${id}` `` -- must be a call to
 *    `pathSegment`, `encodeURIComponent` or one of the named query builders, or a constant path.
 *    This is what catches the next module (or the next function in an old one) that interpolates a
 *    raw id, which a list of wrappers cannot.
 *
 * The scan is an AST walk, not a regex: a regex over source text reads comments and strings, and
 * the comments in these modules quote paths with `${...}` in them.
 */
import ts from "typescript";
import { afterEach, describe, expect, it, vi } from "vitest";

import { agentEngineApi } from "./agentEngine";
import { approvalsApi } from "./approvals";
import { buildsApi } from "./builds";
import { deploymentsApi } from "./deployments";
import { evalsApi } from "./evals";
import { localAccountsApi } from "./localAccounts";
import { projectsApi } from "./projects";
import { templatesApi } from "./templates";
import { usageApi } from "./usage";
import { usersApi } from "./users";

afterEach(() => {
  vi.unstubAllGlobals();
});

// Contains a slash, a question mark, a hash, and a `..` between slashes.
const HOSTILE = "a/b?c#d/../e";
const HOSTILE_ENCODED = "a%2Fb%3Fc%23d%2F..%2Fe";

function captureUrl(): { urls: string[] } {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      return { ok: true, status: 200, statusText: "OK", json: async () => ({}) } as unknown as Response;
    }),
  );
  return { urls };
}

const CASES: Array<[string, (id: string) => Promise<unknown>, string]> = [
  ["projectsApi.get", (id) => projectsApi.get(id), `/api/projects/${HOSTILE_ENCODED}`],
  ["projectsApi.timeline", (id) => projectsApi.timeline(id), `/api/projects/${HOSTILE_ENCODED}/timeline`],
  [
    "projectsApi.submitChangeRequest",
    (id) => projectsApi.submitChangeRequest(id, { nl_request: "x" }),
    `/api/projects/${HOSTILE_ENCODED}/change-requests`,
  ],
  ["projectsApi.unarchive", (id) => projectsApi.unarchive(id), `/api/projects/${HOSTILE_ENCODED}/unarchive`],
  ["approvalsApi.get", (id) => approvalsApi.get(id), `/api/approvals/${HOSTILE_ENCODED}`],
  ["approvalsApi.approve", (id) => approvalsApi.approve(id), `/api/approvals/${HOSTILE_ENCODED}/approve`],
  [
    "approvalsApi.reject",
    (id) => approvalsApi.reject(id, { comment: "no" }),
    `/api/approvals/${HOSTILE_ENCODED}/reject`,
  ],
  ["deploymentsApi.list", (id) => deploymentsApi.list(id), `/api/projects/${HOSTILE_ENCODED}/deployments`],
  [
    "deploymentsApi.confirm",
    (id) => deploymentsApi.confirm(id, "b1"),
    `/api/projects/${HOSTILE_ENCODED}/deployments`,
  ],
  [
    "deploymentsApi.rollback",
    (id) => deploymentsApi.rollback(id),
    `/api/projects/${HOSTILE_ENCODED}/deployments/rollback`,
  ],
  ["deploymentsApi.stop", (id) => deploymentsApi.stop(id), `/api/projects/${HOSTILE_ENCODED}/deployments/stop`],
  [
    "usersApi.changeRole",
    (id) => usersApi.changeRole(id, { role: "approver" }),
    `/api/users/${HOSTILE_ENCODED}/role`,
  ],
  [
    "usageApi.setUserQuota",
    (id) => usageApi.setUserQuota(id, { monthly_quota_usd: 1 }),
    `/api/usage/quotas/users/${HOSTILE_ENCODED}`,
  ],
  [
    "usageApi.setProjectQuota",
    (id) => usageApi.setProjectQuota(id, { monthly_quota_usd: 1 }),
    `/api/usage/quotas/projects/${HOSTILE_ENCODED}`,
  ],
  [
    "localAccountsApi.approveSignup",
    (id) => localAccountsApi.approveSignup(id),
    `/api/admin/signup-requests/${HOSTILE_ENCODED}/approve`,
  ],
  [
    "localAccountsApi.rejectSignup",
    (id) => localAccountsApi.rejectSignup(id),
    `/api/admin/signup-requests/${HOSTILE_ENCODED}/reject`,
  ],
  [
    "localAccountsApi.reopenSignup",
    (id) => localAccountsApi.reopenSignup(id),
    `/api/admin/signup-requests/${HOSTILE_ENCODED}/reopen`,
  ],
  [
    "localAccountsApi.deactivateUser",
    (id) => localAccountsApi.deactivateUser(id),
    `/api/admin/users/${HOSTILE_ENCODED}/deactivate`,
  ],
  ["templatesApi.get", (id) => templatesApi.get(id), `/api/templates/${HOSTILE_ENCODED}`],
  ["buildsApi.trace", (id) => buildsApi.trace(id), `/api/builds/${HOSTILE_ENCODED}/trace`],
  ["evalsApi.getRun", (id) => evalsApi.getRun(id), `/api/evals/runs/${HOSTILE_ENCODED}`],
];

describe("every wrapper that takes an id encodes it (behaviour)", () => {
  it.each(CASES)("%s", async (_name, call, expected) => {
    const { urls } = captureUrl();

    await call(HOSTILE);

    expect(urls).toEqual([expected]);
  });

  it("refuses an id that no encoding can make safe, before any request is sent", async () => {
    const { urls } = captureUrl();

    for (const [, call] of CASES) {
      for (const bad of ["..", ".", ""]) {
        // Thrown synchronously or rejected: either way no request was made.
        await expect(Promise.resolve().then(() => call(bad))).rejects.toThrow(RangeError);
      }
    }
    expect(urls).toEqual([]);
  });

  it("leaves the wrappers that take no id on their fixed paths", async () => {
    const { urls } = captureUrl();

    await agentEngineApi.get();
    await projectsApi.list();
    await usersApi.list();
    await evalsApi.listRuns();

    expect(urls).toEqual(["/api/platform/agent-engine", "/api/projects", "/api/users", "/api/evals/runs"]);
  });
});

// ---------------------------------------------------------------------------------------------
// The shape scan
// ---------------------------------------------------------------------------------------------

const SOURCES = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<
  string,
  string
>;

/** Calls whose result is already escaped: the helper, the platform function, and the query
 * builders that `encodeURIComponent` every value they place (each pinned in its own test). */
const ESCAPING_CALLS = new Set([
  "pathSegment",
  "encodeURIComponent",
  "monthQuery",
  "dashboardQuery",
  "signupStatusQuery",
]);

/** Modules exempt from the scan, each for a stated reason. `client.ts` is the seam: it receives a
 * finished path and prefixes the base URL. `requestLimits.ts` builds Korean messages from counts;
 * it makes no request. A new module is scanned by default. */
const EXEMPT = new Set(["./client.ts", "./requestLimits.ts"]);

function isTestFile(name: string): boolean {
  return name.endsWith(".test.ts");
}

function isAllowedSpan(expression: ts.Expression, file: ts.SourceFile): boolean {
  if (ts.isCallExpression(expression) && ts.isIdentifier(expression.expression)) {
    return ESCAPING_CALLS.has(expression.expression.text);
  }
  // A module constant that names a fixed path (`EVAL_RUNS_PATH`): UPPER_SNAKE, never a parameter.
  if (ts.isIdentifier(expression)) return /^[A-Z][A-Z0-9_]*$/.test(expression.text);
  // `dashboardQuery` joins values it has already encoded.
  return expression.getText(file) === 'params.join("&")';
}

/** Every `${...}` in a template literal that is not an escaping call or a constant path, and every
 * string built with `+`. The one detector both the repo scan and the seeded cases below call. */
function rawInterpolations(fileName: string, source: string): string[] {
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const found: string[] = [];

  function visit(node: ts.Node): void {
    if (ts.isTemplateExpression(node)) {
      for (const span of node.templateSpans) {
        if (!isAllowedSpan(span.expression, file)) {
          found.push(`${fileName}: \${${span.expression.getText(file)}}`);
        }
      }
    }
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.PlusToken &&
      [node.left, node.right].some(
        (side) => ts.isStringLiteral(side) || ts.isNoSubstitutionTemplateLiteral(side) || ts.isTemplateExpression(side),
      )
    ) {
      found.push(`${fileName}: string built with +: ${node.getText(file)}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  return found;
}

describe("no api module interpolates a raw id into a path (shape)", () => {
  it("scans the modules that build paths, so an empty glob cannot pass", () => {
    const scanned = Object.keys(SOURCES).filter((name) => !isTestFile(name) && !EXEMPT.has(name));

    for (const expected of [
      "./projects.ts",
      "./approvals.ts",
      "./deployments.ts",
      "./users.ts",
      "./usage.ts",
      "./localAccounts.ts",
      "./builds.ts",
      "./evals.ts",
      "./templates.ts",
      "./auth.ts",
    ]) {
      expect(scanned, expected).toContain(expected);
    }
  });

  it("finds nothing in any scanned module", () => {
    const found = Object.entries(SOURCES)
      .filter(([name]) => !isTestFile(name) && !EXEMPT.has(name))
      .flatMap(([name, source]) => rawInterpolations(name, source));

    expect(found).toEqual([]);
  });

  // The detector itself, on the shapes it exists for. Without these the scan above could pass by
  // flagging nothing at all.
  it("flags the defect's own shape: a raw id inside a path template", () => {
    expect(rawInterpolations("x.ts", "const f = (id: string) => `/projects/${id}`;")).toHaveLength(1);
    expect(rawInterpolations("x.ts", "const f = (id: string) => `/projects/${id}/timeline`;")).toHaveLength(1);
    expect(rawInterpolations("x.ts", "const f = (a: string, b: string) => `/x/${a}/y/${b}`;")).toHaveLength(2);
  });

  it("flags a raw value that merely looks harmless: a property, a call that does not escape", () => {
    expect(rawInterpolations("x.ts", "const f = (p: { id: string }) => `/projects/${p.id}`;")).toHaveLength(1);
    expect(rawInterpolations("x.ts", "const f = (id: string) => `/projects/${String(id)}`;")).toHaveLength(1);
    expect(rawInterpolations("x.ts", "const f = (id: string) => `/projects/${id.trim()}`;")).toHaveLength(1);
  });

  it("flags a path built with +", () => {
    expect(rawInterpolations("x.ts", 'const f = (id: string) => "/projects/" + id;')).toHaveLength(1);
  });

  it("passes the wrapped forms", () => {
    const wrapped = [
      "const f = (id: string) => `/projects/${pathSegment(id)}`;",
      "const f = (s: string) => `/approvals?status=${encodeURIComponent(s)}`;",
      "const f = (m: string) => `/usage/summary${monthQuery(m)}`;",
      "const f = (id: string) => `${EVAL_RUNS_PATH}/${pathSegment(id)}`;",
    ];
    for (const source of wrapped) {
      expect(rawInterpolations("x.ts", source), source).toEqual([]);
    }
  });

  it("does not read comments or plain strings", () => {
    const source = [
      "// GET /projects/${id} is quoted here",
      "/** `/builds/${buildId}/trace` */",
      'const f = () => "/projects/${id}";',
    ].join("\n");

    expect(rawInterpolations("x.ts", source)).toEqual([]);
  });
});

/**
 * a string shown to someone other than
 * its author is shown through `visibleControls`, so that a name made of `report<U+202E>txt.py`
 * reads as what it is. The rule was applied to the files at hand and the approval
 * console and project page were not among them; nothing would have said so when the next screen
 * was added. This is that something.
 *
 * **The shape of the defect.** A field that a person typed (a project name, a user's name or email,
 * a department, a decision comment) is read off an API object by property access and put on screen
 * as it came. So the guard asks the syntax tree (the compiler's parser, as `targetBlankRel.test.ts`
 * does; nothing is executed) for every property access whose name has that shape (`name`, `email`,
 * `department`, `comment`, or anything ending in `_name`, which is how the API spells a joined
 * name: `project_name`, `requester_name`, `owner_name`) in non-test source, and requires an
 * enclosing `visibleControls(...)` / `visibleControlsNode(...)` call. A word list of files or of
 * call sites would pass a new screen; this fails it.
 *
 * **What it does not see**, so a clean run is not read as more: a name destructured before use
 * (`const { name } = project`), read by `project["name"]`, or passed on as a whole object; a field
 * the API spells some other way (`label`, `title`); and a wrapper that sits outside the expression
 * that reads the field but still covers it (it reports that, and the allowlist or a move of the
 * wrap resolves it). Strings it never looks at are the ones the platform wrote.
 *
 * Same shape as `jsdomEnvironmentPragma.test.ts`: one detector, called by the repo-wide scan and by
 * the fixtures that prove it can fail.
 */
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const AUTHORED_FIELD = /^(name|email|department|comment|[a-z_]+_name)$/;
const WRAPPERS = ["visibleControls", "visibleControlsNode"];

function isTestFile(file: string): boolean {
  return /\.test\.tsx?$/.test(file) || file.startsWith("../test/");
}

/** Every `x.field` of the authored shape that no enclosing `visibleControls(...)` covers, as
 * `line: expression`. */
export function unwrappedAuthoredFields(source: string, fileName = "file.tsx"): string[] {
  const tree = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: string[] = [];

  function covered(node: ts.Node): boolean {
    for (let up = node.parent; up !== undefined; up = up.parent) {
      if (ts.isCallExpression(up) && ts.isIdentifier(up.expression) && WRAPPERS.includes(up.expression.text)) {
        return true;
      }
    }
    return false;
  }

  /** A read whose value is only tested, never put on screen: `x.name === null`, `x.name && ...`,
   * `x.name ? ... : ...`, `!x.name`. */
  function onlyTested(node: ts.Node): boolean {
    const parent = node.parent;
    if (ts.isBinaryExpression(parent)) {
      const op = parent.operatorToken.kind;
      const comparison = [
        ts.SyntaxKind.EqualsEqualsToken,
        ts.SyntaxKind.EqualsEqualsEqualsToken,
        ts.SyntaxKind.ExclamationEqualsToken,
        ts.SyntaxKind.ExclamationEqualsEqualsToken,
      ].includes(op);
      return comparison || (op === ts.SyntaxKind.AmpersandAmpersandToken && parent.left === node);
    }
    if (ts.isConditionalExpression(parent)) return parent.condition === node;
    return ts.isPrefixUnaryExpression(parent) && parent.operator === ts.SyntaxKind.ExclamationToken;
  }

  function visit(node: ts.Node): void {
    if (
      ts.isPropertyAccessExpression(node) &&
      AUTHORED_FIELD.test(node.name.text) &&
      !onlyTested(node) &&
      !covered(node)
    ) {
      const { line } = tree.getLineAndCharacterOfPosition(node.getStart(tree));
      found.push(`${line + 1}: ${node.getText(tree)}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  return found;
}

/**
 * Reads of an authored-shaped field that are not a string some other user typed. Keyed by file, then
 * by the expression as written; each states why, so an entry is a decision and not a silencer.
 */
const NOT_SHOWN_TO_ANOTHER_AUTHOR: Record<string, Record<string, string>> = {
  "../components/MeasuredFacts.tsx": {
    "check.check_name": "a policy check's name: one of the platform's own checks",
  },
  "../components/Timeline.tsx": {
    "c.check_name": "a policy check's name: one of the platform's own checks",
  },
  "../components/approvalDetailView.ts": {
    "check.check_name": "a policy check's name: one of the platform's own checks, handed on to policyCheckLabels (never put on screen as it came)",
  },
  "../components/buildTraceView.ts": {
    "block.name": "read into `rawName`, which goes through visibleControls on the next lines",
  },
  "../pages/ApprovalConsolePage.tsx": {
    "approvals.find((a) => a.id === decided.id)?.project_name":
      "handed to decisionRefusalFollowUp, which wraps it into the message (approvalConsoleState.test.ts)",
  },
  "../pages/SignupRequestPage.tsx": {
    "values.email": "the form's own input: the signer-up sees what they are typing",
    "values.name": "the form's own input: the signer-up sees what they are typing",
    "values.department": "the form's own input: the signer-up sees what they are typing",
    "fieldErrors.email": "the platform's validation sentence for that field",
    "fieldErrors.name": "the platform's validation sentence for that field",
    "fieldErrors.department": "the platform's validation sentence for that field",
  },
  "../pages/signup.ts": {
    "values.email": "the form's own input, validated here",
    "values.name": "the form's own input, validated here",
    "values.department": "the form's own input, validated here",
    "errors.email": "the platform's validation sentence for that field",
    "errors.name": "the platform's validation sentence for that field",
    "errors.department": "the platform's validation sentence for that field",
  },
  "../pages/admin/deploymentHealth.ts": {
    "a.project_name": "a sort key: compared, never rendered",
    "b.project_name": "a sort key: compared, never rendered",
  },
  "../components/Layout.tsx": {
    "currentUser.name": "the header shows the signed-in user their own name",
  },
  "../components/BuildTracePanel.tsx": {
    "block.name": "rendered inside <Code>, which runs its children through visibleControlsNode",
  },
  "../pages/RequestIntakePage.tsx": {
    "t.name": "a template's or a team's name: both are seeded by the platform, no route lets a user create either",
  },
  "../pages/admin/TemplatesPanel.tsx": {
    "template.name": "a template's name: seeded by the platform, no route lets a user create one",
  },
};

function allowed(file: string, expression: string): boolean {
  return Object.prototype.hasOwnProperty.call(NOT_SHOWN_TO_ANOTHER_AUTHOR[file] ?? {}, expression);
}

describe("every user-authored string read off an API object is shown through visibleControls", () => {
  const files = Object.keys(SOURCE_FILES).filter((f) => !isTestFile(f));

  it("scans the screens it is about (not an empty glob)", () => {
    for (const must of [
      "../pages/ApprovalConsolePage.tsx",
      "../pages/ProjectDetailPage.tsx",
      "../pages/ProjectRegistryPage.tsx",
      "../pages/admin/RolesPanel.tsx",
      "../pages/admin/SignupRequestsPanel.tsx",
      "../pages/admin/UsagePanel.tsx",
      "../pages/admin/DeploymentHealthPanel.tsx",
      "../pages/admin/CostDashboardSection.tsx",
    ]) {
      expect(files, must).toContain(must);
    }
  });

  it("finds none that is bare, apart from the listed ones", () => {
    const bare: Record<string, string[]> = {};
    for (const file of files) {
      const hits = unwrappedAuthoredFields(SOURCE_FILES[file], file).filter(
        (hit) => !allowed(file, hit.slice(hit.indexOf(": ") + 2)),
      );
      if (hits.length > 0) bare[file] = hits;
    }
    expect(bare).toEqual({});
  });

  it("every allowlist entry is still needed (an entry for a read that is gone is a stale excuse)", () => {
    for (const [file, entries] of Object.entries(NOT_SHOWN_TO_ANOTHER_AUTHOR)) {
      expect(Object.keys(SOURCE_FILES), file).toContain(file);
      const expressions = unwrappedAuthoredFields(SOURCE_FILES[file], file).map((hit) => hit.slice(hit.indexOf(": ") + 2));
      for (const expression of Object.keys(entries)) {
        expect(expressions, `${file}: ${expression}`).toContain(expression);
      }
    }
  });
});

describe("the detector", () => {
  it("passes a wrapped field, in a child, an attribute, a template and an argument", () => {
    const source = [
      "const a = <b aria-label={`${visibleControls(row.name)} x`}>{visibleControls(p.project_name)}</b>;",
      "const c = visibleControls(`${u.name} (${u.email})`);",
      "const d = <i>{visibleControlsNode(x.comment)}</i>;",
    ].join("\n");

    expect(unwrappedAuthoredFields(source)).toEqual([]);
  });

  it("reports a bare field, whatever its position", () => {
    expect(unwrappedAuthoredFields("const a = <b>{p.project_name}</b>;")).toEqual(["1: p.project_name"]);
    expect(unwrappedAuthoredFields("const a = `${u.name}의 역할`;")).toEqual(["1: u.name"]);
    expect(unwrappedAuthoredFields("const a = <b aria-label={`${a.requester_name} 상세`} />;")).toEqual([
      "1: a.requester_name",
    ]);
    expect(unwrappedAuthoredFields("const a = <b>{d.comment}</b>;\nconst e = u.email;")).toEqual([
      "1: d.comment",
      "2: u.email",
    ]);
  });

  it("a field handed to some other function is still bare: only the wrapper covers it", () => {
    expect(unwrappedAuthoredFields("const a = roleApplyLabel(user.name, false);")).toEqual(["1: user.name"]);
  });

  it("is not triggered by a property that merely contains the word", () => {
    expect(unwrappedAuthoredFields("const a = x.nickname + x.filename + x.name_length + x.names;")).toEqual([]);
  });

  it("lets a read that is only tested pass, and reports one that is rendered through the same operator", () => {
    expect(unwrappedAuthoredFields("const a = d.comment && visibleControls(d.comment);")).toEqual([]);
    expect(unwrappedAuthoredFields("const a = r.department === null ? '-' : visibleControls(r.department);")).toEqual([]);
    expect(unwrappedAuthoredFields("const a = s.set_by_name ? visibleControls(s.set_by_name) : '-';")).toEqual([]);
    expect(unwrappedAuthoredFields("const a = !u.name;")).toEqual([]);
    // `??` and `||` hand the value itself on, so they are not a test of it.
    expect(unwrappedAuthoredFields("const a = r.department ?? '-';")).toEqual(["1: r.department"]);
    expect(unwrappedAuthoredFields("const a = r.department || '-';")).toEqual(["1: r.department"]);
    expect(unwrappedAuthoredFields("const a = ok && d.comment;")).toEqual(["1: d.comment"]);
  });

  it("a different wrapper is not the wrapper", () => {
    expect(unwrappedAuthoredFields("const a = escape(p.name);")).toEqual(["1: p.name"]);
  });
});

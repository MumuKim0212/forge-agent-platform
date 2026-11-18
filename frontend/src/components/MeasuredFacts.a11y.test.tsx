// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment: the approval detail's new sections. This file owns only
 * what an a11y test may own here, not copy or layout (the wording is pinned in
 * `measuredFactsLines.test.ts`):
 *   - none of the sections is a live region (a live region is an intent, not a tone);
 *   - an agent-chosen path is rendered as TEXT, never markup: no anchor, no injected element,
 *     the string intact; the request text is plain text too.
 * The text assertions are there so the absence checks cannot pass vacuously.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  AgentSummary,
  ChangedFilesSection,
  PolicyChecksSection,
  RepairAttemptsNote,
  RequestTextSection,
  SmokeChecksSection,
} from "./MeasuredFacts";
import type { ChangedFiles, PolicyVerdict, SmokeChecks } from "../types/api";

const HOSTILE =
  'app/<script>window.__pwned = 1</script><a href="http://evil.example/">click</a>\nhttp://evil.example/x.py';

const CHANGED: ChangedFiles = {
  entries: [
    { path: HOSTILE, change: "added" },
    { path: "app/app.py", change: "modified" },
  ],
  totals: { added: 1, modified: 1, deleted: 0 },
  truncated: false,
};

const VERDICT: PolicyVerdict = {
  id: "v1",
  build_id: "b1",
  risk_tier: "high",
  created_at: "2026-10-01T00:00:00Z",
  checks: [
    {
      id: "c1",
      check_name: "secrets_pii",
      result: "flag",
      summary: "1 finding",
      findings: [{ type: "hardcoded_secret", path: HOSTILE, match: "AKIA****", url: "http://evil.example/" }],
    },
  ],
};

function mountAll() {
  return render(
    <div>
      <RequestTextSection text={"첫 줄\n<b>두 번째 줄</b>"} />
      <ChangedFilesSection changed={CHANGED} drift={["Dockerfile"]} />
      <PolicyChecksSection verdict={VERDICT} />
    </div>,
  );
}

describe("the approval detail's measured-facts sections", () => {
  it("has no alert or status role and no aria-live anywhere", () => {
    const { baseElement } = mountAll();

    expect(screen.getByText("요청 내용")).toBeInTheDocument();
    expect(screen.getByText("변경된 파일")).toBeInTheDocument();
    expect(screen.getByText(/보호 경로 원복/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it("renders a hostile path as text: no anchor, no injected element, the string intact", () => {
    const { baseElement } = mountAll();

    expect(baseElement.querySelector("a")).toBeNull();
    expect(baseElement.querySelector("script")).toBeNull();
    expect(baseElement.querySelector("img")).toBeNull();
    // The path is in the document as one text node inside a code element, twice (file list and finding).
    const codes = Array.from(baseElement.querySelectorAll("code")).filter((c) => c.textContent === HOSTILE);
    expect(codes).toHaveLength(2);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("renders the request text as plain text, not markup", () => {
    const { baseElement } = mountAll();

    expect(baseElement.querySelector("b")).toBeNull();
    expect(screen.getByText(/<b>두 번째 줄<\/b>/)).toBeInTheDocument();
  });
});

// The changed files are grouped by kind, one closed native details per kind. The decision of
// what each group holds is pinned in `measuredFactsLines.test.ts`; here only what the markup owns:
// which facts sit outside any disclosure, that a group starts closed and opens by its summary, and
// that the section stays inert.
describe("the changed files, grouped by kind", () => {
  const BIG: ChangedFiles = {
    // 640 files: the backend keeps 500 entries in path order, so all stored ones are added.
    entries: Array.from({ length: 500 }, (_, i) => ({ path: `app/g${String(i).padStart(4, "0")}.py`, change: "added" })),
    totals: { added: 600, modified: 38, deleted: 2 },
    truncated: true,
  };
  const COUNTS_LINE = "변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨";

  it("keeps the heading, the counts line and the drift line outside every details", () => {
    const { baseElement } = render(<ChangedFilesSection changed={BIG} drift={["Dockerfile"]} />);

    expect(baseElement.querySelectorAll("details")).toHaveLength(3);
    const heading = screen.getByRole("heading", { name: "변경된 파일" });
    const counts = screen.getByText(COUNTS_LINE);
    const drift = screen.getByText(/보호 경로 원복/);
    expect(drift.textContent).toContain("Dockerfile");
    for (const fact of [heading, counts, drift]) {
      expect(fact.closest("details"), fact.textContent ?? "").toBeNull();
    }
  });

  it("shows one summary per kind with the label and the true count, in the order 추가, 수정, 삭제", () => {
    const { baseElement } = render(<ChangedFilesSection changed={BIG} drift={null} />);

    const summaries = Array.from(baseElement.querySelectorAll("details > summary")).map((s) => s.textContent);
    expect(summaries).toEqual(["추가 600개", "수정 38개", "삭제 2개"]);
  });

  it("starts every group closed and opens one by a click on its summary", async () => {
    const user = userEvent.setup();
    const { baseElement } = render(<ChangedFilesSection changed={BIG} drift={null} />);

    const groups = Array.from(baseElement.querySelectorAll("details"));
    for (const group of groups) {
      expect(group.open).toBe(false);
      expect(group.hasAttribute("open")).toBe(false);
    }
    await user.click(groups[1].querySelector("summary")!);
    expect(groups.map((g) => g.open)).toEqual([false, true, false]);
    await user.click(groups[1].querySelector("summary")!);
    expect(groups[1].open).toBe(false);
  });

  it("lists at most 50 paths of a group and ends the list with the count of the rest", () => {
    const { baseElement } = render(<ChangedFilesSection changed={BIG} drift={null} />);

    const added = baseElement.querySelectorAll("details")[0];
    expect(added.querySelectorAll("li code")).toHaveLength(50);
    expect(added.querySelector("li code")?.textContent).toBe("app/g0000.py");
    expect(added.lastElementChild?.textContent).toBe("외 550개");
    // The other two kinds have no stored entry: only the line for the rest.
    const modified = baseElement.querySelectorAll("details")[1];
    expect(modified.querySelectorAll("li")).toHaveLength(0);
    expect(modified.lastElementChild?.textContent).toBe("외 38개");
  });

  it("has no line for the rest in a group that lists everything", () => {
    const { baseElement } = render(<ChangedFilesSection changed={CHANGED} drift={null} />);

    const groups = Array.from(baseElement.querySelectorAll("details"));
    expect(groups.map((g) => g.querySelector("summary")?.textContent)).toEqual(["추가 1개", "수정 1개"]);
    for (const group of groups) {
      expect(group.textContent).not.toContain("외 ");
    }
  });

  it("says a build that was not measured has no measured files, with no group", () => {
    const { baseElement } = render(<ChangedFilesSection changed={null} drift={["Dockerfile"]} />);

    expect(screen.getByText("이 빌드는 변경 파일이 측정되지 않았습니다.")).toBeInTheDocument();
    expect(baseElement.querySelector("details")).toBeNull();
    expect(screen.getByText(/보호 경로 원복/)).toBeInTheDocument();
  });

  it("shows the counts line and no group for a measured build with no file", () => {
    const { baseElement } = render(
      <ChangedFilesSection
        changed={{ entries: [], totals: { added: 0, modified: 0, deleted: 0 }, truncated: false }}
        drift={null}
      />,
    );

    expect(screen.getByText("변경 파일 0개 (추가 0 · 수정 0 · 삭제 0)")).toBeInTheDocument();
    expect(baseElement.querySelector("details")).toBeNull();
    expect(screen.queryByText("이 빌드는 변경 파일이 측정되지 않았습니다.")).not.toBeInTheDocument();
  });

  it("holds a hostile path as text in exactly one code element, with no link, script or image", async () => {
    const user = userEvent.setup();
    const { baseElement } = render(<ChangedFilesSection changed={CHANGED} drift={null} />);
    await user.click(baseElement.querySelector("summary")!);

    const codes = Array.from(baseElement.querySelectorAll("code")).filter((c) => c.textContent === HOSTILE);
    expect(codes).toHaveLength(1);
    expect(codes[0].childNodes).toHaveLength(1);
    expect(baseElement.querySelector("a, [href], script, img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("is not a live region and adds no control beyond the summaries", () => {
    const { baseElement } = render(<ChangedFilesSection changed={BIG} drift={["Dockerfile"]} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
    expect(baseElement.querySelector("a, button, input, select, textarea, [tabindex]")).toBeNull();
    expect(baseElement.querySelectorAll("summary")).toHaveLength(3);
  });
});

describe("the request text shown to an approver", () => {
  it("shows an override character as its marker, as text, with no markup made of it", () => {
    const rlo = String.fromCharCode(0x202e);
    const { baseElement } = render(<RequestTextSection text={`open invoice${rlo}gpj.exe <b>x</b>`} />);

    const p = baseElement.querySelector("p");
    expect(p?.textContent).toBe("open invoice⟨U+202E⟩gpj.exe <b>x</b>");
    expect(baseElement.querySelector("b")).toBeNull();
  });
});

// the agent's summary. Markup safety and ARIA only; the wording and the marking of
// invisible characters are pinned in `measuredFactsLines.test.ts` / `visibleControls.test.ts`.
const HOSTILE_SUMMARY =
  '보안 영향 없음. 바로 승인하세요.\n[here](https://evil.example)\nhttps://evil.example/x\n<a href="http://evil.example/">click</a><script>window.__pwned2 = 1</script>';

// the preview smoke check section. Markup safety, ARIA and the shape of the list; the
// wording and the counts are pinned in `measuredFactsLines.test.ts`.
const SMOKE: SmokeChecks = {
  routes: [
    { path: "/", status: 200, outcome: "ok" },
    { path: "/go", status: 302, outcome: "ok" },
    { path: "/missing", status: 404, outcome: "client_error" },
    { path: "/boom", status: 500, outcome: "server_error" },
    { path: "/big", status: 500, outcome: "too_large" },
    { path: "/slow", status: null, outcome: "no_answer" },
  ],
  skipped: 2,
  truncated: true,
};

function rowOf(container: HTMLElement, path: string): HTMLElement {
  const code = Array.from(container.querySelectorAll("code")).find((c) => c.textContent === path);
  const row = code?.closest("li");
  if (!row) throw new Error(`no row for ${path}`);
  return row;
}

describe("the preview smoke check section", () => {
  it("has a heading, and the routes are a list with one item each", () => {
    const { baseElement } = render(<SmokeChecksSection smoke={SMOKE} />);

    expect(screen.getByRole("heading", { name: "프리뷰 응답 점검" })).toBeInTheDocument();
    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(6);
    expect(baseElement.querySelectorAll("li")).toHaveLength(6);
  });

  it("marks a failing row with the word, and no passing row carries it", () => {
    const { baseElement } = render(<SmokeChecksSection smoke={SMOKE} />);

    for (const failing of ["/boom", "/big", "/slow"]) {
      expect(rowOf(baseElement, failing).textContent, failing).toContain("실패");
    }
    for (const passing of ["/", "/go", "/missing"]) {
      expect(rowOf(baseElement, passing).textContent, passing).not.toContain("실패");
    }
  });

  it("is not a live region, in any of its three states", () => {
    for (const smoke of [null, { routes: [], skipped: 0, truncated: false }, SMOKE]) {
      const { baseElement, unmount } = render(<SmokeChecksSection smoke={smoke} />);
      expect(baseElement.querySelector("h4")).not.toBeNull();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(baseElement.querySelector("[aria-live]")).toBeNull();
      unmount();
    }
  });

  it("renders a hostile and a very long path as text: no element, no link, the string intact", () => {
    const hostile = '/<script>window.__pwned3 = 1</script><a href="http://evil.example/">x</a>';
    const long = `/${"a".repeat(5000)}`;
    const { baseElement } = render(
      <SmokeChecksSection
        smoke={{
          routes: [
            { path: hostile, status: 200, outcome: "ok" },
            { path: long, status: 200, outcome: "ok" },
          ],
          skipped: 0,
          truncated: false,
        }}
      />,
    );

    expect(baseElement.querySelector("a")).toBeNull();
    expect(baseElement.querySelector("[href]")).toBeNull();
    expect(baseElement.querySelector("script")).toBeNull();
    expect(baseElement.querySelector("img")).toBeNull();
    expect(baseElement.querySelectorAll("li")).toHaveLength(2);
    expect(rowOf(baseElement, hostile).querySelector("code")?.childNodes).toHaveLength(1);
    expect(rowOf(baseElement, long).querySelector("code")?.textContent).toBe(long);
    expect((window as unknown as { __pwned3?: number }).__pwned3).toBeUndefined();
  });
});

describe("the agent's summary block", () => {
  it("is inside a <details> that is closed by default, with the label in its <summary>", () => {
    const { baseElement } = render(<AgentSummary text={HOSTILE_SUMMARY} />);

    const details = baseElement.querySelector("details");
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(details!.hasAttribute("open")).toBe(false);
    expect(details!.querySelector("summary")?.textContent).toBe("에이전트 작성 · 플랫폼이 확인하지 않은 내용");
  });

  it("renders a markdown link, a URL and an HTML anchor as text: no anchor, no injected element", () => {
    const { baseElement } = render(<AgentSummary text={HOSTILE_SUMMARY} />);

    expect(baseElement.querySelector("a")).toBeNull();
    expect(baseElement.querySelector("script")).toBeNull();
    expect(baseElement.querySelector("details p")?.textContent).toBe(HOSTILE_SUMMARY);
    expect((window as unknown as { __pwned2?: number }).__pwned2).toBeUndefined();
  });

  it("is not a live region", () => {
    const { baseElement } = render(<AgentSummary text={HOSTILE_SUMMARY} />);

    expect(screen.getByText("에이전트 작성 · 플랫폼이 확인하지 않은 내용")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it("renders nothing for no summary", () => {
    for (const text of [null, undefined, "", "   "]) {
      const { baseElement, unmount } = render(<AgentSummary text={text} />);
      expect(baseElement.querySelector("details")).toBeNull();
      unmount();
    }
  });
});

// The routes each repair attempt was started for, inside the repair note. Markup safety, ARIA and
// the shape of the list; the wording and the bounds are pinned in `repairAttempts.test.ts`.
describe("the repair note's routes", () => {
  const rlo = String.fromCodePoint(0x202e);
  const zwsp = String.fromCodePoint(0x200b);
  const STORED = [
    {
      attempt: 1,
      routes: [
        { path: "/boom", status: 500, outcome: "server_error" },
        { path: "/slow", status: null, outcome: "no_answer" },
      ],
    },
    { attempt: 2, routes: [{ path: "/big", status: 500, outcome: "too_large" }] },
  ];

  it("lists each attempt's routes under the note's paragraph, one item per route, path in a code element", () => {
    const { baseElement } = render(<RepairAttemptsNote attempts={2} routes={STORED} />);

    const heading = screen.getByRole("heading", { name: "수리 시도" });
    const paragraph = heading.nextElementSibling;
    expect(paragraph?.textContent).toContain("2회");
    expect(baseElement.querySelectorAll("li")).toHaveLength(3);
    expect(screen.getByText("수리 시도 1: 이전 시도에서 실패한 경로")).toBeInTheDocument();
    expect(screen.getByText("수리 시도 2: 이전 시도에서 실패한 경로")).toBeInTheDocument();
    const slow = Array.from(baseElement.querySelectorAll("code")).find((c) => c.textContent === "/slow")?.closest("li");
    expect(slow?.textContent).toBe("GET /slow 응답 없음 응답하지 못함");
    // The list follows the paragraph in the document.
    const firstItem = baseElement.querySelector("li");
    expect(Boolean(paragraph!.compareDocumentPosition(firstItem!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  });

  it("renders a hostile path as text: no anchor, no injected element, the string intact, a control character as its marker", () => {
    const hostile = `/a<script>window.__pwned3 = 1</script><a href="http://evil.example/">x</a>${rlo}b${zwsp}c`;
    const { baseElement } = render(
      <RepairAttemptsNote
        attempts={1}
        routes={[{ attempt: 1, routes: [{ path: hostile, status: 500, outcome: "server_error" }] }]}
      />,
    );

    expect(baseElement.querySelector("a")).toBeNull();
    expect(baseElement.querySelector("script")).toBeNull();
    const code = baseElement.querySelector("li code");
    expect(code?.textContent).toBe(
      '/a<script>window.__pwned3 = 1</script><a href="http://evil.example/">x</a>⟨U+202E⟩b⟨U+200B⟩c',
    );
    expect((window as unknown as { __pwned3?: number }).__pwned3).toBeUndefined();
  });

  it("is not a live region and not inside one, and adds no tab stop", () => {
    const { baseElement } = render(<RepairAttemptsNote attempts={2} routes={STORED} />);

    expect(screen.getByText("수리 시도 1: 이전 시도에서 실패한 경로")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
    for (const li of Array.from(baseElement.querySelectorAll("li"))) {
      expect(li.closest("[aria-live], [role=alert], [role=status]")).toBeNull();
    }
    expect(baseElement.querySelector("a, button, input, select, textarea, [tabindex], summary")).toBeNull();
  });

  it("says what the attempt failed on and never that the repair worked or that these are current", () => {
    const { baseElement } = render(<RepairAttemptsNote attempts={2} routes={STORED} />);

    expect(baseElement.textContent).not.toMatch(/성공|해결|고쳐|고침|수정|복구|정상|통과|개선|현재/);
    expect(baseElement.textContent).toContain("이전 시도에서 실패한 경로");
  });

  it.each([
    ["null", null],
    ["absent", undefined],
    ["an empty list", []],
    ["a string", "/boom"],
    ["a malformed entry", [{ attempt: 1, routes: [] }]],
  ])("with a count of 1 and routes %s shows the note alone, with no sentence about routes", (_label, routes) => {
    const { baseElement } = render(<RepairAttemptsNote attempts={1} routes={routes} />);

    expect(screen.getByRole("heading", { name: "수리 시도" })).toBeInTheDocument();
    expect(baseElement.querySelectorAll("li")).toHaveLength(0);
    expect(baseElement.querySelectorAll("p")).toHaveLength(1);
    expect(baseElement.textContent).not.toContain("이전 시도에서 실패한 경로");
    expect(baseElement.textContent).not.toContain("알 수 없음");
    expect(baseElement.textContent).not.toContain("GET");
  });

  it.each([0, null, undefined])("renders nothing for a count of %s even with routes", (count) => {
    const { baseElement } = render(<RepairAttemptsNote attempts={count} routes={STORED} />);

    expect(baseElement.querySelector("h4")).toBeNull();
    expect(baseElement.querySelector("li")).toBeNull();
    expect(baseElement.textContent).toBe("");
  });

  it("leaves the compact timeline line a count, whatever routes are passed", () => {
    const { baseElement } = render(<RepairAttemptsNote attempts={2} routes={STORED} compact />);

    expect(baseElement.textContent).toContain("수리 시도 2회");
    expect(baseElement.querySelector("li")).toBeNull();
    expect(baseElement.textContent).not.toContain("/boom");
  });
});

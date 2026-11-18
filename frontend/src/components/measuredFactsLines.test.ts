/**
 * the wording and the bounds of what the platform measured. Decision logic,
 * not rendering (ADR 0021): pure functions over the API's shapes.
 */
import { describe, expect, it } from "vitest";

import type { ChangedFiles, SmokeChecks, SmokeOutcome, SmokeRoute } from "../types/api";
import {
  AGENT_SUMMARY_LABEL,
  CHANGED_NOT_MEASURED_LINE,
  SMOKE_OUTCOME_LABELS,
  MAX_FILES_SHOWN,
  MAX_FINDINGS_SHOWN,
  agentSummaryView,
  asChangedCounts,
  asStringList,
  changedCountsLine,
  changedTotal,
  changedFilesView,
  changeKindLabel,
  driftView,
  findingsView,
  isSmokeRouteFailure,
  requestTextLine,
  smokeChecksView,
  smokeCounts,
} from "./measuredFactsLines";

// The hostile path: markup, newlines and a URL, all of which must come back as written.
const HOSTILE = "app/<script>alert(1)</script>\nhttp://evil.example/x?a=1&b=2\n<img src=x onerror=y>.py";

function files(n: number, change = "added"): ChangedFiles {
  return {
    entries: Array.from({ length: n }, (_, i) => ({ path: `app/f${String(i).padStart(4, "0")}.py`, change })),
    totals: { added: change === "added" ? n : 0, modified: change === "modified" ? n : 0, deleted: 0 },
    truncated: false,
  };
}

describe("changedCountsLine", () => {
  it("states the total and the split, in the literal wording", () => {
    expect(changedCountsLine({ totals: { added: 3, modified: 8, deleted: 1 }, truncated: false })).toBe(
      "변경 파일 12개 (추가 3 · 수정 8 · 삭제 1)",
    );
  });

  it("says the stored list is partial when it is truncated, and not otherwise", () => {
    const line = changedCountsLine({ totals: { added: 900, modified: 0, deleted: 0 }, truncated: true });
    expect(line).toBe("변경 파일 900개 (추가 900 · 수정 0 · 삭제 0) · 목록은 일부만 저장됨");
    expect(changedCountsLine({ totals: { added: 900, modified: 0, deleted: 0 }, truncated: false })).not.toContain(
      "일부만",
    );
  });

  it("is zero for a measured build that changed nothing", () => {
    expect(changedCountsLine({ totals: { added: 0, modified: 0, deleted: 0 }, truncated: false })).toBe(
      "변경 파일 0개 (추가 0 · 수정 0 · 삭제 0)",
    );
  });

  it("is null, not zero, for a build that was not measured", () => {
    expect(changedCountsLine(null)).toBeNull();
    expect(changedCountsLine(undefined)).toBeNull();
  });
});

describe("reading untyped timeline data", () => {
  it("accepts the counts shape and rejects everything else as not measured", () => {
    expect(asChangedCounts({ totals: { added: 1 }, truncated: false })).toEqual({
      totals: { added: 1 },
      truncated: false,
    });
    for (const bad of [null, undefined, "x", 3, {}, { totals: null }]) {
      expect(asChangedCounts(bad)).toBeNull();
    }
    expect(asStringList(["Dockerfile"])).toEqual(["Dockerfile"]);
    expect(asStringList(null)).toBeNull();
    expect(asStringList("Dockerfile")).toBeNull();
  });
});

describe("driftView", () => {
  it("labels the paths and returns them as sent", () => {
    expect(driftView(["Dockerfile", HOSTILE])).toEqual({ label: "보호 경로 원복", paths: ["Dockerfile", HOSTILE] });
  });

  it("is null for no drift and for not measured", () => {
    expect(driftView([])).toBeNull();
    expect(driftView(null)).toBeNull();
    expect(driftView(undefined)).toBeNull();
  });
});

function entriesOf(kind: string, n: number, prefix = kind): ChangedFiles["entries"] {
  return Array.from({ length: n }, (_, i) => ({ path: `${prefix}/f${String(i).padStart(4, "0")}.py`, change: kind }));
}

describe("changedFilesView", () => {
  it("makes one group per kind, in the order 추가, 수정, 삭제, each with its true count", () => {
    const view = changedFilesView({
      entries: [...entriesOf("deleted", 2), ...entriesOf("modified", 3), ...entriesOf("added", 4)],
      totals: { added: 4, modified: 3, deleted: 2 },
      truncated: false,
    });
    expect(view?.countsLine).toBe("변경 파일 9개 (추가 4 · 수정 3 · 삭제 2)");
    expect(view?.groups.map((g) => [g.kind, g.label, g.count, g.countText])).toEqual([
      ["added", "추가", 4, "4개"],
      ["modified", "수정", 3, "3개"],
      ["deleted", "삭제", 2, "2개"],
    ]);
    expect(view?.groups[0].paths).toEqual(entriesOf("added", 4).map((e) => e.path));
    expect(view?.groups.every((g) => g.hiddenLine === null)).toBe(true);
  });

  it("has only the group of a single-kind build", () => {
    const view = changedFilesView(files(3, "modified"));
    expect(view?.groups).toHaveLength(1);
    expect(view?.groups[0].label).toBe("수정");
    expect(view?.groups[0].countText).toBe("3개");
    expect(view?.groups[0].paths).toHaveLength(3);
    expect(view?.groups[0].hiddenLine).toBeNull();
  });

  it("lists every path and no 'more' line up to the bound of 50 in a group", () => {
    const view = changedFilesView(files(MAX_FILES_SHOWN));
    expect(MAX_FILES_SHOWN).toBe(50);
    expect(view?.groups[0].paths).toHaveLength(50);
    expect(view?.groups[0].hiddenLine).toBeNull();
  });

  it("lists the first 50 of a larger group in stored order and counts the rest in an 외 N개 line", () => {
    const view = changedFilesView(files(73));
    const group = view?.groups[0];
    expect(group?.count).toBe(73);
    expect(group?.paths).toHaveLength(50);
    expect(group?.paths[0]).toBe("app/f0000.py");
    expect(group?.paths[49]).toBe("app/f0049.py");
    expect(group?.hiddenLine).toBe("외 23개");
  });

  it("bounds each group on its own", () => {
    const view = changedFilesView({
      entries: [...entriesOf("added", 60), ...entriesOf("modified", 55), ...entriesOf("deleted", 5)],
      totals: { added: 60, modified: 55, deleted: 5 },
      truncated: false,
    });
    expect(view?.groups.map((g) => [g.paths.length, g.hiddenLine])).toEqual([
      [50, "외 10개"],
      [50, "외 5개"],
      [5, null],
    ]);
  });

  it("shows the TRUE count of a truncated build, which stores fewer entries than the totals", () => {
    // 640 files: the backend keeps 500 entries in path order, so every stored entry is an added one.
    const stored: ChangedFiles = {
      entries: entriesOf("added", 500),
      totals: { added: 600, modified: 38, deleted: 2 },
      truncated: true,
    };
    const view = changedFilesView(stored);
    expect(view?.countsLine).toBe("변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨");
    expect(view?.groups.map((g) => [g.countText, g.paths.length, g.hiddenLine])).toEqual([
      ["600개", 50, "외 550개"],
      ["38개", 0, "외 38개"],
      ["2개", 0, "외 2개"],
    ]);
  });

  it("takes the limit as a parameter", () => {
    expect(changedFilesView(files(10), 3)?.groups[0].hiddenLine).toBe("외 7개");
  });

  it("gives a kind it does not know its own group after the three known, and drops no entry", () => {
    const view = changedFilesView({
      entries: [
        { path: "z.py", change: "renamed" },
        { path: "a.py", change: "added" },
        { path: "m.py", change: "copied" },
        { path: "n.py", change: "renamed" },
      ],
      totals: { added: 1, renamed: 2, copied: 1, modified: 0, deleted: 0 },
      truncated: false,
    });
    expect(view?.groups.map((g) => [g.kind, g.label, g.countText, g.paths])).toEqual([
      ["added", "추가", "1개", ["a.py"]],
      ["copied", "copied", "1개", ["m.py"]],
      ["renamed", "renamed", "2개", ["z.py", "n.py"]],
    ]);
  });

  it("keeps an unknown kind that only the totals mention, and one that only the entries mention", () => {
    const view = changedFilesView({
      entries: [{ path: "only-entry.py", change: "constructor" }],
      totals: { added: 0, modified: 0, deleted: 0, moved: 4 },
      truncated: true,
    });
    expect(view?.groups.map((g) => [g.kind, g.count, g.paths, g.hiddenLine])).toEqual([
      ["constructor", 1, ["only-entry.py"], null],
      ["moved", 4, [], "외 4개"],
    ]);
  });

  it("never reports fewer files in a group than it stores", () => {
    const view = changedFilesView({
      entries: entriesOf("added", 3),
      totals: { added: 1, modified: 0, deleted: 0 },
      truncated: false,
    });
    expect(view?.groups[0].count).toBe(3);
    expect(view?.groups[0].paths).toHaveLength(3);
  });

  it("labels each kind in Korean and shows an unknown kind as sent", () => {
    expect(changeKindLabel("added")).toBe("추가");
    expect(changeKindLabel("modified")).toBe("수정");
    expect(changeKindLabel("deleted")).toBe("삭제");
    expect(changeKindLabel("renamed")).toBe("renamed");
    expect(changeKindLabel("constructor")).toBe("constructor");
  });

  it("returns a path with markup, newlines and a URL unaltered, as a literal string", () => {
    const view = changedFilesView({
      entries: [{ path: HOSTILE, change: "added" }],
      totals: { added: 1, modified: 0, deleted: 0 },
      truncated: false,
    });
    expect(view?.groups[0].paths[0]).toBe(HOSTILE);
    expect(view?.groups[0].paths[0]).toContain("<script>");
    expect(view?.groups[0].paths[0].split(String.fromCharCode(10))).toHaveLength(3);
    // ...and the platform's own sentences never contain it.
    expect(view?.countsLine).not.toContain("script");
    expect(view?.groups[0].countText).toBe("1개");
    expect(view?.groups[0].hiddenLine).toBeNull();
  });

  it("has the counts line and no group for a measured build that changed nothing", () => {
    const view = changedFilesView(files(0));
    expect(view?.countsLine).toBe("변경 파일 0개 (추가 0 · 수정 0 · 삭제 0)");
    expect(view?.groups).toEqual([]);
  });

  it("is null for a build that was not measured", () => {
    expect(changedFilesView(null)).toBeNull();
    expect(changedFilesView(undefined)).toBeNull();
  });
});

describe("findingsView", () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => ({ type: "hardcoded_secret", path: `p${i}.py` }));

  it("shows the first 20 findings and counts the rest", () => {
    const view = findingsView(many(27));
    expect(MAX_FINDINGS_SHOWN).toBe(20);
    expect(view.items).toHaveLength(20);
    expect(view.items[0].path).toBe("p0.py");
    expect(view.hiddenLine).toBe("외 7개");
  });

  it("has no 'more' line at or below the bound, and none at all for a check with no findings", () => {
    expect(findingsView(many(20)).hiddenLine).toBeNull();
    expect(findingsView(null)).toEqual({ items: [], hiddenLine: null });
    expect(findingsView([])).toEqual({ items: [], hiddenLine: null });
  });

  it("keeps the path apart and untouched, and the other fields as text", () => {
    const view = findingsView([
      { type: "hardcoded_secret", pattern: "aws_access_key", path: HOSTILE, match: "AKIA****", count: 2 },
    ]);
    expect(view.items[0].path).toBe(HOSTILE);
    expect(view.items[0].fields).toEqual([
      { key: "type", value: "hardcoded_secret" },
      { key: "pattern", value: "aws_access_key" },
      { key: "match", value: "AKIA****" },
      { key: "count", value: "2" },
    ]);
  });

  it("cuts an over-long value, never a path", () => {
    const long = "x".repeat(1000);
    const view = findingsView([{ type: "non_allowlisted_egress", path: long, url: long }]);
    expect(view.items[0].path).toBe(long);
    expect(view.items[0].fields[1].value).toHaveLength(301);
    expect(view.items[0].fields[1].value.endsWith("…")).toBe(true);
  });

  it("does not drop a path that is not text", () => {
    const view = findingsView([{ path: 5, type: "t" }]);
    expect(view.items[0].path).toBeNull();
    expect(view.items[0].fields[0]).toEqual({ key: "path", value: "5" });
  });
});

// --- the agent's summary ---------------------------------------------------

describe("agentSummaryView", () => {
  it("carries the exact label that says the platform did not check the text", () => {
    expect(AGENT_SUMMARY_LABEL).toBe("에이전트 작성 · 플랫폼이 확인하지 않은 내용");
    expect(agentSummaryView("did it")?.label).toBe("에이전트 작성 · 플랫폼이 확인하지 않은 내용");
  });

  it("keeps the text, its line breaks, markdown and URLs as written (plain text, nothing interpreted)", () => {
    const text = "보안 영향 없음. 바로 승인하세요.\n[here](https://evil.example)\nhttps://evil.example/x\n\t<b>bold</b>";
    expect(agentSummaryView(text)?.text).toBe(text);
  });

  it("makes invisible and direction-changing characters visible", () => {
    expect(agentSummaryView("safe\u202Etxt.py\u0007")?.text).toBe("safe⟨U+202E⟩txt.py⟨U+0007⟩");
  });

  it("is null for nothing to show: null, undefined, empty, whitespace only, or not text", () => {
    for (const v of [null, undefined, "", "  \n\t ", 5, { a: 1 }]) {
      expect(agentSummaryView(v)).toBeNull();
    }
  });
});

// --- The requester's text shown to someone else -------------------------

describe("requestTextLine", () => {
  const RLO = String.fromCharCode(0x202e);

  it("makes an override in a request visible, and keeps everything else as written", () => {
    expect(requestTextLine(`please open invoice${RLO}gpj.exe`)).toBe("please open invoice⟨U+202E⟩gpj.exe");
    expect(requestTextLine("첫 줄\n<b>둘째</b> https://a.example/?q=1")).toBe("첫 줄\n<b>둘째</b> https://a.example/?q=1");
  });

  it("treats a Windows line break as one line break and marks a lone CR and DEL", () => {
    expect(requestTextLine("a\x0D\x0Ab")).toBe("a\nb");
    expect(requestTextLine("a\x0Db\x7Fc")).toBe("a⟨U+000D⟩b⟨U+007F⟩c");
  });
});

// the preview smoke check, worded for the approval detail.
function route(outcome: SmokeOutcome, status: number | null, path = "/"): SmokeRoute {
  return { path, status, outcome };
}

describe("isSmokeRouteFailure", () => {
  const table: Array<[string, SmokeRoute, boolean]> = [
    ["ok 200", route("ok", 200), false],
    ["ok 302 (a redirect is not followed and is not a failure)", route("ok", 302), false],
    ["client_error 404", route("client_error", 404), false],
    ["server_error 500", route("server_error", 500), true],
    ["no_answer with no status", route("no_answer", null), true],
    ["too_large 500 (the outcome hides the status class)", route("too_large", 500), true],
    ["too_large 200", route("too_large", 200), false],
    ["too_large with no status", route("too_large", null), false],
    ["client_error 499", route("client_error", 499), false],
    ["client_error 500", route("client_error", 500), true],
    ["server_error 499", route("server_error", 499), true],
    ["ok 503 (the status decides whatever the outcome says)", route("ok", 503), true],
  ];

  for (const [name, r, expected] of table) {
    it(`${name} -> ${expected}`, () => {
      expect(isSmokeRouteFailure(r)).toBe(expected);
    });
  }
});

describe("the smoke outcome labels", () => {
  // Typed as a Record over the union: when the backend enum gains a value, this literal stops
  // compiling until the value is added here, and then the loop below checks it has a label.
  const BACKEND_OUTCOMES: Record<SmokeOutcome, true> = {
    ok: true,
    client_error: true,
    server_error: true,
    no_answer: true,
    too_large: true,
  };

  it("has a non-empty Korean label for every outcome the backend can send", () => {
    for (const outcome of Object.keys(BACKEND_OUTCOMES)) {
      const label = (SMOKE_OUTCOME_LABELS as Record<string, string | undefined>)[outcome];
      expect(typeof label, outcome).toBe("string");
      expect(label?.trim().length ?? 0, outcome).toBeGreaterThan(0);
      expect(label, outcome).toMatch(/[가-힣]/);
    }
  });

  it("has no label for an outcome that does not exist", () => {
    expect(Object.keys(SMOKE_OUTCOME_LABELS).sort()).toEqual(Object.keys(BACKEND_OUTCOMES).sort());
  });

  it("shows an outcome this client does not know as it was sent", () => {
    for (const unknown of ["brand_new", "constructor"]) {
      const view = smokeChecksView({
        routes: [{ path: "/x", status: 200, outcome: unknown as unknown as SmokeOutcome }],
        skipped: 0,
        truncated: false,
      });
      expect(view.routes[0].outcomeLabel).toBe(unknown);
    }
  });
});

describe("smokeCounts", () => {
  it("counts the routes called and the ones that failed", () => {
    expect(
      smokeCounts([route("ok", 200), route("server_error", 500), route("too_large", 500), route("client_error", 404)]),
    ).toEqual({ called: 4, failed: 2 });
    expect(smokeCounts([])).toEqual({ called: 0, failed: 0 });
  });
});

describe("smokeChecksView: the three states are never conflated", () => {
  it("NULL is not measured: no count, no zero, no pass wording", () => {
    for (const value of [null, undefined]) {
      const view = smokeChecksView(value);
      expect(view.measured).toBe(false);
      expect(view.summary).toBe("측정되지 않음: 점검 결과가 없습니다. 이는 확인되었다는 뜻이 아닙니다.");
      expect(view.summary).not.toMatch(/\d/);
      expect(view.summary).not.toMatch(/통과|성공|정상|없음/);
      expect(view.notes).toEqual([]);
      expect(view.routes).toEqual([]);
    }
  });

  it("a malformed value is not measured either, never an invented zero", () => {
    expect(smokeChecksView({ skipped: 2, truncated: true } as unknown as SmokeChecks).measured).toBe(false);
  });

  it("measured with no routes says the service exposes none, and is not the not-measured line", () => {
    const view = smokeChecksView({ routes: [], skipped: 0, truncated: false });
    expect(view.measured).toBe(true);
    expect(view.summary).toBe("호출할 수 있는 GET 경로가 없음");
    expect(view.notes).toEqual([]);
    expect(view.routes).toEqual([]);
    expect(view.summary).not.toBe(smokeChecksView(null).summary);
  });

  it("measured with no routes still reports what was skipped and that it was partial", () => {
    expect(smokeChecksView({ routes: [], skipped: 3, truncated: false }).notes).toEqual(["건너뛴 경로 3개"]);
    const both = smokeChecksView({ routes: [], skipped: 3, truncated: true });
    expect(both.summary).toBe("호출할 수 있는 GET 경로가 없음");
    expect(both.notes).toEqual(["건너뛴 경로 3개", "일부만 점검됨"]);
  });

  it("measured with routes: a summary of how many were called and how many failed, then one row each", () => {
    const view = smokeChecksView({
      routes: [route("ok", 200, "/"), route("client_error", 404, "/missing"), route("no_answer", null, "/slow")],
      skipped: 0,
      truncated: false,
    });
    expect(view.measured).toBe(true);
    expect(view.summary).toBe("호출한 GET 경로 3개 중 1개 실패");
    expect(view.notes).toEqual([]);
    expect(view.routes).toEqual([
      { path: "/", statusText: "200", outcomeLabel: "응답함", failed: false },
      { path: "/missing", statusText: "404", outcomeLabel: "클라이언트 오류 (4xx)", failed: false },
      { path: "/slow", statusText: "응답 없음", outcomeLabel: "응답하지 못함", failed: true },
    ]);
  });

  it("says no route failed when none did, and counts a too-large 5xx as a failure", () => {
    const quiet = smokeChecksView({ routes: [route("ok", 200), route("ok", 302, "/go")], skipped: 0, truncated: false });
    expect(quiet.summary).toBe("호출한 GET 경로 2개 · 실패 없음");
    const big = smokeChecksView({ routes: [route("too_large", 500, "/big")], skipped: 0, truncated: false });
    expect(big.summary).toBe("호출한 GET 경로 1개 중 1개 실패");
    expect(big.routes[0]).toEqual({ path: "/big", statusText: "500", outcomeLabel: "응답이 너무 큼", failed: true });
  });

  it("notes the skipped routes only when there are some, and the partial check only when truncated", () => {
    const base = { routes: [route("ok", 200)] };
    expect(smokeChecksView({ ...base, skipped: 0, truncated: false }).notes).toEqual([]);
    expect(smokeChecksView({ ...base, skipped: 4, truncated: false }).notes).toEqual(["건너뛴 경로 4개"]);
    expect(smokeChecksView({ ...base, skipped: 0, truncated: true }).notes).toEqual(["일부만 점검됨"]);
    expect(smokeChecksView({ ...base, skipped: 4, truncated: true }).notes).toEqual(["건너뛴 경로 4개", "일부만 점검됨"]);
  });

  it("returns a hostile or very long path as it came, outside every platform sentence", () => {
    const hostile = "/<script>x</script>";
    const long = `/${"a".repeat(5000)}`;
    const view = smokeChecksView({
      routes: [route("ok", 200, hostile), route("ok", 200, long)],
      skipped: 0,
      truncated: false,
    });
    expect(view.routes.map((r) => r.path)).toEqual([hostile, long]);
    expect(view.summary).not.toContain("script");
  });
});

describe("changedTotal", () => {
  it("sums the true totals of the three kinds, and is null for a build that was not measured", () => {
    expect(changedTotal({ totals: { added: 600, modified: 38, deleted: 2 }, truncated: true })).toBe(640);
    expect(changedTotal({ totals: {}, truncated: false })).toBe(0);
    expect(changedTotal(null)).toBeNull();
    expect(changedTotal(undefined)).toBeNull();
  });

  it("states a build whose files were not measured in one literal sentence", () => {
    expect(CHANGED_NOT_MEASURED_LINE).toBe("이 빌드는 변경 파일이 측정되지 않았습니다.");
  });
});

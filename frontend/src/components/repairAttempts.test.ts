/** The wording and the gates of the repair display. Literals, because the wording is a contract. */
import { describe, expect, it } from "vitest";

import {
  asRepairAttempts,
  caseAttemptsText,
  repairApprovalNote,
  repairApprovalView,
  repairRoutesView,
  repairSettingLabel,
  repairTimelineLine,
  shouldShowRepairNote,
} from "./repairAttempts";

describe("shouldShowRepairNote", () => {
  it.each([1, 2, 3, 10])("is true for the integer %s", (value) => {
    expect(shouldShowRepairNote(value)).toBe(true);
  });

  it.each([
    ["0", 0],
    ["null", null],
    ["undefined", undefined],
    ["a fraction", 1.5],
    ["a fraction below one", 0.5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["a negative", -1],
    ["true", true],
    ["false", false],
    ["the string 1", "1"],
    ["an empty string", ""],
    ["an object", {}],
    ["an array", [1]],
  ])("is false for %s", (_label, value) => {
    expect(shouldShowRepairNote(value)).toBe(false);
  });
});

describe("asRepairAttempts", () => {
  it.each([0, 1, 7])("keeps the non-negative integer %s", (value) => {
    expect(asRepairAttempts(value)).toBe(value);
  });

  it.each([null, undefined, -1, 1.5, Number.NaN, true, "2", {}, []])("reads %s as no count", (value) => {
    expect(asRepairAttempts(value)).toBeNull();
  });
});

describe("the timeline line", () => {
  it("is a fixed sentence plus the number", () => {
    expect(repairTimelineLine(1)).toBe(
      "수리 시도 1회: 프리뷰 응답 점검에서 실패한 경로가 있어 에이전트에게 다시 시도하게 했습니다.",
    );
    expect(repairTimelineLine(2)).toContain("수리 시도 2회");
  });

  it.each([0, null, undefined, "1", 1.5, -1, true])("is absent for %s", (value) => {
    expect(repairTimelineLine(value)).toBeNull();
  });
});

describe("the approval note", () => {
  it("says what the platform did and that the smoke check below is the shipped attempt's", () => {
    expect(repairApprovalNote(1)).toBe(
      "플랫폼의 프리뷰 응답 점검에서 실패한 경로가 확인되어 에이전트에게 1회 다시 시도하게 했습니다. " +
        "아래 프리뷰 응답 점검은 최종으로 전달된 시도의 결과입니다.",
    );
    expect(repairApprovalNote(3)).toContain("3회");
  });

  it("never says the repair worked or that the build was fixed", () => {
    expect(repairApprovalNote(2)).not.toMatch(/성공|해결|고쳐|고침|수정|복구|정상|통과|개선/);
    expect(repairTimelineLine(2)).not.toMatch(/성공|해결|고쳐|고침|수정|복구|정상|통과|개선/);
  });

  it.each([0, null, undefined, "2", 2.5, -2, false])("is absent for %s", (value) => {
    expect(repairApprovalNote(value)).toBeNull();
  });
});

describe("the repair setting a run recorded", () => {
  it("words a number as a maximum and 0 as the loop being off", () => {
    expect(repairSettingLabel(2)).toBe("수리 시도 최대 2회");
    expect(repairSettingLabel(1)).toBe("수리 시도 최대 1회");
    expect(repairSettingLabel(0)).toBe("수리 시도 최대 0회 (수리 사용 안 함)");
  });

  it.each([null, undefined, -1, 1.5, "2", Number.NaN])("says not recorded for %s, never 0", (value) => {
    const text = repairSettingLabel(value);
    expect(text).toBe("수리 설정 기록 없음");
    expect(text).not.toContain("0회");
  });
});

describe("a case's attempts", () => {
  it("shows a number, 0 included, once the case has measured figures", () => {
    expect(caseAttemptsText(0, true)).toBe("0회");
    expect(caseAttemptsText(1, true)).toBe("1회");
  });

  it("shows a dash for NULL, and for anything when the case has no measured figures", () => {
    expect(caseAttemptsText(null, true)).toBe("—");
    expect(caseAttemptsText(undefined, true)).toBe("—");
    expect(caseAttemptsText(0, false)).toBe("—");
    expect(caseAttemptsText(2, false)).toBe("—");
  });
});

// The routes each repair attempt was started for. Literals: the labels and the bounds are a contract.
const ROUTE = { path: "/boom", status: 500, outcome: "server_error" };
const NO_ANSWER = { path: "/slow", status: null, outcome: "no_answer" };

function routesOf(n: number) {
  return Array.from({ length: n }, (_, i) => ({ path: `/r${i}`, status: 500, outcome: "server_error" }));
}

describe("repairRoutesView: a well formed value", () => {
  it("words each attempt and each route, with the smoke section's status and outcome wording", () => {
    const view = repairRoutesView([
      { attempt: 1, routes: [ROUTE, NO_ANSWER] },
      {
        attempt: 2,
        routes: [
          { path: "/big", status: 500, outcome: "too_large" },
          { path: "/x", status: 404, outcome: "client_error" },
        ],
      },
    ]);

    expect(view).not.toBeNull();
    expect(view!.hiddenLine).toBeNull();
    expect(view!.attempts.map((a) => a.label)).toEqual([
      "수리 시도 1: 이전 시도에서 실패한 경로",
      "수리 시도 2: 이전 시도에서 실패한 경로",
    ]);
    expect(view!.attempts[0].hiddenLine).toBeNull();
    expect(view!.attempts[0].routes.map((r) => [r.path, r.statusText, r.outcomeLabel])).toEqual([
      ["/boom", "500", "서버 오류 (5xx)"],
      ["/slow", "응답 없음", "응답하지 못함"],
    ]);
    expect(view!.attempts[1].routes.map((r) => [r.path, r.statusText, r.outcomeLabel])).toEqual([
      ["/big", "500", "응답이 너무 큼"],
      ["/x", "404", "클라이언트 오류 (4xx)"],
    ]);
  });

  it("returns a path exactly as stored, never altered or interpolated", () => {
    const rlo = String.fromCodePoint(0x202e);
    const path = `/a${rlo}b <b>x</b> http://evil.example/`;
    const view = repairRoutesView([{ attempt: 1, routes: [{ path, status: 500, outcome: "server_error" }] }]);

    expect(view!.attempts[0].routes[0].path).toBe(path);
    expect(view!.attempts[0].label).not.toContain("/a");
  });

  it("shows an outcome this client does not know as sent, and a 3xx status as its number", () => {
    const view = repairRoutesView([{ attempt: 1, routes: [{ path: "/n", status: 302, outcome: "brand_new" }] }]);

    expect(view!.attempts[0].routes[0].statusText).toBe("302");
    expect(view!.attempts[0].routes[0].outcomeLabel).toBe("brand_new");
  });

  it("keeps the stored order and the stored attempt numbers", () => {
    const view = repairRoutesView([
      { attempt: 3, routes: [ROUTE] },
      { attempt: 2, routes: [ROUTE] },
    ]);

    expect(view!.attempts.map((a) => a.label)).toEqual([
      "수리 시도 3: 이전 시도에서 실패한 경로",
      "수리 시도 2: 이전 시도에서 실패한 경로",
    ]);
  });
});

describe("repairRoutesView: the bounds", () => {
  it("lists 3 attempts and says how many were left out", () => {
    const entries = [1, 2, 3, 4, 5].map((attempt) => ({ attempt, routes: [ROUTE] }));
    const view = repairRoutesView(entries);

    expect(view!.attempts).toHaveLength(3);
    expect(view!.attempts.map((a) => a.label)).toEqual([
      "수리 시도 1: 이전 시도에서 실패한 경로",
      "수리 시도 2: 이전 시도에서 실패한 경로",
      "수리 시도 3: 이전 시도에서 실패한 경로",
    ]);
    expect(view!.hiddenLine).toBe("외 2회 시도");
  });

  it("lists exactly 3 attempts with no left-out line", () => {
    const view = repairRoutesView([1, 2, 3].map((attempt) => ({ attempt, routes: [ROUTE] })));

    expect(view!.attempts).toHaveLength(3);
    expect(view!.hiddenLine).toBeNull();
  });

  it("lists 20 routes per attempt and says how many were left out", () => {
    const view = repairRoutesView([{ attempt: 1, routes: routesOf(25) }]);

    expect(view!.attempts[0].routes).toHaveLength(20);
    expect(view!.attempts[0].routes[19].path).toBe("/r19");
    expect(view!.attempts[0].hiddenLine).toBe("외 5개");
  });

  it("lists exactly 20 routes with no left-out line", () => {
    const view = repairRoutesView([{ attempt: 1, routes: routesOf(20) }]);

    expect(view!.attempts[0].routes).toHaveLength(20);
    expect(view!.attempts[0].hiddenLine).toBeNull();
  });
});

describe("repairRoutesView: anything else is null, never a partial list", () => {
  const bad = (route: unknown) => [{ attempt: 1, routes: [route] }];
  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a string", "[]"],
    ["a number", 2],
    ["an object", { attempt: 1, routes: [ROUTE] }],
    ["an empty list", []],
    ["an entry that is null", [null]],
    ["an entry that is a list", [[1, [ROUTE]]]],
    ["an entry with no routes key", [{ attempt: 1 }]],
    ["an entry with an empty routes list", [{ attempt: 1, routes: [] }]],
    ["an entry whose routes is an object", [{ attempt: 1, routes: ROUTE }]],
    ["an entry whose routes is a string", [{ attempt: 1, routes: "/a" }]],
    ["an attempt of 0", [{ attempt: 0, routes: [ROUTE] }]],
    ["a negative attempt", [{ attempt: -1, routes: [ROUTE] }]],
    ["a fractional attempt", [{ attempt: 1.5, routes: [ROUTE] }]],
    ["an attempt that is a string", [{ attempt: "1", routes: [ROUTE] }]],
    ["a missing attempt", [{ routes: [ROUTE] }]],
    ["a route that is null", bad(null)],
    ["a route that is a string", bad("/a")],
    ["a route that is a list", bad(["/a", 500, "server_error"])],
    ["a path that is not a string", bad({ path: 7, status: 500, outcome: "server_error" })],
    ["a missing path", bad({ status: 500, outcome: "server_error" })],
    ["a status that is a string", bad({ path: "/a", status: "500", outcome: "server_error" })],
    ["a status that is a fraction", bad({ path: "/a", status: 500.5, outcome: "server_error" })],
    ["a status that is NaN", bad({ path: "/a", status: Number.NaN, outcome: "server_error" })],
    ["a missing status", bad({ path: "/a", outcome: "server_error" })],
    ["an outcome that is not a string", bad({ path: "/a", status: 500, outcome: 5 })],
    ["a missing outcome", bad({ path: "/a", status: 500 })],
  ])("is null for %s", (_label, value) => {
    expect(repairRoutesView(value)).toBeNull();
  });

  it("is null when the first entry is valid and a later one is not (no partial list)", () => {
    expect(repairRoutesView([{ attempt: 1, routes: [ROUTE] }, { attempt: 2, routes: [] }])).toBeNull();
    expect(repairRoutesView([{ attempt: 1, routes: [ROUTE] }, null])).toBeNull();
    expect(
      repairRoutesView([{ attempt: 1, routes: [ROUTE, { path: 1, status: 500, outcome: "server_error" }] }]),
    ).toBeNull();
  });

  it("is null when a malformed entry lies beyond the shown bound too", () => {
    const entries: unknown[] = [1, 2, 3].map((attempt) => ({ attempt, routes: [ROUTE] }));
    entries.push({ attempt: 4, routes: [] });

    expect(repairRoutesView(entries)).toBeNull();
  });

  it("never throws, even for a value that throws when read", () => {
    const hostile = {
      get attempt(): number {
        throw new Error("boom");
      },
      routes: [ROUTE],
    };
    const proxy = new Proxy([], {
      get() {
        throw new Error("boom");
      },
    });

    expect(() => repairRoutesView([hostile])).not.toThrow();
    expect(repairRoutesView([hostile])).toBeNull();
    expect(() => repairRoutesView(proxy)).not.toThrow();
    expect(repairRoutesView(proxy)).toBeNull();
  });
});

describe("repairApprovalView: the routes show only under a note that shows", () => {
  const stored = [{ attempt: 1, routes: [ROUTE] }];

  it("gives the note and the routes for a count of 1 or more with well formed routes", () => {
    const view = repairApprovalView(2, stored);

    expect(view!.note).toBe(repairApprovalNote(2));
    expect(view!.routes!.attempts[0].routes[0].path).toBe("/boom");
  });

  it.each([null, undefined, "x", [], [{ attempt: 1, routes: [] }]])("gives the note alone for routes %j", (routes) => {
    const view = repairApprovalView(1, routes);

    expect(view!.note).toBe(repairApprovalNote(1));
    expect(view!.routes).toBeNull();
  });

  it.each([0, null, undefined, "1", 1.5, -1, true])("gives nothing at all for a count of %s, whatever the routes", (count) => {
    expect(repairApprovalView(count, stored)).toBeNull();
  });
});

describe("the routes list never says the repair worked", () => {
  it("has no word of success, a fix or a pass in a label or a status wording", () => {
    const view = repairRoutesView([
      { attempt: 1, routes: [ROUTE, NO_ANSWER, { path: "/b", status: 500, outcome: "too_large" }] },
      { attempt: 2, routes: [{ path: "/c", status: 404, outcome: "client_error" }] },
    ]);
    const words = view!.attempts.flatMap((a) => [a.label, ...a.routes.flatMap((r) => [r.statusText, r.outcomeLabel])]);

    for (const text of words) expect(text).not.toMatch(/성공|해결|고쳐|고침|수정|복구|정상|통과|개선|현재/);
  });
});

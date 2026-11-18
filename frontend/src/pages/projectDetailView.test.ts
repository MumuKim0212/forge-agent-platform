/**
 * What the project page's tabs and summary tiles decide (ADR 0021: the decision, not the
 * rendering). The tab ids and labels are contract strings the page and its a11y tests read, so they
 * are pinned here as literals.
 */
import { describe, expect, it } from "vitest";

import {
  DEFAULT_PROJECT_TAB,
  NO_DEPLOYMENT_TEXT,
  PROJECT_TABS,
  deploymentCountLabel,
  lastDeploymentLabel,
  tabAfterChangeRequest,
  type ProjectTabId,
} from "./projectDetailView";

describe("the tabs: which exist, in what order, which opens first", () => {
  it("are 배포, 변경 요청, 타임라인, in that order", () => {
    expect(PROJECT_TABS.map((t) => [t.id, t.label])).toEqual([
      ["deploy", "배포"],
      ["change-request", "변경 요청"],
      ["timeline", "타임라인"],
    ]);
  });

  it("open on 배포", () => {
    expect(DEFAULT_PROJECT_TAB).toBe("deploy");
  });
});

describe("tabAfterChangeRequest: where a finished submit leaves the reader", () => {
  it("a submit the backend accepted opens the timeline, wherever the reader was", () => {
    const from: ProjectTabId[] = ["deploy", "change-request", "timeline"];
    for (const current of from) {
      expect(tabAfterChangeRequest(current, "submitted")).toBe("timeline");
    }
  });

  it("a refused submit stays on the form, so the message and the typed text are where they were", () => {
    expect(tabAfterChangeRequest("change-request", "failed")).toBe("change-request");
  });

  it("a refused submit never moves the reader: the tab it was on is the tab it stays on", () => {
    const from: ProjectTabId[] = ["deploy", "change-request", "timeline"];
    for (const current of from) {
      expect(tabAfterChangeRequest(current, "failed")).toBe(current);
    }
  });
});

describe("lastDeploymentLabel: the newest record's time, or 없음", () => {
  it("with no deployment says 없음, never blank", () => {
    expect(NO_DEPLOYMENT_TEXT).toBe("없음");
    expect(lastDeploymentLabel([])).toBe("없음");
  });

  it("reads the newest by time, not by position in the list", () => {
    const oldestFirst = [{ created_at: "2024-03-01T00:00:00Z" }, { created_at: "2026-03-01T00:00:00Z" }];
    const newestFirst = [...oldestFirst].reverse();

    expect(lastDeploymentLabel(oldestFirst)).toContain("2026");
    expect(lastDeploymentLabel(oldestFirst)).not.toContain("2024");
    expect(lastDeploymentLabel(newestFirst)).toBe(lastDeploymentLabel(oldestFirst));
  });

  it("skips a time that does not parse, and says 없음 when none does, never Invalid Date", () => {
    expect(lastDeploymentLabel([{ created_at: "not a time" }, { created_at: "2026-03-01T00:00:00Z" }])).toContain(
      "2026",
    );
    expect(lastDeploymentLabel([{ created_at: "not a time" }])).toBe("없음");
    expect(lastDeploymentLabel([{ created_at: "" }])).not.toContain("Invalid");
  });
});

describe("deploymentCountLabel: the number of records the page loaded", () => {
  it("is the length of the list: zero for none, one per record otherwise", () => {
    expect(deploymentCountLabel([])).toBe("0회");
    expect(deploymentCountLabel([{}, {}, {}])).toBe("3회");
  });
});

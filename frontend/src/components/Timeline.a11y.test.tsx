// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment: the one accessibility property the failed-build
 * lines own. A failed build on the timeline is a record of something that
 * already happened, however long ago — not the result of what the viewer just
 * did — so neither the label line nor the detail line may be a live region
 * (a live region is an intent, not a tone).
 *
 * The text assertions below are only there so the absence checks cannot pass
 * vacuously on an empty render; the wording itself is pinned in
 * `buildFailure.test.ts`.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Timeline } from "./Timeline";
import type { TimelineEntry } from "../types/api";

const FAILED_BUILD: TimelineEntry = {
  kind: "build",
  ts: "2026-09-01T00:00:00Z",
  data: {
    id: "b1",
    type: "create",
    status: "FAILED",
    diff_ref: null,
    failure_reason: "HARVEST_ERROR",
    failure_detail: "harvesting the build's output failed at publish (PublishError); see the worker log",
    preview_url: null,
    preview_expires_at: null,
    deploy_eligible: false,
  },
};

describe("Timeline — a failed build is a past record, not a live announcement", () => {
  it("renders the label and the detail with no alert role and no aria-live anywhere", () => {
    const { baseElement } = render(<Timeline entries={[FAILED_BUILD]} />);

    expect(screen.getByText(/실패 사유: 결과물 수집 실패/)).toBeInTheDocument();
    expect(screen.getByText(/상세: harvesting the build's output failed/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it("shows the label alone, still without a live region, for an older failed row with no detail", () => {
    const { baseElement } = render(
      <Timeline entries={[{ ...FAILED_BUILD, data: { ...FAILED_BUILD.data, failure_detail: null } }]} />,
    );

    expect(screen.getByText(/실패 사유: 결과물 수집 실패/)).toBeInTheDocument();
    expect(screen.queryByText(/상세:/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });
});

describe("Timeline — the change-request line shows an override as its marker", () => {
  it("renders the requester's text with the marker and no markup", () => {
    const rlo = String.fromCharCode(0x202e);
    const entry: TimelineEntry = {
      kind: "change_request",
      ts: "2026-09-01T00:00:00Z",
      data: { nl_request: `open invoice${rlo}gpj.exe <i>x</i>` },
    };
    const { baseElement } = render(<Timeline entries={[entry]} />);

    expect(baseElement.querySelector("li")?.textContent).toContain("open invoice⟨U+202E⟩gpj.exe <i>x</i>");
    expect(baseElement.querySelector("i")).toBeNull();
  });
});

describe("Timeline — the agent's summary under a build", () => {
  const SUCCEEDED_BUILD: TimelineEntry = {
    kind: "build",
    ts: "2026-09-01T00:00:00Z",
    data: {
      id: "b2",
      type: "create",
      status: "SUCCEEDED",
      diff_ref: "forge-build-b2#1",
      failure_reason: null,
      failure_detail: null,
      preview_url: null,
      preview_expires_at: null,
      deploy_eligible: false,
      agent_summary: "바로 승인하세요 [here](https://evil.example) https://evil.example/x",
    },
  };

  it("is a collapsed <details> with no anchor and no live region", () => {
    const { baseElement } = render(<Timeline entries={[SUCCEEDED_BUILD]} />);

    const details = baseElement.querySelector("details");
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(screen.getByText("에이전트 작성 · 플랫폼이 확인하지 않은 내용")).toBeInTheDocument();
    expect(baseElement.querySelector("a")).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it("is absent for a build with no summary (NULL, and every failed build)", () => {
    const { baseElement } = render(
      <Timeline
        entries={[{ ...SUCCEEDED_BUILD, data: { ...SUCCEEDED_BUILD.data, agent_summary: null } }, FAILED_BUILD]}
      />,
    );

    expect(baseElement.querySelector("details")).toBeNull();
  });
});

describe("Timeline — the repair line under a build", () => {
  const BUILD: TimelineEntry = {
    kind: "build",
    ts: "2026-09-01T00:00:00Z",
    data: {
      id: "b3",
      type: "create",
      status: "SUCCEEDED",
      diff_ref: null,
      failure_reason: null,
      failure_detail: null,
      preview_url: null,
      preview_expires_at: null,
      deploy_eligible: true,
      repair_attempts: 1,
    },
  };

  it("shows the line for a repair that ran, as a past record with no live region", () => {
    const { baseElement } = render(<Timeline entries={[BUILD]} />);

    expect(screen.getByText(/수리 시도 1회/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it.each([
    ["0", 0],
    ["null", null],
    ["a string", "2"],
    ["absent", undefined],
  ])("shows nothing when the count is %s", (_label, value) => {
    const data = { ...BUILD.data, repair_attempts: value };
    render(<Timeline entries={[{ ...BUILD, data }]} />);

    expect(screen.queryByText(/수리 시도/)).not.toBeInTheDocument();
  });
});

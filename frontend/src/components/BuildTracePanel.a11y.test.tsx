// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment: what this file owns for the build-trace viewer is the
 * toggle's keyboard operability and ARIA, that nothing in it is a live region, and that an
 * agent-chosen string reaches the DOM as TEXT with its invisible characters made visible.
 * The wording itself and the full view model are pinned in `buildTraceView.test.ts`. The text
 * assertions here only keep the absence checks from passing on an empty render.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { BuildTraceEntry, BuildTraceView } from "./BuildTracePanel";
import { Timeline } from "./Timeline";
import type { BuildTrace, TimelineEntry } from "../types/api";

const traceMock = vi.fn();
vi.mock("../api/builds", () => ({
  buildsApi: { trace: (id: string) => traceMock(id) },
}));

const RLO = String.fromCharCode(0x202e);
const HOSTILE = '<a href="http://evil.example/">click</a><script>window.__pwned3 = 1</script>\nhttp://evil.example/x';

const TRACE: BuildTrace = {
  build_id: "b1",
  captured_at: "2026-10-10T00:00:00Z",
  measured: {
    calls: {
      state: "captured",
      steps: [
        {
          ordinal: 1,
          started_at: "2026-10-10T00:00:00Z",
          ended_at: "2026-10-10T00:00:02Z",
          model: "claude-test",
          input_tokens: 10,
          output_tokens: 5,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          tokens: 15,
          cost_usd: 0.01,
          stop_reason: "tool_use",
          content: "recorded",
          thinking: [{ chars: 40 }],
        },
      ],
      calls_total: 1,
      content_truncated: false,
      omitted_calls: 0,
      omitted_tokens: 0,
      omitted_cost_usd: 0,
    },
    diff: {
      state: "computed",
      files_total: 1,
      truncated: false,
      files: [{ path: `report${RLO}txt.py`, change: "added", content: "text", diff: `+print("x${RLO}y")\n+${HOSTILE}`, truncated: false }],
    },
  },
  claims: {
    calls: [
      {
        ordinal: 1,
        blocks: [
          { kind: "assistant_text", text: HOSTILE },
          { kind: "tool_use", name: "Bash", text: `{"command":"cat a${RLO}b"}` },
          { kind: "tool_result", text: `ok${RLO}` },
        ],
      },
    ],
    log: { text: `log line ${RLO}`, truncated: false },
  },
};

function expectNoLiveRegion(root: HTMLElement) {
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(root.querySelector("[aria-live]")).toBeNull();
}

beforeEach(() => {
  traceMock.mockReset();
});

describe("the trace entry point on a timeline build", () => {
  it("is a button that tells assistive tech whether the trace is open, and opens from the keyboard", async () => {
    traceMock.mockResolvedValue(TRACE);
    const user = userEvent.setup();
    render(<BuildTraceEntry buildId="b1" hasTrace={true} canRead={true} status="SUCCEEDED" />);

    const button = screen.getByRole("button", { name: "트레이스 보기" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(traceMock).not.toHaveBeenCalled();

    button.focus();
    await user.keyboard("{Enter}");
    const open = await screen.findByRole("button", { name: "트레이스 닫기" });
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(open).toHaveFocus();
    expect(document.getElementById(open.getAttribute("aria-controls")!)).not.toBeNull();
    expect(traceMock).toHaveBeenCalledWith("b1");
    await screen.findByText(/호출 1회/);

    await user.keyboard(" ");
    expect(screen.getByRole("button", { name: "트레이스 보기" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/호출 1회/)).not.toBeInTheDocument();
  });

  it("offers no control to a viewer the predicate refuses, and says 'no trace' only to one it allows", () => {
    const { container, rerender } = render(
      <BuildTraceEntry buildId="b1" hasTrace={true} canRead={false} status="SUCCEEDED" />,
    );
    expect(container).toBeEmptyDOMElement();

    rerender(<BuildTraceEntry buildId="b1" hasTrace={false} canRead={true} status="SUCCEEDED" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("이 빌드의 트레이스는 저장되어 있지 않습니다.")).toBeInTheDocument();
  });

  it("shows a 403 as an explained refusal, not a crash, and not as a live region", async () => {
    traceMock.mockRejectedValue(new ApiError(403, "Project owner or approver role required"));
    const user = userEvent.setup();
    const { baseElement } = render(<BuildTraceEntry buildId="b1" hasTrace={true} canRead={true} status="SUCCEEDED" />);

    await user.click(screen.getByRole("button", { name: "트레이스 보기" }));
    expect(
      await screen.findByText("이 빌드의 트레이스는 프로젝트 소유자와 승인자 이상만 볼 수 있습니다."),
    ).toBeInTheDocument();
    expectNoLiveRegion(baseElement);
  });

  it("shows a 404 as 'no stored trace' and any other failure as the generic message", async () => {
    const user = userEvent.setup();
    traceMock.mockRejectedValueOnce(new ApiError(404, "This build has no stored trace"));
    const { unmount } = render(<BuildTraceEntry buildId="b1" hasTrace={true} canRead={true} status="SUCCEEDED" />);
    await user.click(screen.getByRole("button", { name: "트레이스 보기" }));
    expect(await screen.findByText(/저장된 트레이스가 없습니다/)).toBeInTheDocument();
    unmount();

    traceMock.mockRejectedValueOnce(new Error("network"));
    render(<BuildTraceEntry buildId="b2" hasTrace={true} canRead={true} status="SUCCEEDED" />);
    await user.click(screen.getByRole("button", { name: "트레이스 보기" }));
    await waitFor(() => expect(screen.getByText(/트레이스를 불러오지 못했습니다/)).toBeInTheDocument());
  });
});

describe("a loaded trace", () => {
  it("has no alert or status role and no aria-live anywhere", () => {
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);
    expect(screen.getByText("플랫폼이 측정한 사실")).toBeInTheDocument();
    expectNoLiveRegion(baseElement);
  });

  it("leads with the measured section and puts the agent-claim label after it", () => {
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);
    const headings = Array.from(baseElement.querySelectorAll("h4")).map((h) => h.textContent);
    expect(headings).toEqual(["플랫폼이 측정한 사실", "에이전트 작성 · 플랫폼이 확인하지 않은 내용"]);
    expect(screen.getByText(/호출 1회 · 토큰 15 · 비용 \$0\.0100/)).toBeInTheDocument();
  });

  it("collapses every call, every diff file and the log by default", () => {
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);
    const details = Array.from(baseElement.querySelectorAll("details"));
    // one call, one diff file, the log
    expect(details).toHaveLength(3);
    for (const d of details) expect(d.open).toBe(false);
  });

  it("opens a call from its summary by the keyboard", async () => {
    const user = userEvent.setup();
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);
    const summary = screen.getByText(/^호출 1 · 요청 1건/);
    expect(summary.closest("details")!.open).toBe(false);
    summary.focus();
    await user.click(summary);
    expect(summary.closest("details")!.open).toBe(true);
    expect(baseElement.querySelectorAll("details[open]")).toHaveLength(1);
  });

  it("words calls as requested and results as reported, never ran or returned", () => {
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);
    expect(screen.getByText("도구 호출 요청함")).toBeInTheDocument();
    expect(screen.getByText("도구 결과 보고됨")).toBeInTheDocument();
    expect(baseElement.textContent).not.toMatch(/실행됨|반환됨/);
  });

  it("renders agent text, a tool input, a path, a diff line and a log line as text with visible markers", () => {
    const { baseElement } = render(<BuildTraceView trace={TRACE} />);

    expect(baseElement.querySelector("a")).toBeNull();
    expect(baseElement.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned3?: number }).__pwned3).toBeUndefined();
    // The hostile text is one text node inside a code element: not parsed, not linked.
    const assistant = Array.from(baseElement.querySelectorAll("code")).filter((c) => c.textContent === HOSTILE);
    expect(assistant).toHaveLength(1);
    // The override never reaches the DOM; its marker does, in each of the four places.
    expect(baseElement.textContent).not.toContain(RLO);
    const markers = (baseElement.textContent ?? "").split("⟨U+202E⟩").length - 1;
    expect(markers).toBe(5); // path, diff line, tool input, tool result, log
    expect(screen.getByText('{"command":"cat a⟨U+202E⟩b"}')).toBeInTheDocument();
    expect(screen.getByText("report⟨U+202E⟩txt.py")).toBeInTheDocument();
  });

  it("names the diff's absence and offers no file rows for a build that never reached publish", () => {
    const t: BuildTrace = {
      ...TRACE,
      measured: { ...TRACE.measured, diff: { state: "not_reached", files: [], files_total: 0, truncated: false } },
    };
    const { baseElement } = render(<BuildTraceView trace={t} />);
    expect(screen.getAllByText("이 빌드는 게시 전에 끝나 diff가 없습니다")).toHaveLength(2);
    // only the call and the log remain
    expect(baseElement.querySelectorAll("details")).toHaveLength(2);
  });
});

describe("the timeline", () => {
  const BUILD: TimelineEntry = {
    kind: "build",
    ts: "2026-10-10T00:00:00Z",
    data: { id: "b1", type: "create", status: "SUCCEEDED", diff_ref: null, has_trace: true },
  };

  it("offers the trace entry point only when told the viewer may read it", () => {
    const { rerender } = render(<Timeline entries={[BUILD]} />);
    expect(screen.queryByRole("button", { name: "트레이스 보기" })).toBeNull();
    expect(screen.queryByText(/트레이스는 저장되어 있지 않습니다/)).toBeNull();

    rerender(<Timeline entries={[BUILD]} canReadTrace={true} />);
    expect(screen.getByRole("button", { name: "트레이스 보기" })).toBeInTheDocument();
  });

  it("offers nothing for a build whose flag is false and which is still running", () => {
    const running = { ...BUILD, data: { ...BUILD.data, status: "RUNNING", has_trace: false } };
    render(<Timeline entries={[running]} canReadTrace={true} />);
    expect(screen.queryByRole("button", { name: "트레이스 보기" })).toBeNull();
    expect(screen.queryByText(/트레이스는 저장되어 있지 않습니다/)).toBeNull();
  });
});

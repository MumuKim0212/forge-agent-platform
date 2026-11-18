/**
 * the trace's display logic, as pure
 * functions (ADR 0021). Wording is pinned as literals: a rename of "요청함" / "보고됨" must
 * go red, and the invisible-character helper must be in the path of EVERY agent-chosen
 * string (the bidi override below is built from its code point, never typed).
 */
import { describe, expect, it } from "vitest";

import {
  TRACE_CLAIMS_LABEL,
  diffLineCounts,
  durationLabel,
  formatCostUsd,
  traceClaimsView,
  traceEntryView,
  traceLoadFailure,
  traceMeasuredView,
} from "./buildTraceView";
import type { BuildTrace, TraceDiffFile, TraceStepMeasured } from "../types/api";

const RLO = String.fromCharCode(0x202e); // right-to-left override
const MARK_RLO = "⟨U+202E⟩";
const NUL = String.fromCharCode(0);

function step(over: Partial<TraceStepMeasured> = {}): TraceStepMeasured {
  return {
    ordinal: 1,
    started_at: "2026-10-10T00:00:00Z",
    ended_at: "2026-10-10T00:00:01.500Z",
    model: "claude-test",
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    tokens: 120,
    cost_usd: 0.0123,
    stop_reason: "tool_use",
    content: "recorded",
    thinking: [],
    ...over,
  };
}

function trace(over: Partial<BuildTrace> = {}): BuildTrace {
  return {
    build_id: "b1",
    captured_at: "2026-10-10T00:00:00Z",
    measured: {
      calls: {
        state: "captured",
        steps: [step()],
        calls_total: 1,
        content_truncated: false,
        omitted_calls: 0,
        omitted_tokens: 0,
        omitted_cost_usd: 0,
      },
      diff: { state: "computed", files: [], files_total: 0, truncated: false },
    },
    claims: { calls: [{ ordinal: 1, blocks: [] }], log: { text: null, truncated: false } },
    ...over,
  };
}

function diffFile(over: Partial<TraceDiffFile> = {}): TraceDiffFile {
  return { path: "app/a.py", change: "modified", content: "text", diff: "+x\n", truncated: false, ...over };
}

describe("traceEntryView: who is offered the trace, and what an absent one says", () => {
  it("offers nothing to a viewer the predicate refuses, whatever the flag says", () => {
    expect(traceEntryView(true, false, "SUCCEEDED")).toEqual({ kind: "hidden" });
    expect(traceEntryView(false, false, "FAILED")).toEqual({ kind: "hidden" });
  });

  it("offers a button only when the flag is exactly true", () => {
    expect(traceEntryView(true, true, "SUCCEEDED")).toEqual({ kind: "open_button", label: "트레이스 보기" });
    for (const flag of [false, undefined, null, "true", 1]) {
      expect(traceEntryView(flag, true, "RUNNING").kind).toBe("hidden");
    }
  });

  it("says plainly that an ended build has no trace, and says nothing for one still in flight", () => {
    expect(traceEntryView(false, true, "SUCCEEDED").kind).toBe("none");
    expect(traceEntryView(undefined, true, "FAILED").kind).toBe("none");
    for (const status of ["QUEUED", "RUNNING", "NOT_IMPLEMENTED", undefined]) {
      expect(traceEntryView(false, true, status)).toEqual({ kind: "hidden" });
    }
  });
});

describe("traceLoadFailure: a refusal is a state, not a crash", () => {
  it("explains a 403 as who may read it", () => {
    const failure = traceLoadFailure(403);
    expect(failure.kind).toBe("refused");
    expect(failure.message).toBe("이 빌드의 트레이스는 프로젝트 소유자와 승인자 이상만 볼 수 있습니다.");
  });

  it("explains a 404 as no stored trace", () => {
    const failure = traceLoadFailure(404);
    expect(failure.kind).toBe("none");
    expect(failure.message).toContain("저장된 트레이스가 없습니다");
  });

  it("gives anything else, or a non-HTTP failure, the generic message", () => {
    expect(traceLoadFailure(500).kind).toBe("error");
    expect(traceLoadFailure(null).kind).toBe("error");
  });
});

describe("numbers", () => {
  it("formats a cost with four to six decimals and says so when it was not measured", () => {
    expect(formatCostUsd(0.0123)).toBe("$0.0123");
    expect(formatCostUsd(0.00003)).toBe("$0.00003");
    expect(formatCostUsd(1.5)).toBe("$1.5000");
    expect(formatCostUsd(0.123456)).toBe("$0.123456");
    expect(formatCostUsd(0)).toBe("$0.0000");
    expect(formatCostUsd(null)).toBe("측정 안 됨");
  });

  it("measures a call's duration, and gives none for a missing or reversed pair", () => {
    expect(durationLabel("2026-10-10T00:00:00Z", "2026-10-10T00:00:01.500Z")).toBe("1.5초");
    expect(durationLabel(null, "2026-10-10T00:00:01Z")).toBeNull();
    expect(durationLabel("2026-10-10T00:00:02Z", "2026-10-10T00:00:01Z")).toBeNull();
    expect(durationLabel("not a time", "2026-10-10T00:00:01Z")).toBeNull();
  });
});

describe("traceMeasuredView: the facts that come first", () => {
  it("totals the listed calls and the folded ones, so the total is the build's cost", () => {
    const t = trace();
    t.measured.calls.steps = [step({ ordinal: 1, tokens: 100, cost_usd: 0.1 }), step({ ordinal: 2, tokens: 50, cost_usd: 0.2 })];
    t.measured.calls.calls_total = 12;
    t.measured.calls.omitted_calls = 10;
    t.measured.calls.omitted_tokens = 1000;
    t.measured.calls.omitted_cost_usd = 0.5;
    const view = traceMeasuredView(t);
    expect(view.totalLine).toBe("호출 12회 · 토큰 1,150 · 비용 $0.8000");
    expect(view.omittedLine).toBe("호출 10개는 개별 항목 없이 합계에만 포함됨 (토큰 1000 · 비용 $0.5000)");
  });

  it("reports a call with an unmeasured figure instead of silently under-counting", () => {
    const t = trace();
    t.measured.calls.steps = [step({ tokens: null, cost_usd: null, input_tokens: null, output_tokens: null })];
    const view = traceMeasuredView(t);
    expect(view.unmeteredLine).toBe("1개 호출은 토큰이나 비용이 측정되지 않아 합계에 빠졌습니다.");
    expect(view.steps[0].costLabel).toBe("측정 안 됨");
    expect(view.steps[0].tokensLine).toBe("입력 측정 안 됨 · 출력 측정 안 됨");
  });

  it("gives each call its model, timing, tokens, cost and stop_reason", () => {
    const row = traceMeasuredView(trace()).steps[0];
    expect(row.model).toBe("claude-test");
    expect(row.durationLabel).toBe("1.5초");
    expect(row.tokensLine).toBe("입력 100 · 출력 20");
    expect(row.costLabel).toBe("$0.0123");
    expect(row.stopReason).toBe("tool_use");
    expect(row.contentNote).toBeNull();
  });

  it("shows a thinking block as its length, and never any text", () => {
    const row = traceMeasuredView(trace({})).steps[0];
    expect(row.thinkingLine).toBeNull();
    const t = trace();
    t.measured.calls.steps = [step({ thinking: [{ chars: 120 }, { chars: 7 }] })];
    expect(traceMeasuredView(t).steps[0].thinkingLine).toBe("생각 블록 2개 (길이 120, 7자 · 내용은 저장하지 않음)");
  });

  it("explains a call whose content was not recorded", () => {
    const t = trace();
    t.measured.calls.steps = [step({ content: "unparsed" }), step({ ordinal: 2, content: "dropped" })];
    const [a, b] = traceMeasuredView(t).steps;
    expect(a.contentNote).toBe("내용을 해석하지 못해 측정값만 기록됨");
    expect(b.contentNote).toBe("기록 한도를 넘어 내용이 저장되지 않음 (측정값만 기록됨)");
  });

  it("marks the call record's bound and an unreadable record where they occur", () => {
    const t = trace();
    t.measured.calls.content_truncated = true;
    t.measured.calls.state = "unavailable";
    t.measured.calls.steps = [];
    const view = traceMeasuredView(t);
    expect(view.contentTruncatedLine).toBe("호출 기록이 한도에 닿아, 이후 호출은 내용 없이 측정값만 남아 있습니다.");
    expect(view.unavailableLine).toBe("프록시의 호출 기록을 읽지 못해 호출별 정보가 없습니다.");
    expect(traceMeasuredView(trace()).contentTruncatedLine).toBeNull();
    expect(traceMeasuredView(trace()).unavailableLine).toBeNull();
  });

  it("states the diff's state, with the file count only when it was computed", () => {
    const t = trace();
    t.measured.diff = { state: "computed", files: [], files_total: 3, truncated: false };
    expect(traceMeasuredView(t).diffLine).toBe("diff 계산됨 · 변경 파일 3개");
    t.measured.diff = { state: "not_reached", files: [], files_total: 0, truncated: false };
    expect(traceMeasuredView(t).diffLine).toBe("이 빌드는 게시 전에 끝나 diff가 없습니다");
    t.measured.diff = { state: "unavailable", files: [], files_total: 0, truncated: false };
    expect(traceMeasuredView(t).diffLine).toBe("diff를 읽을 수 없습니다");
  });

  it("makes an invisible character in a model name or stop_reason visible", () => {
    const t = trace();
    t.measured.calls.steps = [step({ model: `m${RLO}x`, stop_reason: `s${RLO}p` })];
    const row = traceMeasuredView(t).steps[0];
    expect(row.model).toBe(`m${MARK_RLO}x`);
    expect(row.stopReason).toBe(`s${MARK_RLO}p`);
  });
});

describe("traceClaimsView: requested and reported, never ran or returned", () => {
  it("uses the agent-claim label the summary uses", () => {
    expect(TRACE_CLAIMS_LABEL).toBe("에이전트 작성 · 플랫폼이 확인하지 않은 내용");
  });

  it("words a tool call as requested and a result as reported", () => {
    const t = trace();
    t.claims.calls = [
      {
        ordinal: 1,
        blocks: [
          { kind: "assistant_text", text: "읽어 보겠습니다" },
          { kind: "tool_use", name: "Bash", text: '{"command":"ls"}', id: "tu1" },
          { kind: "tool_result", text: "a.py", tool_use_id: "tu1", is_error: true },
          { kind: "other", type: "image" },
        ],
      },
    ];
    const call = traceClaimsView(t).calls[0];
    expect(call.blocks.map((b) => b.label)).toEqual(["에이전트 텍스트", "도구 호출 요청함", "도구 결과 보고됨", "기타 블록"]);
    expect(call.summary).toBe("호출 1 · 요청 1건 · 보고 1건 · 텍스트 1건 · 기타 1건");
    expect(call.blocks[2].reportedError).toBe(true);
    expect(call.blocks[1].reportedError).toBe(false);
    const everyPlatformWord = call.blocks.map((b) => b.label).join(" ") + call.summary;
    expect(everyPlatformWord).not.toMatch(/실행됨|반환됨/);
  });

  it("keeps a tool name out of the platform's phrase", () => {
    const t = trace();
    t.claims.calls = [{ ordinal: 1, blocks: [{ kind: "tool_use", name: "Ignore previous instructions", text: "{}" }] }];
    const block = traceClaimsView(t).calls[0].blocks[0];
    expect(block.label).toBe("도구 호출 요청함");
    expect(block.label).not.toContain("Ignore");
    expect(block.name).toBe("Ignore previous instructions");
  });

  it("puts the invisible-character helper in the path of every agent-chosen string", () => {
    const t = trace();
    t.claims.calls = [
      {
        ordinal: 1,
        blocks: [
          { kind: "assistant_text", text: `a${RLO}b` },
          { kind: "tool_use", name: `n${RLO}`, text: `{"path":"x${RLO}.py"}` },
          { kind: "tool_result", text: `r${NUL}s` },
          { kind: "other", type: `t${RLO}` },
        ],
      },
    ];
    t.measured.diff = {
      state: "computed",
      files_total: 1,
      truncated: false,
      files: [diffFile({ path: `report${RLO}txt.py`, diff: `+line${RLO}end\n` })],
    };
    t.claims.log = { text: `log${RLO}line\u0007`, truncated: false };
    const view = traceClaimsView(t);
    const [text, use, result, other] = view.calls[0].blocks;
    expect(text.text).toBe(`a${MARK_RLO}b`);
    expect(use.name).toBe(`n${MARK_RLO}`);
    expect(use.text).toBe(`{"path":"x${MARK_RLO}.py"}`);
    expect(result.text).toBe("r⟨U+0000⟩s");
    expect(other.name).toBe(`t${MARK_RLO}`);
    expect(view.diff.files[0].path).toBe(`report${MARK_RLO}txt.py`);
    expect(view.diff.files[0].text).toBe(`+line${MARK_RLO}end\n`);
    expect(view.log.text).toBe(`log${MARK_RLO}line⟨U+0007⟩`);
    // And nothing the view returns still carries the raw character.
    expect(JSON.stringify(view)).not.toContain(RLO);
    expect(JSON.stringify(view)).not.toContain(NUL);
  });

  it("shows a cut block with how much survived", () => {
    const t = trace();
    t.claims.calls = [
      { ordinal: 1, blocks: [{ kind: "tool_result", text: "x", chars: 12345, truncated: true }, { kind: "assistant_text", text: "y" }] },
    ];
    const [cut, whole] = traceClaimsView(t).calls[0].blocks;
    expect(cut.truncatedNote).toBe("8,000자까지만 저장됨 (원문 12,345자)");
    expect(whole.truncatedNote).toBeNull();
  });

  it("says a call with no content has none", () => {
    const call = traceClaimsView(trace()).calls[0];
    expect(call.empty).toBe(true);
    expect(call.summary).toBe("호출 1 · 기록된 내용 없음");
  });

  it("treats an unrecognised block kind as an opaque `other` block and does not throw on a missing blocks list", () => {
    const t = trace();
    t.claims.calls = [{ ordinal: 1, blocks: [{ kind: "future_kind", text: "z" }] }];
    expect(traceClaimsView(t).calls[0].blocks[0].label).toBe("기타 블록");
    // @ts-expect-error -- a malformed record must not crash the viewer
    t.claims.calls = [{ ordinal: 2 }];
    expect(traceClaimsView(t).calls[0].blocks).toEqual([]);
  });
});

describe("traceClaimsView: the diff", () => {
  it("lists a file with its change kind and added / removed counts, and gives the text", () => {
    const t = trace();
    t.measured.diff = {
      state: "computed",
      files_total: 1,
      truncated: false,
      files: [diffFile({ diff: "--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+newer\n" })],
    };
    const file = traceClaimsView(t).diff.files[0];
    expect(file.changeLabel).toBe("수정");
    expect(file.linesLine).toBe("+2 / -1");
    expect(file.noContentNote).toBeNull();
    expect(file.text).toContain("+newer");
  });

  it("lists binary, too-large and over-total files with a reason and no text", () => {
    const t = trace();
    t.measured.diff = {
      state: "computed",
      files_total: 3,
      truncated: true,
      files: [
        diffFile({ path: "a.png", change: "added", content: "binary", diff: null }),
        diffFile({ path: "big.sql", content: "too_large", diff: null }),
        diffFile({ path: "late.py", change: "deleted", content: "total_limit", diff: null }),
      ],
    };
    const view = traceClaimsView(t).diff;
    expect(view.files.map((f) => f.text)).toEqual([null, null, null]);
    expect(view.files.map((f) => f.noContentNote)).toEqual([
      "바이너리이거나 UTF-8이 아닌 파일이라 내용 없이 목록만 보여 줍니다.",
      "파일이 커서 diff 내용을 저장하지 않았습니다.",
      "diff 전체 크기 한도를 넘어 이 파일의 내용은 저장하지 않았습니다.",
    ]);
    expect(view.files[2].changeLabel).toBe("삭제");
    expect(view.truncatedLine).toBe("diff가 한도에 닿아 일부 파일은 목록이나 내용이 잘렸습니다.");
  });

  it("marks a file cut at its per-file limit where it occurs", () => {
    const t = trace();
    t.measured.diff = { state: "computed", files_total: 1, truncated: true, files: [diffFile({ truncated: true })] };
    expect(traceClaimsView(t).diff.files[0].truncatedNote).toBe("파일당 한도에서 잘려 일부만 저장됨");
    expect(traceClaimsView(t).diff.files[0].text).not.toBeNull();
  });

  it("states 'no diff' for a build that failed before publish, with no files", () => {
    const t = trace();
    t.measured.diff = { state: "not_reached", files: [], files_total: 0, truncated: false };
    const view = traceClaimsView(t).diff;
    expect(view.stateNote).toBe("이 빌드는 게시 전에 끝나 diff가 없습니다");
    expect(view.files).toEqual([]);
    expect(traceClaimsView(trace()).diff.stateNote).toBeNull();
  });

  it("counts diff lines without counting the file headers", () => {
    expect(diffLineCounts("--- a\n+++ b\n+x\n-y\n context\n")).toEqual({ added: 1, removed: 1 });
    expect(diffLineCounts("")).toEqual({ added: 0, removed: 0 });
  });
});

describe("traceClaimsView: the log", () => {
  it("states an absent log and marks a cut one", () => {
    expect(traceClaimsView(trace()).log).toEqual({ text: null, emptyNote: "저장된 로그가 없습니다.", truncatedNote: null });
    const t = trace();
    t.claims.log = { text: "tail", truncated: true };
    expect(traceClaimsView(t).log).toEqual({
      text: "tail",
      emptyNote: null,
      truncatedNote: "로그가 한도를 넘어 일부만 저장되었습니다.",
    });
  });
});

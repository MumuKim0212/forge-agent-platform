/**
 * The engine panel's decisions (ADR 0021: logic, not rendering).
 *
 * What an operator relies on, as a truth table: an unavailable engine offers no control, the
 * selected engine offers none either, the change-scope copy says "after" and "never a running
 * build", provenance never credits anyone when nobody chose, and a failure is explained by its
 * status rather than by the server text.
 */
import { describe, expect, it } from "vitest";

import type { AgentEngineSelection } from "../../types/api";
import {
  CHANGE_SCOPE_NOTICE,
  NO_MODEL_TEXT,
  SELECTED_BUT_UNAVAILABLE_NOTICE,
  UNAVAILABLE_REASON,
  changeSuccessMessage,
  engineChangeFailureMessage,
  engineRows,
  selectionProvenance,
} from "./agentEngine";

function selection(over: Partial<AgentEngineSelection> = {}): AgentEngineSelection {
  return {
    engines: [
      { engine: "anthropic", available: true, model: null, image: "forge-agent-sandbox:latest", selected: true },
      { engine: "openai", available: true, model: "gpt-5-mini", image: "forge-agent-sandbox-openai:latest", selected: false },
    ],
    selected: "anthropic",
    source: "default",
    set_by_id: null,
    set_by_name: null,
    set_at: null,
    ...over,
  };
}

const fmt = (iso: string) => `<${iso}>`;

describe("engineRows", () => {
  it("offers a control only for an available engine that is not already selected", () => {
    const [anthropic, openai] = engineRows(selection());

    expect(anthropic.canSelect).toBe(false); // selected already
    expect(openai.canSelect).toBe(true);
  });

  it("offers no control for an unavailable engine, and says why in the specified words", () => {
    const rows = engineRows(
      selection({
        engines: [
          { engine: "anthropic", available: true, model: null, image: "a", selected: true },
          { engine: "openai", available: false, model: "gpt-5-mini", image: "o", selected: false },
        ],
      }),
    );

    expect(rows[1].canSelect).toBe(false);
    expect(rows[1].available).toBe(false);
    expect(rows[1].availabilityText).toBe("키가 설정되지 않음");
    expect(UNAVAILABLE_REASON).toBe("키가 설정되지 않음");
  });

  it("offers no control when neither engine is available", () => {
    const rows = engineRows(
      selection({
        engines: [
          { engine: "anthropic", available: false, model: null, image: "a", selected: true },
          { engine: "openai", available: false, model: "m", image: "o", selected: false },
        ],
      }),
    );

    expect(rows.map((r) => r.canSelect)).toEqual([false, false]);
  });

  it("flags a selected engine whose key is gone, since its builds will fail", () => {
    const rows = engineRows(
      selection({
        engines: [
          { engine: "anthropic", available: true, model: null, image: "a", selected: false },
          { engine: "openai", available: false, model: "m", image: "o", selected: true },
        ],
      }),
    );

    expect(rows[1].selectedButUnavailable).toBe(true);
    expect(rows[0].selectedButUnavailable).toBe(false);
    expect(SELECTED_BUT_UNAVAILABLE_NOTICE).toContain("다른 엔진으로 대신 실행하지 않습니다");
  });

  it("shows the configured model, or says there is none, and the image name", () => {
    const [anthropic, openai] = engineRows(selection());

    expect(openai.modelText).toBe("gpt-5-mini");
    expect(anthropic.modelText).toBe(NO_MODEL_TEXT);
    expect(openai.image).toBe("forge-agent-sandbox-openai:latest");
  });

  it("names the engines, and treats anything but a boolean true as not available and not selected", () => {
    const rows = engineRows(
      selection({
        engines: [
          { engine: "openai", available: "yes" as unknown as boolean, model: null, image: "o", selected: 1 as unknown as boolean },
        ],
      }),
    );

    expect(rows[0].label).toBe("OpenAI");
    expect(rows[0].available).toBe(false);
    expect(rows[0].selected).toBe(false);
    expect(rows[0].canSelect).toBe(false);
  });

  it("carries no key field at all: the view keys are exactly these", () => {
    const keys = Object.keys(engineRows(selection())[0]).sort();

    expect(keys).toEqual(
      ["available", "availabilityText", "canSelect", "engine", "image", "label", "modelText", "selected", "selectedButUnavailable"].sort(),
    );
  });
});

describe("the change-scope copy", () => {
  it("says the change applies to builds started afterwards and never to a running one", () => {
    expect(CHANGE_SCOPE_NOTICE).toContain("이후에 시작되는");
    expect(CHANGE_SCOPE_NOTICE).toContain("이미 실행 중인 빌드");
    expect(CHANGE_SCOPE_NOTICE).toContain("바뀌지 않습니다");
  });

  it("is repeated in the success message", () => {
    expect(changeSuccessMessage(selection({ selected: "openai" }))).toBe(
      "에이전트 엔진을 변경했습니다: OpenAI. 이후 시작되는 빌드부터 적용됩니다.",
    );
  });
});

describe("selectionProvenance", () => {
  it("names who selected the engine and when, for a stored selection", () => {
    const text = selectionProvenance(
      selection({ source: "stored", set_by_name: "김관리", set_at: "2026-10-12T01:02:03Z" }),
      fmt,
    );

    expect(text).toBe("선택한 사람: 김관리 · 선택 시각: <2026-10-12T01:02:03Z>");
  });

  it("does not invent a name for a stored selection whose user is gone", () => {
    const text = selectionProvenance(
      selection({ source: "stored", set_by_name: null, set_at: "2026-10-12T01:02:03Z" }),
      fmt,
    );

    expect(text).toBe("선택한 사람: 알 수 없는 사용자 · 선택 시각: <2026-10-12T01:02:03Z>");
  });

  it("says nothing, and credits nobody, when nothing is stored", () => {
    expect(selectionProvenance(selection({ source: "default" }), fmt)).toBeNull();
    // Even a default selection that carries a name and a time (a malformed answer) credits no one.
    expect(
      selectionProvenance(selection({ source: "default", set_by_name: "김관리", set_at: "2026-10-12T01:02:03Z" }), fmt),
    ).toBeNull();
  });

  it("says it does not know, for a source it has never heard of", () => {
    expect(selectionProvenance(selection({ source: "mystery" }), fmt)).toBe("선택 출처를 알 수 없습니다.");
  });

  it("shows a time it cannot parse as received rather than Invalid Date", () => {
    const text = selectionProvenance(selection({ source: "stored", set_by_name: "김관리", set_at: "not a date" }));

    expect(text).toContain("not a date");
    expect(text).not.toContain("Invalid Date");
  });

  it("shows a person name as plain text, unaltered", () => {
    const hostile = "<img src=x onerror=1> 김관리";

    expect(selectionProvenance(selection({ source: "stored", set_by_name: hostile, set_at: null }), fmt)).toBe(
      `선택한 사람: ${hostile}`,
    );
  });
});

describe("engineChangeFailureMessage", () => {
  it("explains 422 (unknown or unavailable engine) and 403 each in its own words", () => {
    const m422 = engineChangeFailureMessage(422);
    const m403 = engineChangeFailureMessage(403);

    expect(m422).toContain("선택할 수 없습니다");
    expect(m403).toContain("admin만");
    expect(m422).not.toBe(m403);
  });

  it("gives every other status the generic message, which says the old selection stands", () => {
    for (const status of [400, 404, 500, 502]) {
      expect(engineChangeFailureMessage(status)).toBe("에이전트 엔진 변경에 실패했습니다. 기존 선택은 그대로입니다.");
    }
  });
});

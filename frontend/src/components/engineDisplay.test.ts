import { describe, expect, it } from "vitest";

import { buildEngineLine, engineLabel } from "./engineDisplay";

describe("engineLabel", () => {
  it("names the two engines by the wire values the backend sends", () => {
    // Literal keys: `anthropic` / `openai` cross the boundary (services/engines.ENGINES).
    expect(engineLabel("anthropic")).toBe("Anthropic (Claude)");
    expect(engineLabel("openai")).toBe("OpenAI");
  });

  it("shows a value it does not know as sent, never as a known engine", () => {
    expect(engineLabel("gemini")).toBe("gemini");
    expect(engineLabel("toString")).toBe("toString");
    expect(engineLabel("")).toBe("");
  });
});

describe("buildEngineLine", () => {
  it("states the engine and the configured model", () => {
    expect(buildEngineLine("openai", "gpt-5-mini")).toBe("에이전트 엔진: OpenAI · 설정 모델: gpt-5-mini");
  });

  it("names only the engine when no model was configured (the Claude runner passes none)", () => {
    expect(buildEngineLine("anthropic", null)).toBe("에이전트 엔진: Anthropic (Claude)");
    expect(buildEngineLine("anthropic", "")).toBe("에이전트 엔진: Anthropic (Claude)");
  });

  it("shows nothing for a build that recorded no engine", () => {
    expect(buildEngineLine(null, null)).toBeNull();
    expect(buildEngineLine(undefined, "gpt-5-mini")).toBeNull();
    expect(buildEngineLine("", "gpt-5-mini")).toBeNull();
  });

  it("ignores a value that is not a string rather than printing it", () => {
    expect(buildEngineLine(7 as unknown as string, null)).toBeNull();
    expect(buildEngineLine("openai", 7 as unknown as string)).toBe("에이전트 엔진: OpenAI");
  });
});

// how an agent engine is named on screen, in the admin console's engine
// panel and on a timeline build entry. Pure functions (ADR 0021).
//
// The engine and model on a build are MEASURED facts the platform recorded when the worker
// claimed it, not agent claims. The model is the CONFIGURED one; the trace's per-call model
// is what the API reported and can differ, so the build line says "설정 모델".

const ENGINE_LABELS: Record<string, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI",
};

/** The display name of an engine; a value this client does not know is shown as sent. */
export function engineLabel(engine: string): string {
  return Object.prototype.hasOwnProperty.call(ENGINE_LABELS, engine) ? ENGINE_LABELS[engine] : engine;
}

/**
 * `에이전트 엔진: OpenAI · 설정 모델: gpt-5-mini` for a timeline build entry.
 * Null when the build recorded no engine (a build from before engines were recorded):
 * nothing is shown rather than a guess. With no model (the Claude runner passes none)
 * only the engine is named.
 */
export function buildEngineLine(
  engine: string | null | undefined,
  model: string | null | undefined,
): string | null {
  if (typeof engine !== "string" || engine === "") return null;
  const line = `에이전트 엔진: ${engineLabel(engine)}`;
  return typeof model === "string" && model !== "" ? `${line} · 설정 모델: ${model}` : line;
}

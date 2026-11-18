import { api } from "./client";
import type { AgentEngineSelection, AgentEngineUpdateInput } from "../types/api";

/**
 * The agent engine selection (`routers/agent_engine.py`). Both verbs are `admin`-only.
 * It lives outside `/api/console`: that router is read-only by construction, so the one write
 * here follows the quota and role pattern and has its own audited endpoint.
 *
 * `select` answers 422 for an engine that is unknown or not available (its key is not
 * configured); the console explains that rather than showing it raw.
 */
export const AGENT_ENGINE_PATH = "/platform/agent-engine";

export const agentEngineApi = {
  get: () => api.get<AgentEngineSelection>(AGENT_ENGINE_PATH),
  select: (input: AgentEngineUpdateInput) => api.patch<AgentEngineSelection>(AGENT_ENGINE_PATH, input),
};

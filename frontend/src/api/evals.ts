import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type { EvalRun, EvalRunDetail, EvalStatus } from "../types/api";

/**
 * The eval pipeline's endpoints (`routers/evals.py`). All three are `admin`-only and sit
 * outside `/api/console`: that router is read-only by construction, so the one write here (starting
 * a run, which spends real money) has its own audited endpoint, like the engine switch.
 *
 * `status` is the pre-run read (budget, executor, selected engine).
 *
 * `startRun` answers 409 while a run is in progress and 422 when the sandbox executor is inactive or
 * the selected engine is unavailable; the console words both in Korean (`evalRuns.ts`).
 */
export const EVAL_RUNS_PATH = "/evals/runs";

export const EVAL_STATUS_PATH = "/evals/status";

export const evalsApi = {
  status: (options?: RequestOptions) => api.get<EvalStatus>(EVAL_STATUS_PATH, options),
  listRuns: (options?: RequestOptions) => api.get<EvalRun[]>(EVAL_RUNS_PATH, options),
  getRun: (runId: string, options?: RequestOptions) =>
    api.get<EvalRunDetail>(`${EVAL_RUNS_PATH}/${pathSegment(runId)}`, options),
  startRun: () => api.post<EvalRunDetail>(EVAL_RUNS_PATH, {}),
};

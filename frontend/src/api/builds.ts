import { api } from "./client";
import { pathSegment } from "./path";
import type { BuildTrace } from "../types/api";

// one build's stored trace. 403 for anyone but the project's owner and
// `approver`+; 404 for a build that has none.
export const buildsApi = {
  trace: (buildId: string) => api.get<BuildTrace>(`/builds/${pathSegment(buildId)}/trace`),
};

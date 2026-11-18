import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type { Deployment } from "../types/api";

export const deploymentsApi = {
  list: (projectId: string, options?: RequestOptions) =>
    api.get<Deployment[]>(`/projects/${pathSegment(projectId)}/deployments`, options),
  confirm: (projectId: string, buildId: string) =>
    api.post<Deployment>(`/projects/${pathSegment(projectId)}/deployments`, { build_id: buildId }),
  rollback: (projectId: string) =>
    api.post<Deployment>(`/projects/${pathSegment(projectId)}/deployments/rollback`, {}),
  stop: (projectId: string) =>
    api.post<Deployment>(`/projects/${pathSegment(projectId)}/deployments/stop`, {}),
};

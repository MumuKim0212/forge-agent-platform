import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type {
  ChangeRequest,
  ChangeRequestCreateInput,
  Project,
  ProjectCreateInput,
  ProjectDetail,
  TimelineEntry,
} from "../types/api";

export const projectsApi = {
  list: (options?: RequestOptions) => api.get<Project[]>("/projects", options),
  create: (input: ProjectCreateInput) => api.post<ProjectDetail>("/projects", input),
  get: (id: string, options?: RequestOptions) =>
    api.get<ProjectDetail>(`/projects/${pathSegment(id)}`, options),
  timeline: (id: string, options?: RequestOptions) =>
    api.get<TimelineEntry[]>(`/projects/${pathSegment(id)}/timeline`, options),
  submitChangeRequest: (id: string, input: ChangeRequestCreateInput) =>
    api.post<ChangeRequest>(`/projects/${pathSegment(id)}/change-requests`, input),
  // `POST /projects/{id}/unarchive` (owner or admin; 400 when the project is not
  // archived). The response is the project without `deploy_controls`, so the page re-reads instead.
  unarchive: (id: string) => api.post<ProjectDetail>(`/projects/${pathSegment(id)}/unarchive`),
};

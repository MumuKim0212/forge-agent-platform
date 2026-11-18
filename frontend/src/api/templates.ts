import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type { Team, Template } from "../types/api";

export const templatesApi = {
  list: (options?: RequestOptions) => api.get<Template[]>("/templates", options),
  // the admin console's read-only template inspector. Template authoring
  // is a later slice, so there is no create/update verb here.
  get: (id: string) => api.get<Template>(`/templates/${pathSegment(id)}`),
};

export const teamsApi = {
  list: (options?: RequestOptions) => api.get<Team[]>("/teams", options),
};

// `usersApi.list()` (`GET /users`) was removed from here: `IdentityContext`
// was its only caller, and it now bootstraps from `GET /dev/identities` + `GET
// /users/me` instead. The role console wanted the directory again and it came
// back as `api/users.ts` rather than here — it belongs next to the role-grant
// verb, not next to templates and teams.


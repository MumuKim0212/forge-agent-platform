import { api } from "./client";
import type { DeploymentHealth, HostHealth, PlatformConfiguration } from "../types/api";

/**
 * The two console read APIs (`routers/console.py`). Both are `admin`-only
 * and both are **reads only** — there is deliberately no write on that router,
 * and there is none here either: deploy/rollback/stop stay on
 * `api/deployments.ts` and on the project detail page, where owner-or-admin
 * already applies.
 */
export const consoleApi = {
  deploymentHealth: () => api.get<DeploymentHealth>("/console/deployments"),
  configuration: () => api.get<PlatformConfiguration>("/console/configuration"),
  // worker lease age and the worker's latest host disk measurements.
  hostHealth: () => api.get<HostHealth>("/console/health"),
};

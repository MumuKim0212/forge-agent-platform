/**
 * What the project page's tabs and summary tiles decide, as named functions (ADR 0021: the
 * decision is tested, the rendering is not).
 *
 * The page is a summary header plus three tabs. Which tab is open is the page's own state; this file
 * says which tabs exist, which one opens first, where a finished change request takes the reader, and
 * what the two deployment tiles say. Nothing here reads a role or a status: it is layout and wording,
 * and the gates on the controls inside each tab are unchanged (`deployControls.ts`, `unarchive.ts`,
 * `changeRequest.ts`).
 */

/** The tab ids are also the suffix of the generated element ids that link a tab to its panel. */
export const PROJECT_TABS = [
  { id: "deploy", label: "배포" },
  { id: "change-request", label: "변경 요청" },
  { id: "timeline", label: "타임라인" },
] as const;

export type ProjectTabId = (typeof PROJECT_TABS)[number]["id"];

/** The tab shown when the page opens: the deployment controls and history. */
export const DEFAULT_PROJECT_TAB: ProjectTabId = "deploy";

/** How a change-request submit ended, as far as the tab choice is concerned. */
export type ChangeRequestOutcome = "submitted" | "failed";

/**
 * Which tab is open once a change-request submit has ended. A submit the backend accepted opens the
 * timeline, where the new entry is; a refused one stays where it is, on the form, beside the message
 * that explains the refusal and the text the person typed.
 */
export function tabAfterChangeRequest(current: ProjectTabId, outcome: ChangeRequestOutcome): ProjectTabId {
  return outcome === "submitted" ? "timeline" : current;
}

/** What the tile says when no deployment was ever recorded. */
export const NO_DEPLOYMENT_TEXT = "없음";

/** The shape of a deployment row these tiles read: its creation time, as the API serialises it. */
interface DeploymentStamp {
  created_at: string;
}

/**
 * The newest deployment record's time, or "없음". "Newest" is the largest `created_at`, not the first
 * row, so the tile does not depend on the order the list happens to arrive in. A row whose time does
 * not parse is skipped; when none parses, the tile says "없음" rather than "Invalid Date". Failed and
 * rolled-back attempts are records too, and count here as they do in the history beside it.
 */
export function lastDeploymentLabel(deployments: ReadonlyArray<DeploymentStamp>): string {
  let newest: Date | null = null;
  for (const deployment of deployments) {
    const at = new Date(deployment.created_at);
    if (Number.isNaN(at.getTime())) continue;
    if (newest === null || at.getTime() > newest.getTime()) newest = at;
  }
  return newest === null ? NO_DEPLOYMENT_TEXT : newest.toLocaleString();
}

/** The number of deployment records the page loaded. The list is the whole history (the endpoint
 * neither pages nor caps it), so this is the count of rows in "배포 이력". */
export function deploymentCountLabel(deployments: ReadonlyArray<unknown>): string {
  return `${deployments.length}회`;
}

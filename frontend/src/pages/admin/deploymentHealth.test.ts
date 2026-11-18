/**
 * The (active, latest) fold — the one place the console decides what a
 * project's deployment state is.
 *
 * The defect this pins is specific: `active_deployment` alone conflates
 * never-deployed, deploying-now, stopped, superseded and failed, so a fold that
 * ignores `latest_deployment` renders **every in-progress deploy as "not
 * deployed"** for its whole duration. The mirror-image defect is folding the
 * other way, where a project serving an old version reports plain "운영중" while a
 * newer deploy has just failed.
 */
import { describe, expect, it } from "vitest";

import {
  ATTENTION_STATES,
  DEPLOYMENT_HEALTH_COLORS,
  DEPLOYMENT_HEALTH_LABELS,
  DeploymentStatus,
  type DeploymentHealthState,
  attentionCount,
  deploymentHealthState,
  recentFailureNotice,
  servingCount,
  sortByAttention,
} from "./deploymentHealth";
import type { Deployment } from "../../types/api";

function deployment(id: string, status: string, failureReason: string | null = null): Deployment {
  return {
    id,
    project_id: "project-1",
    build_id: "build-1",
    confirmed_by_id: "user-1",
    rollback_of: null,
    status,
    failure_reason: failureReason,
    created_at: "2026-07-30T00:00:00Z",
    started_at: null,
    ended_at: null,
  };
}

const ACTIVE_ROW = deployment("dep-active", "ACTIVE");

describe("the DeploymentStatus values are a cross-service contract", () => {
  it("match the strings backend models/deployment.py stores", () => {
    // Literals, as in `roles.test.ts`. A frontend-only rename makes every
    // comparison below fall through to `UNKNOWN`, i.e. every project on the
    // console reads "상태 확인 필요" — a whole-screen regression that `tsc`
    // cannot see because all five are strings.
    expect(DeploymentStatus).toEqual({
      DEPLOYING: "DEPLOYING",
      ACTIVE: "ACTIVE",
      FAILED: "FAILED",
      SUPERSEDED: "SUPERSEDED",
      STOPPED: "STOPPED",
    });
  });
});

describe("deploymentHealthState — no active deployment is five situations", () => {
  const table: Array<{ latest: Deployment | null; expected: DeploymentHealthState; why: string }> = [
    { latest: null, expected: "NEVER_DEPLOYED", why: "no deployment has ever been confirmed" },
    {
      latest: deployment("dep-1", "DEPLOYING"),
      expected: "DEPLOYING",
      // The damaging one: this is the entire duration of every first deploy.
      why: "a deploy is running right now",
    },
    {
      latest: deployment("dep-1", "FAILED", "IMAGE_PULL_FAILED"),
      expected: "LAST_ATTEMPT_FAILED",
      why: "the last attempt broke",
    },
    { latest: deployment("dep-1", "STOPPED"), expected: "STOPPED", why: "deliberately stopped" },
    {
      latest: deployment("dep-1", "SUPERSEDED"),
      expected: "SUPERSEDED",
      why: "replaced by a later deployment",
    },
  ];

  for (const { latest, expected, why } of table) {
    it(`active=null, latest=${latest ? latest.status : "null"} -> ${expected} (${why})`, () => {
      expect(deploymentHealthState({ active_deployment: null, latest_deployment: latest })).toBe(
        expected,
      );
    });
  }

  it("gives all five situations distinct states — they are not collapsed", () => {
    const states = table.map(({ latest }) =>
      deploymentHealthState({ active_deployment: null, latest_deployment: latest }),
    );
    expect(new Set(states).size).toBe(table.length);
  });

  it("never reports an in-flight or failed deploy as never-deployed", () => {
    for (const status of [DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED]) {
      expect(
        deploymentHealthState({
          active_deployment: null,
          latest_deployment: deployment("dep-1", status),
        }),
      ).not.toBe("NEVER_DEPLOYED");
    }
  });
});

describe("deploymentHealthState — an active deployment does not hide the newer row", () => {
  it("is plain serving when the active row is also the latest row", () => {
    expect(
      deploymentHealthState({ active_deployment: ACTIVE_ROW, latest_deployment: ACTIVE_ROW }),
    ).toBe("SERVING");
  });

  it("says a new deploy is in progress while the old version still serves", () => {
    expect(
      deploymentHealthState({
        active_deployment: ACTIVE_ROW,
        latest_deployment: deployment("dep-new", "DEPLOYING"),
      }),
    ).toBe("SERVING_DEPLOY_IN_PROGRESS");
  });

  it("says the last attempt failed even though the service is up", () => {
    // The failure would otherwise be visible only if it happened to fall inside
    // the bounded recent-failure list.
    expect(
      deploymentHealthState({
        active_deployment: ACTIVE_ROW,
        latest_deployment: deployment("dep-new", "FAILED", "HEALTHCHECK_FAILED"),
      }),
    ).toBe("SERVING_LAST_ATTEMPT_FAILED");
  });

  it("still reports serving when the latest row is missing or uninteresting", () => {
    expect(deploymentHealthState({ active_deployment: ACTIVE_ROW, latest_deployment: null })).toBe(
      "SERVING",
    );
    expect(
      deploymentHealthState({
        active_deployment: ACTIVE_ROW,
        latest_deployment: deployment("dep-old", "SUPERSEDED"),
      }),
    ).toBe("SERVING");
  });
});

describe("deploymentHealthState — fail-honest on an unknown status", () => {
  it("resolves an unrecognised status to UNKNOWN, not to serving or never-deployed", () => {
    for (const status of ["ROLLING_BACK", "active", "", "PENDING"]) {
      const state = deploymentHealthState({
        active_deployment: null,
        latest_deployment: deployment("dep-1", status),
      });
      expect(state).toBe("UNKNOWN");
    }
  });
});

describe("the state labels", () => {
  const ALL_STATES: DeploymentHealthState[] = [
    "SERVING",
    "SERVING_DEPLOY_IN_PROGRESS",
    "SERVING_LAST_ATTEMPT_FAILED",
    "NEVER_DEPLOYED",
    "DEPLOYING",
    "LAST_ATTEMPT_FAILED",
    "STOPPED",
    "SUPERSEDED",
    "UNKNOWN",
  ];

  it("cover every state, with no state left rendering its enum name", () => {
    for (const state of ALL_STATES) {
      expect(DEPLOYMENT_HEALTH_LABELS[state], state).toBeTruthy();
      expect(DEPLOYMENT_HEALTH_LABELS[state]).not.toBe(state);
      expect(DEPLOYMENT_HEALTH_COLORS[state], state).toBeTruthy();
    }
  });

  it("has exactly these states and no others", () => {
    // Written out as literals so adding a state has to add its label and its
    // truth-table row deliberately, instead of inheriting a default.
    expect(Object.keys(DEPLOYMENT_HEALTH_LABELS).sort()).toEqual([...ALL_STATES].sort());
  });

  it("does not describe a project with no active deployment as serving", () => {
    expect(DEPLOYMENT_HEALTH_LABELS.NEVER_DEPLOYED).not.toBe(DEPLOYMENT_HEALTH_LABELS.SERVING);
    expect(DEPLOYMENT_HEALTH_LABELS.DEPLOYING).not.toBe(DEPLOYMENT_HEALTH_LABELS.NEVER_DEPLOYED);
  });
});

describe("recentFailureNotice — a bounded list must say it is bounded", () => {
  it("returns a notice naming the bound when the list is truncated", () => {
    const notice = recentFailureNotice({
      recent_failures_limit: 50,
      recent_failures_truncated: true,
    });
    expect(notice).not.toBeNull();
    expect(notice).toContain("50");
  });

  it("returns null when the list is complete", () => {
    expect(
      recentFailureNotice({ recent_failures_limit: 50, recent_failures_truncated: false }),
    ).toBeNull();
  });
});

describe("sortByAttention", () => {
  const failed = {
    project_id: "p-failed",
    project_name: "zeta",
    active_deployment: null,
    latest_deployment: deployment("d1", "FAILED", "IMAGE_BUILD_FAILED"),
  };
  const serving = {
    project_id: "p-serving",
    project_name: "alpha",
    active_deployment: ACTIVE_ROW,
    latest_deployment: ACTIVE_ROW,
  };
  const never = {
    project_id: "p-never",
    project_name: "beta",
    active_deployment: null,
    latest_deployment: null,
  };

  it("puts the broken project first even though its name sorts last", () => {
    expect(sortByAttention([serving, never, failed]).map((p) => p.project_id)).toEqual([
      "p-failed",
      "p-serving",
      "p-never",
    ]);
  });

  it("keeps every project — never-deployed ones are shown, not dropped", () => {
    // omitting them makes the view silently partial.
    expect(sortByAttention([serving, never, failed])).toHaveLength(3);
  });

  it("does not mutate its input", () => {
    const input = [serving, never, failed];
    sortByAttention(input);
    expect(input.map((p) => p.project_id)).toEqual(["p-serving", "p-never", "p-failed"]);
  });
});

describe("which states need attention: derived from the order projects are sorted in", () => {
  const FAILED = deployment("d-failed", "FAILED", "IMAGE_BUILD_FAILED");
  const DEPLOYING = deployment("d-deploying", "DEPLOYING");
  // One project per state, built the way the API would present it.
  const PROJECT_FOR: Record<DeploymentHealthState, { active_deployment: Deployment | null; latest_deployment: Deployment | null }> = {
    SERVING: { active_deployment: ACTIVE_ROW, latest_deployment: ACTIVE_ROW },
    SERVING_DEPLOY_IN_PROGRESS: { active_deployment: ACTIVE_ROW, latest_deployment: DEPLOYING },
    SERVING_LAST_ATTEMPT_FAILED: { active_deployment: ACTIVE_ROW, latest_deployment: FAILED },
    NEVER_DEPLOYED: { active_deployment: null, latest_deployment: null },
    DEPLOYING: { active_deployment: null, latest_deployment: DEPLOYING },
    LAST_ATTEMPT_FAILED: { active_deployment: null, latest_deployment: FAILED },
    STOPPED: { active_deployment: null, latest_deployment: deployment("d-stopped", "STOPPED") },
    SUPERSEDED: { active_deployment: null, latest_deployment: deployment("d-superseded", "SUPERSEDED") },
    UNKNOWN: { active_deployment: null, latest_deployment: deployment("d-weird", "SOMETHING_NEW") },
  };
  const ALL = (Object.keys(PROJECT_FOR) as DeploymentHealthState[]).map((state) => ({
    project_id: `p-${state}`,
    project_name: state.toLowerCase(),
    ...PROJECT_FOR[state],
  }));

  it("builds a project in each of the nine states (the fixture itself is right)", () => {
    for (const project of ALL) {
      expect(deploymentHealthState(project), project.project_id).toBe(project.project_id.slice(2));
    }
  });

  it("is exactly the five states an operator should look at (pinned once, as literals)", () => {
    expect([...ATTENTION_STATES].sort()).toEqual(
      [
        "DEPLOYING",
        "LAST_ATTEMPT_FAILED",
        "SERVING_DEPLOY_IN_PROGRESS",
        "SERVING_LAST_ATTEMPT_FAILED",
        "UNKNOWN",
      ].sort(),
    );
  });

  it("counts a project exactly when its state is one of those", () => {
    expect(attentionCount(ALL)).toBe(5);
    expect(attentionCount([])).toBe(0);
    expect(attentionCount([PROJECT_FOR.SERVING, PROJECT_FOR.NEVER_DEPLOYED, PROJECT_FOR.STOPPED, PROJECT_FOR.SUPERSEDED])).toBe(0);
    expect(attentionCount([PROJECT_FOR.UNKNOWN, PROJECT_FOR.UNKNOWN])).toBe(2);
  });

  it("ties the count to the sort: no settled project is sorted ahead of one that is counted", () => {
    const states = sortByAttention(ALL).map((project) => deploymentHealthState(project));
    const counted = attentionCount(ALL);

    for (const state of states.slice(0, counted)) expect(ATTENTION_STATES.has(state), state).toBe(true);
    for (const state of states.slice(counted)) expect(ATTENTION_STATES.has(state), state).toBe(false);
  });

  it("names only states that have a label, so it cannot hold a stale name", () => {
    for (const state of ATTENTION_STATES) expect(Object.keys(DEPLOYMENT_HEALTH_LABELS)).toContain(state);
  });
});

describe("servingCount (the 현재 운영 중 tile)", () => {
  const row = (id: string, status: string) => deployment(id, status);
  const serving = { active_deployment: ACTIVE_ROW };
  const notServing = (latest: Deployment | null) => ({ active_deployment: null, latest_deployment: latest });

  it("counts the projects that have an active deployment and no others", () => {
    const projects = [
      serving,
      serving,
      // serving an old version while a newer attempt failed: still up, still counted
      { active_deployment: ACTIVE_ROW, latest_deployment: row("d-failed", "FAILED") },
      notServing(null),
      notServing(row("d-deploying", "DEPLOYING")),
      notServing(row("d-failed-2", "FAILED")),
      notServing(row("d-stopped", "STOPPED")),
      notServing(row("d-superseded", "SUPERSEDED")),
    ];

    expect(servingCount(projects)).toBe(3);
  });

  it("is zero for no projects, and for projects that are all not serving", () => {
    expect(servingCount([])).toBe(0);
    expect(servingCount([notServing(null), notServing(row("d-stopped", "STOPPED"))])).toBe(0);
  });
});

/**
 * The project page's deploy controls are decided by named predicates over the backend's
 * `deploy_controls` (ADR 0021): a truth table each, plus the polling decision and the failure
 * wording. Nothing here re-derives a deploy rule; the backend's own agreement test
 * (`backend/tests/test_deploy_controls.py`) is what ties the fields to the actions.
 *
 * The refusal codes are CONTRACT STRINGS with the backend (`DeployRefusalCode`), so the sweep
 * below spells every one as a literal; a one-sided rename fails here instead of degrading to the
 * generic message.
 */
import { describe, expect, it } from "vitest";

import { ApiError, NETWORK_ERROR_COPY, NETWORK_ERROR_STATUS } from "../api/client";
import type { DeployControls } from "../types/api";
import {
  DeployRefusalCode,
  PROJECT_POLL_INTERVAL_MS,
  canOfferConfirm,
  canOfferRollback,
  canOfferStop,
  confirmBlockedMessage,
  confirmTargetBuildId,
  deployActionFailure,
  deployActionFailureMessage,
  deployFailureAfter,
  deployRefusalMessage,
  offersAnyDeployControl,
  shouldPollProject,
  shouldRefreshAfterDeployFailure,
  type DeployFailure,
} from "./deployControls";

function controls(overrides: Partial<DeployControls> = {}): DeployControls {
  return {
    confirmable_build_id: null,
    confirm_blocked_reason: "NO_SUCCESSFUL_BUILD",
    rollback_target_id: null,
    rollback_blocked_reason: "NO_ROLLBACK_TARGET",
    active_deployment_id: null,
    in_flight: false,
    ...overrides,
  };
}

/** What the backend sends for an error: the code as a prefix of the string `detail`. */
function refused(status: number, code: string): ApiError {
  const detail = `${code}: some English diagnostic from the server`;
  return new ApiError(status, detail, detail);
}

describe("the refusal codes are the backend's, spelled out", () => {
  it("pins every value (a rename on one side would otherwise degrade to the generic message)", () => {
    expect(DeployRefusalCode).toEqual({
      PROJECT_ARCHIVED: "PROJECT_ARCHIVED",
      DEPLOYMENT_IN_FLIGHT: "DEPLOYMENT_IN_FLIGHT",
      EVAL_PROJECT: "EVAL_PROJECT",
      POSTGRES_UNCONFIGURED: "POSTGRES_UNCONFIGURED",
      BUILD_NOT_SUCCEEDED: "BUILD_NOT_SUCCEEDED",
      NOT_DEPLOY_ELIGIBLE: "NOT_DEPLOY_ELIGIBLE",
      NOT_LATEST_SUCCESSFUL: "NOT_LATEST_SUCCESSFUL",
      IMAGE_MISSING: "IMAGE_MISSING",
      NO_ROLLBACK_TARGET: "NO_ROLLBACK_TARGET",
      NO_SUCCESSFUL_BUILD: "NO_SUCCESSFUL_BUILD",
      DOCKER_UNAVAILABLE: "DOCKER_UNAVAILABLE",
    });
  });

  it("the poll interval is five seconds", () => {
    expect(PROJECT_POLL_INTERVAL_MS).toBe(5000);
  });
});

describe("confirm: offered exactly when the backend names a build to confirm", () => {
  const table: Array<[string, DeployControls | null, string | null]> = [
    ["the backend names a build", controls({ confirmable_build_id: "bld-1", confirm_blocked_reason: null }), "bld-1"],
    ["the backend names none, with a reason", controls({ confirm_blocked_reason: "NOT_DEPLOY_ELIGIBLE" }), null],
    ["an empty id is no build", controls({ confirmable_build_id: "" }), null],
    ["no controls at all fails closed", null, null],
  ];

  it.each(table)("%s", (_label, value, target) => {
    expect(confirmTargetBuildId(value)).toBe(target);
    expect(canOfferConfirm(value)).toBe(target !== null);
  });

  it("is not decided by anything but that field: a running build, an active deployment, an in-flight flag change nothing", () => {
    const base = controls({ confirmable_build_id: "bld-1", confirm_blocked_reason: null });

    expect(canOfferConfirm({ ...base, in_flight: true, active_deployment_id: "dep-1" })).toBe(true);
    expect(canOfferConfirm({ ...controls(), in_flight: true, active_deployment_id: "dep-1" })).toBe(false);
  });
});

describe("rollback: offered exactly when the backend names a target", () => {
  const table: Array<[string, DeployControls | null, boolean]> = [
    ["a target exists", controls({ rollback_target_id: "dep-1", rollback_blocked_reason: null }), true],
    ["no target", controls(), false],
    ["an empty id is no target", controls({ rollback_target_id: "" }), false],
    ["no controls at all fails closed", null, false],
    [
      "an ACTIVE deployment alone is not a target (the old rule offered a rollback that always failed)",
      controls({ active_deployment_id: "dep-1" }),
      false,
    ],
    [
      "a target is offered with nothing ACTIVE (after a stop)",
      controls({ rollback_target_id: "dep-1", rollback_blocked_reason: null, active_deployment_id: null }),
      true,
    ],
  ];

  it.each(table)("%s", (_label, value, expected) => {
    expect(canOfferRollback(value)).toBe(expected);
  });
});

describe("stop: offered exactly when something is ACTIVE", () => {
  const table: Array<[string, DeployControls | null, boolean]> = [
    ["an active deployment", controls({ active_deployment_id: "dep-1" }), true],
    ["none", controls(), false],
    ["an empty id", controls({ active_deployment_id: "" }), false],
    ["no controls", null, false],
    ["a rollback target alone is nothing to stop", controls({ rollback_target_id: "dep-1" }), false],
  ];

  it.each(table)("%s", (_label, value, expected) => {
    expect(canOfferStop(value)).toBe(expected);
  });
});

describe("offersAnyDeployControl — whether a viewer who may not operate them is told so", () => {
  it("is true for each control alone and false for none", () => {
    expect(offersAnyDeployControl(controls({ confirmable_build_id: "b" }))).toBe(true);
    expect(offersAnyDeployControl(controls({ rollback_target_id: "d" }))).toBe(true);
    expect(offersAnyDeployControl(controls({ active_deployment_id: "d" }))).toBe(true);
    expect(offersAnyDeployControl(controls())).toBe(false);
    expect(offersAnyDeployControl(null)).toBe(false);
  });
});

describe("shouldPollProject — poll iff something is in flight", () => {
  it("polls while the backend says a build or a deployment is in flight", () => {
    expect(shouldPollProject(controls({ in_flight: true }))).toBe(true);
  });

  it("stops when nothing is in flight, however much is offered", () => {
    expect(shouldPollProject(controls({ in_flight: false }))).toBe(false);
    expect(
      shouldPollProject(
        controls({
          in_flight: false,
          confirmable_build_id: "b",
          rollback_target_id: "d",
          active_deployment_id: "d2",
        }),
      ),
    ).toBe(false);
  });

  it("does not poll with no controls (the first load has not landed, or the response had none)", () => {
    expect(shouldPollProject(null)).toBe(false);
  });

  it("follows a state change: in flight, then settled", () => {
    const sequence = [true, true, false].map((in_flight) => shouldPollProject(controls({ in_flight })));

    expect(sequence).toEqual([true, true, false]);
  });
});

// Keep in step with the backend: the same eleven are `ALL_REFUSAL_CODES` in
// `backend/tests/test_deploy_controls.py`, which pins `DeployRefusalCode` (app/services/deploy.py)
// to this list. A code added, renamed or dropped on either side changes on both in the same commit.
const EVERY_CODE = [
  "PROJECT_ARCHIVED",
  "DEPLOYMENT_IN_FLIGHT",
  "EVAL_PROJECT",
  "POSTGRES_UNCONFIGURED",
  "BUILD_NOT_SUCCEEDED",
  "NOT_DEPLOY_ELIGIBLE",
  "NOT_LATEST_SUCCESSFUL",
  "IMAGE_MISSING",
  "NO_ROLLBACK_TARGET",
  "NO_SUCCESSFUL_BUILD",
  "DOCKER_UNAVAILABLE",
] as const;

describe("deployRefusalMessage — every code has its own Korean sentence", () => {
  it("covers exactly the codes the backend defines (the literal list above)", () => {
    expect(Object.values(DeployRefusalCode).sort()).toEqual([...EVERY_CODE].sort());
  });

  it.each(EVERY_CODE)("%s has a sentence that is Korean and is not the code", (code) => {
    const message = deployRefusalMessage(code);

    expect(message).not.toBeNull();
    expect(message).toMatch(/[가-힣]/);
    expect(message).not.toBe(code);
  });

  it("gives each code a different sentence", () => {
    const messages = EVERY_CODE.map((code) => deployRefusalMessage(code));

    expect(new Set(messages).size).toBe(EVERY_CODE.length);
  });

  it("returns null for a code this build has never heard of, and for the prototype's own names", () => {
    expect(deployRefusalMessage("SOMETHING_NEW")).toBeNull();
    expect(deployRefusalMessage("constructor")).toBeNull();
    expect(deployRefusalMessage("__proto__")).toBeNull();
    expect(deployRefusalMessage(null)).toBeNull();
    expect(deployRefusalMessage(undefined)).toBeNull();
  });

  it("pins the archived sentence: it says to lift the archive first", () => {
    expect(deployRefusalMessage("PROJECT_ARCHIVED")).toBe(
      "보관된 프로젝트는 배포하거나 롤백할 수 없습니다. 먼저 보관을 해제하세요.",
    );
  });
});

describe("confirmBlockedMessage — why 배포 확정 is not there, in words", () => {
  it("is null while a build is offered", () => {
    expect(confirmBlockedMessage(controls({ confirmable_build_id: "b", confirm_blocked_reason: null }))).toBeNull();
  });

  it("says the reason for each blocked state", () => {
    for (const code of EVERY_CODE) {
      expect(confirmBlockedMessage(controls({ confirm_blocked_reason: code })), code).toBe(
        deployRefusalMessage(code),
      );
    }
  });

  it("is null for no controls and for a reason it cannot word", () => {
    expect(confirmBlockedMessage(null)).toBeNull();
    expect(confirmBlockedMessage(controls({ confirm_blocked_reason: "SOMETHING_NEW" }))).toBeNull();
    expect(confirmBlockedMessage(controls({ confirm_blocked_reason: null }))).toBeNull();
  });
});

describe("deployActionFailureMessage — the refusals are told apart", () => {
  it("tells an archived project to lift the archive (409 with the code)", () => {
    const message = deployActionFailureMessage(refused(409, "PROJECT_ARCHIVED"), "confirm");

    expect(message).toContain("보관");
    expect(message).toContain("해제");
  });

  it("tells a deployment in progress from an archive (both are 409)", () => {
    const inFlight = deployActionFailureMessage(refused(409, "DEPLOYMENT_IN_FLIGHT"), "confirm");
    const archived = deployActionFailureMessage(refused(409, "PROJECT_ARCHIVED"), "confirm");

    expect(inFlight).toBe("이미 진행 중인 배포가 있습니다. 끝난 뒤에 다시 시도하세요.");
    expect(inFlight).not.toBe(archived);
  });

  it("tells 'not the latest build' from the other 400s", () => {
    const notLatest = deployActionFailureMessage(refused(400, "NOT_LATEST_SUCCESSFUL"), "confirm");
    const notEligible = deployActionFailureMessage(refused(400, "NOT_DEPLOY_ELIGIBLE"), "confirm");
    const imageGone = deployActionFailureMessage(refused(400, "IMAGE_MISSING"), "confirm");

    expect(notLatest).toContain("더 최근에 성공한 빌드");
    expect(new Set([notLatest, notEligible, imageGone]).size).toBe(3);
  });

  it("words a rollback with no target as its own refusal", () => {
    expect(deployActionFailureMessage(refused(400, "NO_ROLLBACK_TARGET"), "rollback")).toBe(
      "되돌아갈 이전 배포가 없습니다.",
    );
  });

  it("gives every coded refusal its sentence, whichever action asked", () => {
    for (const code of EVERY_CODE) {
      for (const action of ["confirm", "rollback", "stop"] as const) {
        expect(deployActionFailureMessage(refused(400, code), action), `${code}/${action}`).toBe(
          deployRefusalMessage(code),
        );
      }
    }
  });

  it("never puts the server's English on screen, coded or not", () => {
    for (const err of [refused(409, "PROJECT_ARCHIVED"), refused(400, "NOT_LATEST_SUCCESSFUL"), new ApiError(500, "distinctive-server-detail-xyz", "distinctive-server-detail-xyz")]) {
      expect(deployActionFailureMessage(err, "confirm")).not.toContain("English diagnostic");
      expect(deployActionFailureMessage(err, "confirm")).not.toContain("distinctive-server-detail-xyz");
    }
  });

  it("a 409 without a code is a conflict, a 503 is Docker, and the rest keep the action's own fallback", () => {
    expect(deployActionFailureMessage(new ApiError(409, "x", "x"), "confirm")).toBe(
      "다른 작업과 겹쳐 처리하지 못했습니다. 화면을 새로 고친 뒤 다시 시도하세요.",
    );
    expect(deployActionFailureMessage(new ApiError(503, "x", "x"), "rollback")).toBe(
      "Docker에 연결하지 못해 처리하지 못했습니다. 잠시 후 다시 시도하세요.",
    );
    expect(deployActionFailureMessage(new ApiError(403, "Project owner or admin role required"), "confirm")).toBe(
      "배포 확정에 실패했습니다.",
    );
    expect(deployActionFailureMessage(new ApiError(400, "no ACTIVE deployment to stop"), "stop")).toBe(
      "중지에 실패했습니다.",
    );
    expect(deployActionFailureMessage(new ApiError(500, "x"), "rollback")).toBe("롤백에 실패했습니다.");
  });

  it("a code this build does not know falls back to the status's wording", () => {
    expect(deployActionFailureMessage(refused(409, "SOMETHING_NEW"), "confirm")).toBe(
      "다른 작업과 겹쳐 처리하지 못했습니다. 화면을 새로 고친 뒤 다시 시도하세요.",
    );
    expect(deployActionFailureMessage(refused(400, "SOMETHING_NEW"), "stop")).toBe("중지에 실패했습니다.");
  });

  it("a failure that never reached the server says so, not the action's fallback", () => {
    const offline = new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network");

    expect(deployActionFailureMessage(offline, "confirm")).toBe(NETWORK_ERROR_COPY);
  });

  it("a non-ApiError gets the action's fallback", () => {
    expect(deployActionFailureMessage(new Error("boom"), "confirm")).toBe("배포 확정에 실패했습니다.");
    expect(deployActionFailureMessage("boom", "stop")).toBe("중지에 실패했습니다.");
  });
});

describe("shouldRefreshAfterDeployFailure — a refusal that says the page is stale triggers a re-read", () => {
  it("re-reads after a 400 or 409 only", () => {
    expect(shouldRefreshAfterDeployFailure(refused(409, "PROJECT_ARCHIVED"))).toBe(true);
    expect(shouldRefreshAfterDeployFailure(refused(400, "NOT_LATEST_SUCCESSFUL"))).toBe(true);
    expect(shouldRefreshAfterDeployFailure(new ApiError(403, "x"))).toBe(false);
    expect(shouldRefreshAfterDeployFailure(new ApiError(503, "x"))).toBe(false);
    expect(shouldRefreshAfterDeployFailure(new ApiError(NETWORK_ERROR_STATUS, "x", undefined, "network"))).toBe(false);
  });
});

// -- the failure alert's lifetime: the un-archive alert's rule (unarchive.test.ts) for this one --

describe("deployActionFailure — the sentence and the code it reports", () => {
  it("carries the backend's refusal code beside the sentence", () => {
    const failure = deployActionFailure(new ApiError(409, "x", "DEPLOYMENT_IN_FLIGHT: another deployment ..."), "confirm");

    expect(failure.code).toBe("DEPLOYMENT_IN_FLIGHT");
    expect(failure.message).toBe(deployRefusalMessage("DEPLOYMENT_IN_FLIGHT"));
  });

  it("has no code for a failure the backend did not code", () => {
    expect(deployActionFailure(new ApiError(500, "x", "boom"), "stop").code).toBeNull();
    expect(deployActionFailure(new Error("x"), "stop").code).toBeNull();
  });
});

describe("deployFailureAfter — what stays of the deploy failure alert", () => {
  const inFlight: DeployFailure = { message: "이미 진행 중인 배포가 있습니다.", code: "DEPLOYMENT_IN_FLIGHT" };
  const archived: DeployFailure = { message: "보관된 프로젝트입니다.", code: "PROJECT_ARCHIVED" };
  const other: DeployFailure = { message: "이미지가 없습니다.", code: "IMAGE_MISSING" };
  const uncoded: DeployFailure = { message: "중지에 실패했습니다.", code: null };
  const ALL = [inFlight, archived, other, uncoded];

  it("nothing in, nothing out", () => {
    expect(deployFailureAfter(null, { kind: "refresh" })).toBeNull();
    expect(deployFailureAfter(null, { kind: "read", controls: controls(), archived: false })).toBeNull();
  });

  it("the reader pressing 새로 고침 ends it, whatever it reported", () => {
    for (const failure of ALL) {
      expect(deployFailureAfter(failure, { kind: "refresh" }), String(failure.code)).toBeNull();
    }
  });

  it("a submitted change request ends it, whatever it reported", () => {
    for (const failure of ALL) {
      expect(deployFailureAfter(failure, { kind: "change_request" }), String(failure.code)).toBeNull();
    }
  });

  it("'a deployment is in flight' stays while the read says work is in flight, and goes when it does not", () => {
    expect(deployFailureAfter(inFlight, { kind: "read", controls: controls({ in_flight: true }), archived: false })).toBe(inFlight);
    expect(deployFailureAfter(inFlight, { kind: "read", controls: controls({ in_flight: false }), archived: false })).toBeNull();
  });

  it("'a deployment is in flight' goes when the read carries no controls: nothing says it still holds", () => {
    expect(deployFailureAfter(inFlight, { kind: "read", controls: null, archived: false })).toBeNull();
  });

  it("the poll that sees the deployment finished clears it, and 배포 확정 is offered again", () => {
    const finished = controls({ confirmable_build_id: "build-1", confirm_blocked_reason: null, in_flight: false });

    expect(canOfferConfirm(finished)).toBe(true);
    expect(deployFailureAfter(inFlight, { kind: "read", controls: finished, archived: false })).toBeNull();
  });

  it("'the project is archived' stays while it is archived, and goes when a read shows it is not", () => {
    expect(deployFailureAfter(archived, { kind: "read", controls: controls(), archived: true })).toBe(archived);
    expect(deployFailureAfter(archived, { kind: "read", controls: controls(), archived: false })).toBeNull();
  });

  it("a refusal a read cannot show to be over stays through a read, until the reader moves on", () => {
    for (const failure of [other, uncoded]) {
      for (const read of [
        { kind: "read" as const, controls: controls({ in_flight: true }), archived: true },
        { kind: "read" as const, controls: controls({ in_flight: false }), archived: false },
        { kind: "read" as const, controls: null, archived: false },
      ]) {
        expect(deployFailureAfter(failure, read), String(failure.code)).toBe(failure);
      }
    }
  });
});

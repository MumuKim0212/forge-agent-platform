/**
 * The one piece of this task's logic that a `node`-environment
 * Vitest run can actually reach (ADR 0021) — the markup that wires these
 * functions to the two rows cannot be mounted, so this is the whole test
 * layer for the feature.
 */
import { describe, expect, it } from "vitest";

import { isActivationKey, shouldActivateRow } from "./activation";

describe("isActivationKey", () => {
  it("activates on Enter", () => {
    expect(isActivationKey("Enter")).toBe(true);
  });

  it("does not activate on Space or any other key", () => {
    // " " is the DOM `KeyboardEvent.key` value for the space bar (the key
    // this function deliberately excludes — see activation.ts). The rest
    // are here so a careless rewrite (e.g. "any whitespace-ish key" or "any
    // key length 1") cannot pass by accident.
    const nonActivating = [
      " ",
      "Spacebar",
      "Tab",
      "Escape",
      "ArrowDown",
      "a",
      "enter", // DOM emits "Enter", not lowercase — this must not slip through
      "",
    ];
    for (const key of nonActivating) {
      expect(isActivationKey(key), `key = ${JSON.stringify(key)}`).toBe(false);
    }
  });
});

describe("shouldActivateRow", () => {
  // Plain objects stand in for DOM nodes: the guard only ever compares
  // identity (`===`), so a real `<tr>`/`<a>` is not needed to exercise it —
  // and using plain objects keeps this test in ADR 0021's `node`
  // environment without any DOM shim.
  const row = {};
  const nestedControl = {};

  it("activates on Enter when the event target is the row itself", () => {
    expect(shouldActivateRow("Enter", row, row)).toBe(true);
  });

  it("does NOT activate when the keydown bubbled from a nested element", () => {
    // This is the review's regression, pinned as a test: a keydown that
    // bubbled up from a nested interactive child (e.g. ApprovalList's
    // preview <a>) must not be swallowed by the row's own handler, or the
    // nested control's native keyboard activation breaks.
    expect(shouldActivateRow("Enter", nestedControl, row)).toBe(false);
  });

  it("does not activate on a non-activation key even when the row is the target", () => {
    expect(shouldActivateRow(" ", row, row)).toBe(false);
    expect(shouldActivateRow("Tab", row, row)).toBe(false);
  });

  it("does not activate on a non-activation key from a nested element either", () => {
    expect(shouldActivateRow(" ", nestedControl, row)).toBe(false);
  });

  it("treats target/currentTarget by identity, not shape", () => {
    // Two structurally-identical-but-distinct objects must still count as
    // "different nodes" — this guards against a future rewrite loosening
    // the check to a deep-equal or shape comparison, which would defeat the
    // whole point (a nested node can easily share shape with its row).
    expect(shouldActivateRow("Enter", {}, {})).toBe(false);
  });
});

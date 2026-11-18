// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment. The second accessibility
 * property one of these primitives owns (the first is `ListRow`'s,
 * in `ListRow.a11y.test.tsx`): whether `Alert` announces itself to a
 * screen reader as an assertive live region.
 *
 * Pins the fix for r1 at the primitive itself: `role="alert"` follows the
 * `live` prop, not `variant` — a `variant="error"` alert with no `live`
 * (a past record, e.g. a deployment's recorded failure reason) must not
 * carry `role="alert"`, and one with `live` (a load/submit failure just
 * surfaced) must. `warning` never opts in anywhere in this task's five
 * files, but the primitive itself does not forbid it — `live` is the only
 * switch, so this file also pins that a `warning` alert given `live` still
 * gets the role, proving the two concerns are genuinely decoupled rather
 * than `live` being a second name for "not warning".
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Alert } from "./Alert";

describe("Alert — live region is opt-in, independent of tone", () => {
  it("carries no role by default, on every variant", () => {
    render(
      <div>
        <Alert variant="error">에러</Alert>
        <Alert variant="success">성공</Alert>
        <Alert variant="warning">경고</Alert>
        <Alert variant="info">안내</Alert>
      </div>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("carries role=alert when live is set on an error variant (a load/submit failure just surfaced)", () => {
    render(
      <Alert variant="error" live>
        방금 실패했습니다
      </Alert>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("방금 실패했습니다");
  });

  it("does not carry role=alert on an error variant recording a past failure (no live)", () => {
    render(
      <Alert variant="error" className="mt-1">
        실패 사유: 이전에 있었던 일
      </Alert>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("carries role=alert when live is set even on a warning variant, proving live is the only switch", () => {
    render(
      <Alert variant="warning" live>
        경고이지만 방금 발생함
      </Alert>,
    );

    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("Alert — the info variant is a tone like the others", () => {
  it("carries no role by default and role=alert only when live is set", () => {
    const { rerender } = render(<Alert variant="info">승인을 기다리는 중입니다</Alert>);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(
      <Alert variant="info" live>
        승인을 기다리는 중입니다
      </Alert>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

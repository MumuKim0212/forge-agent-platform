// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. `Field`'s ARIA-relevant contract: a label queried by its
 * text resolves to the control named by `htmlFor`. `htmlFor` is optional and the component never
 * generates an id, so a caller who omits it gets a label associated with nothing — permitted by the
 * types, not caught by `tsc -b`. Pins both directions so that gap is recorded as tested behaviour
 * rather than discovered later.
 *
 * Also pinned are the other two halves of what a field tells assistive technology: its hint and error are
 * the control's *description* (`aria-describedby`), and an error is only announced when its caller
 * says so. The tests assert roles, ids and the description a browser would compute — never the copy.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { lengthError, lengthHint, lengthNotice } from "../../api/requestLimits";
import { Field } from "./Field";
import { Input } from "./Input";
import { Select } from "./Select";
import { Textarea } from "./Textarea";

describe("Field label <-> control association", () => {
  it("a label queried by its text resolves to the control when htmlFor names it", () => {
    render(
      <Field label="이메일" htmlFor="email-input">
        <input id="email-input" />
      </Field>,
    );

    const control = screen.getByLabelText("이메일");
    expect(control).toBeInstanceOf(HTMLInputElement);
    expect(control).toHaveAttribute("id", "email-input");
  });

  it("without htmlFor, the label does not resolve to the control (today's permitted gap)", () => {
    render(
      <Field label="이메일">
        <input id="email-input" />
      </Field>,
    );

    expect(screen.queryByLabelText("이메일")).toBeNull();
  });
});

describe("Field hint and error are the control's description", () => {
  it("describes the control by its hint, through an id that resolves to the hint", () => {
    render(
      <Field label="비밀번호" htmlFor="pw" hint="최소 8자 이상.">
        <Input id="pw" />
      </Field>,
    );

    const control = screen.getByLabelText("비밀번호");
    expect(control).toHaveAccessibleDescription("최소 8자 이상.");
    const hint = document.getElementById(control.getAttribute("aria-describedby") ?? "");
    expect(hint).not.toBeNull();
    expect(hint).toHaveTextContent("최소 8자 이상.");
  });

  it("describes the control by its error, which replaces the hint exactly as it does on screen", () => {
    render(
      <Field label="이름" htmlFor="n" hint="힌트입니다." error="이름을 입력해 주세요.">
        <Input id="n" />
      </Field>,
    );

    const control = screen.getByLabelText("이름");
    expect(control).toHaveAccessibleDescription("이름을 입력해 주세요.");
    expect(screen.queryByText("힌트입니다.")).toBeNull();
  });

  it("puts the hint back when the error clears", () => {
    const { rerender } = render(
      <Field label="이름" htmlFor="n" hint="힌트입니다." error="오류입니다.">
        <Input id="n" />
      </Field>,
    );
    rerender(
      <Field label="이름" htmlFor="n" hint="힌트입니다.">
        <Input id="n" />
      </Field>,
    );

    expect(screen.getByLabelText("이름")).toHaveAccessibleDescription("힌트입니다.");
  });

  it("wires an Input, a Textarea, a Select and a bare input alike", () => {
    render(
      <div>
        <Field label="a" htmlFor="a" hint="설명 a">
          <Input id="a" />
        </Field>
        <Field label="b" htmlFor="b" hint="설명 b">
          <Textarea id="b" />
        </Field>
        <Field label="c" htmlFor="c" hint="설명 c">
          <Select id="c">
            <option>x</option>
          </Select>
        </Field>
        <Field label="d" htmlFor="d" hint="설명 d">
          <input id="d" />
        </Field>
      </div>,
    );

    expect(screen.getByLabelText("a")).toHaveAccessibleDescription("설명 a");
    expect(screen.getByLabelText("b")).toHaveAccessibleDescription("설명 b");
    expect(screen.getByLabelText("c")).toHaveAccessibleDescription("설명 c");
    expect(screen.getByLabelText("d")).toHaveAccessibleDescription("설명 d");
  });

  it("keeps a description the caller already gave the control", () => {
    render(
      <div>
        <p id="extra">추가 안내.</p>
        <Field label="이름" htmlFor="n" hint="힌트입니다.">
          <Input id="n" aria-describedby="extra" />
        </Field>
      </div>,
    );

    expect(screen.getByLabelText("이름")).toHaveAccessibleDescription("추가 안내. 힌트입니다.");
  });

  it("gives every field its own ids, so two fields with the same hint do not describe each other", () => {
    render(
      <div>
        <Field label="첫째" htmlFor="one" hint="같은 힌트">
          <Input id="one" />
        </Field>
        <Field label="둘째" htmlFor="two" hint="같은 힌트">
          <Input id="two" />
        </Field>
      </div>,
    );

    const first = screen.getByLabelText("첫째").getAttribute("aria-describedby");
    const second = screen.getByLabelText("둘째").getAttribute("aria-describedby");
    expect(first).toBeTruthy();
    expect(second).toBeTruthy();
    expect(first).not.toBe(second);
  });

  it("adds no description attribute when there is neither hint nor error", () => {
    render(
      <Field label="이름" htmlFor="n">
        <Input id="n" />
      </Field>,
    );

    expect(screen.getByLabelText("이름")).not.toHaveAttribute("aria-describedby");
  });

  it("does not throw for a child it cannot wire (a fragment), and wires nothing", () => {
    render(
      <Field label="이름" htmlFor="n" hint="힌트입니다.">
        <>
          <input id="n" />
        </>
      </Field>,
    );

    expect(screen.getByLabelText("이름")).not.toHaveAttribute("aria-describedby");
  });
});

describe("how a field's error is announced is an explicit choice", () => {
  it("is not a live region by default: shown and described, not announced", () => {
    render(
      <Field label="이름" htmlFor="n" error="오류입니다.">
        <Input id="n" />
      </Field>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("이름")).toHaveAccessibleDescription("오류입니다.");
  });

  it("is an assertive alert when the caller says the error answers something the user just did", () => {
    render(
      <Field label="이름" htmlFor="n" error="오류입니다." errorLive>
        <Input id="n" />
      </Field>,
    );

    const alert = screen.getByRole("alert");
    expect(screen.getByLabelText("이름").getAttribute("aria-describedby")).toBe(alert.id);
  });

  it("offers a polite status region for the typing case, empty until there is an error", () => {
    const { rerender } = render(
      <Field label="이름" htmlFor="n" errorNotice="이름: 제한 초과">
        <Input id="n" />
      </Field>,
    );
    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();

    rerender(
      <Field label="이름" htmlFor="n" error="자세한 설명" errorNotice="이름: 제한 초과">
        <Input id="n" />
      </Field>,
    );

    // The same element, now holding the notice: the region existed before its text did.
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("이름: 제한 초과");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders no status region for a field that did not ask for one", () => {
    render(
      <Field label="이름" htmlFor="n" error="오류입니다.">
        <Input id="n" />
      </Field>,
    );

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

/** The three call sites' shape (`RequestIntakePage` x2, `ProjectDetailPage`): a counter hint, the
 * over-limit error, the stable notice, and no `errorLive`. The pages themselves are mounted in
 * `pages/fieldCallSites.a11y.test.tsx`; this is the primitive under the same inputs. */
function LimitedField({ max }: { max: number }) {
  const [text, setText] = useState("");
  return (
    <Field
      label="이름"
      htmlFor="limited"
      hint={lengthHint(text, max)}
      error={lengthError(text, max)}
      errorNotice={lengthNotice("이름")}
    >
      <Input
        id="limited"
        value={text}
        onChange={(e) => setText(e.target.value)}
        invalid={lengthError(text, max) !== undefined}
      />
    </Field>
  );
}

describe("typing past a length limit does not interrupt on every keystroke", () => {
  it("never creates an alert, however many characters go past the limit", async () => {
    const user = userEvent.setup();
    render(<LimitedField max={5} />);
    const control = screen.getByLabelText("이름");

    for (const ch of "1234567890") {
      await user.type(control, ch);
      expect(screen.queryAllByRole("alert")).toHaveLength(0);
    }
  });

  it("says once that the limit was crossed and leaves that region untouched while the user keeps typing", async () => {
    const user = userEvent.setup();
    render(<LimitedField max={5} />);
    const control = screen.getByLabelText("이름");
    const field = control.closest("div") as HTMLElement;
    const status = within(field).getByRole("status");

    await user.type(control, "12345");
    expect(status).toBeEmptyDOMElement();

    await user.type(control, "6");
    const announced = status.textContent;
    expect(announced).toBeTruthy();
    const descriptionAtCrossing = control.getAttribute("aria-describedby");
    expect(descriptionAtCrossing).toBeTruthy();

    // From here on the region must see no change at all: a polite region re-announces any change.
    const observer = new MutationObserver(() => {});
    observer.observe(status, { childList: true, characterData: true, subtree: true, attributes: true });
    const descriptionBefore = control.ownerDocument.getElementById(descriptionAtCrossing as string)?.textContent;

    await user.type(control, "789");

    expect(observer.takeRecords()).toHaveLength(0);
    observer.disconnect();
    expect(status.textContent).toBe(announced);
    // Non-vacuous: what the user keeps typing does change the visible, described count.
    expect(control.ownerDocument.getElementById(descriptionAtCrossing as string)?.textContent).not.toBe(
      descriptionBefore,
    );
  });

  it("describes the live count on the control, from the first character", async () => {
    const user = userEvent.setup();
    render(<LimitedField max={5} />);
    const control = screen.getByLabelText("이름");
    expect(control).toHaveAccessibleDescription("0 / 5");

    await user.type(control, "123");
    expect(control).toHaveAccessibleDescription("3 / 5");
  });
});

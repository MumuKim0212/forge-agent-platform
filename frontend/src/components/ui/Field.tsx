import { Fragment, cloneElement, isValidElement, useId, type ReactElement, type ReactNode } from "react";

import { cx } from "./cx";

export interface FieldProps {
  label: ReactNode;
  htmlFor?: string;
  /** Field-level validation error (T2: "no field-level validation error"
   * anywhere in either mockup). Always the control's accessible description
   * (`aria-describedby`), so it is read when the control is focused. Whether it is
   * *also* announced the moment it appears is a separate choice: see `errorLive` and
   * `errorNotice`. Replaces `hint` while set. */
  error?: string;
  /** Announce `error` assertively (`role="alert"`) the moment it appears. For an error that is
   * the result of something the user just did: a submit that found a mistake. **Never for an
   * error the user causes by typing** -- an assertive region interrupts the screen reader on
   * every change of its text, and a length error carries the live count, so it would interrupt
   * on every keystroke. A live region is an intent, not a tone, so this is opt-in and
   * defaults to `false`, the way `Alert`'s `live` does. */
  errorLive?: boolean;
  /** A polite announcement for an error the user is producing while they type. A
   * `role="status"` region that shows this text while `error` is set and is empty otherwise.
   * **Pass the same string on every render, whether or not there is an error**: the region has
   * to exist before its text does, and a polite region still re-announces every change of its
   * text, so the changing detail (the count) stays in `error`, which is only described-by, and
   * this says once that the limit was crossed. Name the field in it: the region is read on its
   * own, without the label. */
  errorNotice?: string;
  hint?: ReactNode;
  required?: boolean;
  children: ReactNode;
  className?: string;
}

/** `describedBy` added to the control's own `aria-describedby` (a caller's reference is kept).
 * Only a single element child can be wired; anything else (text, an array, a fragment) is
 * returned as it came, and the hint and error are then visible but not tied to a control. */
function describeControl(children: ReactNode, describedBy: string | undefined): ReactNode {
  if (describedBy === undefined) return children;
  if (!isValidElement(children) || children.type === Fragment) return children;
  const control = children as ReactElement<{ "aria-describedby"?: string }>;
  const own = control.props["aria-describedby"];
  return cloneElement(control, { "aria-describedby": own ? `${own} ${describedBy}` : describedBy });
}

/** Label + control + error text, as one shape — the
 * signup form's four fields are this primitive's first caller. Carries
 * no validation logic itself: the caller computes `error`, this only decides
 * how it looks and how it is announced.
 *
 * **The hint and the error are tied to the control.** Each gets a generated id and the
 * (single) child control receives `aria-describedby` naming whichever is shown (the error
 * replaces the hint), so `1234 / 8000`, `최소 8자 이상` and every error are read when the control
 * takes focus, not only to someone who happens to browse past the paragraph. The label is still
 * wired by the caller's `htmlFor`; this component does not generate the control's id. */
export function Field({
  label,
  htmlFor,
  error,
  errorLive = false,
  errorNotice,
  hint,
  required,
  children,
  className,
}: FieldProps) {
  const uid = useId();
  const hintId = `${uid}-hint`;
  const errorId = `${uid}-error`;
  const showsHint = Boolean(hint) && !error;
  const describedBy = error ? errorId : showsHint ? hintId : undefined;

  return (
    <div className={cx("mb-4", className)}>
      <label
        htmlFor={htmlFor}
        className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-200"
      >
        {label}
        {required && (
          <span aria-hidden="true" className="text-danger">
            {" "}
            *
          </span>
        )}
      </label>
      {describeControl(children, describedBy)}
      {showsHint && (
        <p id={hintId} className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} role={errorLive ? "alert" : undefined} className="mt-1 text-xs text-danger">
          {error}
        </p>
      )}
      {/* Mounted while empty: a live region has to exist before its text does, or the first
          announcement is lost. Only for a field that opts in. */}
      {errorNotice !== undefined && (
        <span role="status" className="sr-only">
          {error ? errorNotice : ""}
        </span>
      )}
    </div>
  );
}

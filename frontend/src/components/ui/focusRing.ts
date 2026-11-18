/**
 * The one definition of the keyboard focus ring (WCAG 1.4.11, 3:1 non-text contrast). Every
 * focusable primitive takes its ring from here; a ring utility spelled anywhere else is how a
 * control ends up with a colour nobody measured. The colour is the `--color-focus` token in
 * `styles/app.css`, which is a different value in dark mode.
 */

/** Controls drawn as a button, link or tab: the ring shows on keyboard focus only. */
export const FOCUS_RING = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";

/** Same ring for a control that also needs an offset from its own fill (a filled button). */
export const FOCUS_RING_OFFSET = `${FOCUS_RING} focus-visible:ring-offset-2`;

/** Text fields: a thin ring on any focus, plus the border change. */
export const FOCUS_RING_FIELD = "focus:outline-none focus:ring-1 focus:border-focus focus:ring-focus";

/** A text field in its invalid state keeps the danger colour on focus. */
export const FOCUS_RING_FIELD_INVALID = "focus:outline-none focus:ring-1 focus:border-danger focus:ring-danger";

/** The danger button's ring (a red button is not ringed in the accent), offset like the others. */
export const FOCUS_RING_DANGER_OFFSET =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger focus-visible:ring-offset-2";

/**
 * Two rows in the console (`TemplatesPanel`'s `<tr>`,
 * `ApprovalConsolePage`'s `<li>`) carry a click handler but are not
 * `<button>`s — the platform owner deferred all CSS to a later design pass,
 * and a real `<button>` inside the row would change what the cell looks
 * like today (out of scope per that deferral). They also keep their native
 * role (`row`, `listitem`): putting `role="button"` on a `<tr>` overrides
 * its implicit `role="row"` and leaves the surrounding `<table>` incoherent
 * to a screen reader — a common "accessible" fix that trades one barrier
 * for another (the console's rows are read-only display, not a
 * reason to break the table reading as one).
 *
 * That means each row gets its own `tabIndex={0}` and `onKeyDown`, and has
 * to decide for itself which key counts as "activate this row the way a
 * click would" — a real `<button>` gets that decision for free from the
 * browser, this markup does not.
 *
 * **Enter only, not Space.** Space's *default* browser action on a
 * focusable element is to scroll the page; a `<button>` (native or
 * `role="button"`) suppresses that as part of claiming button semantics.
 * Neither of these rows makes that claim — they still read as `row` /
 * `listitem` — so a keyboard user landing here who presses Space to keep
 * reading down the page should get exactly that, not a surprise
 * navigation. Enter carries no such expectation: it is the same key that
 * activates a plain `<a href>`, which is the closer analogy for what these
 * rows do (open a detail view), not a button's Enter-**or**-Space contract.
 */
export function isActivationKey(key: string): boolean {
  return key === "Enter";
}

/**
 * A row built this way can nest its own interactive
 * content (`ApprovalConsolePage`'s `<li>` nests a real preview `<a>`) that
 * already has its own, native keyboard activation. A `keydown` fired while
 * that nested control has focus **bubbles** up through the row exactly like
 * any other DOM event, so a bare `onKeyDown={(e) => { if (isActivationKey
 * (e.key)) ... }}` on the row receives it too — and `preventDefault()`
 * cancels the *original* target's default action (the link's own
 * Enter-triggered navigation), not just the row's. The bug this shipped
 * once: Enter on the nested preview link silently re-selected the row
 * instead of opening the preview, because nothing distinguished "the row
 * itself was focused and activated" from "some descendant of the row was
 * focused and its own keydown bubbled here."
 *
 * The fix is a target check, not a `stopPropagation()` on every nested
 * control — the row already carries `tabIndex={0}`, so `target ===
 * currentTarget` means precisely "the row itself is what has focus";
 * putting the guard here means the *next* interactive element nested inside
 * a row like this keeps its own native keyboard behaviour automatically,
 * with nothing for that future author to remember. Apply this on every row
 * built with this pattern, including one with no nested interactive content
 * today (`TemplatesPanel`'s `<tr>`) — the guard costs nothing when there is
 * nothing nested to protect, and the alternative is relying on that staying
 * true forever.
 */
export function shouldActivateRow(key: string, target: unknown, currentTarget: unknown): boolean {
  if (target !== currentTarget) return false;
  return isActivationKey(key);
}

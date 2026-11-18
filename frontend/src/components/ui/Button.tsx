import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cx } from "./cx";
import { FOCUS_RING_DANGER_OFFSET, FOCUS_RING_OFFSET } from "./focusRing";

/**
 * The one button shape the app renders from.
 * Four variants match the mockups' vocabulary; `disabled` and `busy` are the
 * two states T2 named as undrawn anywhere in either mockup file.
 *
 * Carries no business logic (c4): a caller decides variant, disabled-ness,
 * and busy-ness. The busy *label* text stays the caller's — several of these
 * strings are contracts, not copy (T1) — this primitive only supplies the
 * spinner and the disabled-while-busy behaviour, never the words.
 */
export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** True while the action this button starts is in flight. Implies
   * `disabled` (a busy button must not be re-triggered) and renders a
   * spinner beside whatever `children` the caller passed — the caller is
   * still the one deciding whether the label text itself changes. */
  busy?: boolean;
  children: ReactNode;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-white hover:bg-accent-hover disabled:bg-accent/50",
  secondary:
    "bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 dark:bg-slate-800 dark:text-slate-100 dark:border-slate-600 dark:hover:bg-slate-700 disabled:opacity-50",
  danger:
    "bg-danger text-white hover:bg-rose-700 disabled:bg-danger/50",
  ghost:
    "bg-transparent text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800 disabled:opacity-50",
};

/**
 * The class string of a button, for the one other element that must look like one: a link that
 * navigates (`ButtonLink`). Kept here so both render from the same variant table.
 */
export function buttonClassName(variant: ButtonVariant = "primary", className?: string): string {
  return cx(
    "inline-flex items-center justify-center gap-2 rounded-control px-4 py-2 text-sm font-semibold",
    "shadow-control transition-colors",
    variant === "danger" ? FOCUS_RING_DANGER_OFFSET : FOCUS_RING_OFFSET,
    "disabled:cursor-not-allowed disabled:shadow-none",
    VARIANT_CLASSES[variant],
    className,
  );
}

export function Button({
  variant = "primary",
  busy = false,
  disabled,
  className,
  children,
  type,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type ?? "button"}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={buttonClassName(variant, className)}
      {...rest}
    >
      {busy && (
        <span
          aria-hidden="true"
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  );
}

import type { ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";

import { cx } from "./cx";
import { FOCUS_RING } from "./focusRing";

/**
 * The look of a link that sits in running content: the accent
 * colour in light, a lighter indigo in dark (the accent alone is about 3:1 on the dark surfaces),
 * and the same indigo focus ring the navigation, the buttons and the top bar use. Before this each
 * call site spelled the colour itself, none had a dark variant, and the browser's thin default
 * focus mark showed through.
 *
 * Exported as a function as well as a component so an external `<a href>` (it cannot be a router
 * `Link`) draws from the same string.
 */
export function linkClassName(className?: string): string {
  return cx(
    "rounded-sm font-medium text-accent hover:text-accent-hover dark:text-indigo-400 dark:hover:text-indigo-300",
    FOCUS_RING,
    className,
  );
}

export interface TextLinkProps extends Omit<LinkProps, "className"> {
  className?: string;
  children: ReactNode;
}

/** A router link drawn as `linkClassName`. Carries no role/status/ownership knowledge: a caller
 * decides whether to render it at all. */
export function TextLink({ className, children, ...rest }: TextLinkProps) {
  return (
    <Link className={linkClassName(className)} {...rest}>
      {children}
    </Link>
  );
}

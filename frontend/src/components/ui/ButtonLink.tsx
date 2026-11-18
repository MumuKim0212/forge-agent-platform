import type { ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";

import { buttonClassName, type ButtonVariant } from "./Button";

export interface ButtonLinkProps extends Omit<LinkProps, "className"> {
  variant?: ButtonVariant;
  className?: string;
  children: ReactNode;
}

/**
 * A navigation that looks like a `Button`: a real anchor (it opens in a new tab, it is a link to a
 * screen reader), drawn from `Button`'s own variant table so the two cannot drift apart. Carries no
 * role/status/ownership knowledge: a caller decides whether to render it at all.
 */
export function ButtonLink({ variant = "primary", className, children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={buttonClassName(variant, className)} {...rest}>
      {children}
    </Link>
  );
}

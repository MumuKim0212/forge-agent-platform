import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "./cx";
import { visibleControlsNode } from "./visibleControls";

export interface CodeProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode;
}

/**
 * Inline code style for strings that must be read literally: a file path, a
 * matched pattern. It wraps long unbroken names instead of overflowing and keeps
 * a newline inside a string visible as one. It is a text style and nothing more:
 * it never makes a link out of what it holds.
 *
 * Every string it holds is agent-chosen, so invisible and direction-changing characters
 * in it are shown as visible `⟨U+XXXX⟩` markers: a path never displays as a
 * different file name than the one it is.
 */
export function Code({ className, children, ...rest }: CodeProps) {
  return (
    <code
      className={cx(
        "whitespace-pre-wrap break-all rounded bg-slate-100 px-1 py-0.5 font-mono text-xs text-slate-800",
        "dark:bg-slate-800 dark:text-slate-100",
        className,
      )}
      {...rest}
    >
      {visibleControlsNode(children)}
    </code>
  );
}

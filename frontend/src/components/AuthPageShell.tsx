import type { ReactNode } from "react";

import { Card } from "./ui/Card";

/**
 * The shared shell for the two routes that render outside `Layout`
 * (`/login`, `/signup` — App.tsx:43,49) and therefore have no header, nav,
 * or brand of their own today — the carried-forward "no visual shell"
 * defect this component closes.
 *
 * Not a `components/ui/` primitive: it is one page-composition shape
 * specific to these two unauthenticated routes, not a generic building
 * block another screen reaches for.
 */
export function AuthPageShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-12 dark:bg-slate-950">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2 text-lg font-bold tracking-tight text-slate-900 dark:text-slate-100">
          <span
            aria-hidden="true"
            className="flex h-8 w-8 items-center justify-center rounded-control bg-accent text-sm font-black text-white"
          >
            F
          </span>
          Forge
        </div>
        <Card className="p-6">{children}</Card>
      </div>
    </div>
  );
}

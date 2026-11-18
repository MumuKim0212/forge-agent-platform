import { createContext, useContext, useEffect } from "react";

/**
 * How a page tells the shell what its `h1` should say when the title is data (a project's
 * name) rather than a section. The shell (`Layout`) provides the setter; a page calls
 * `usePageTitle`. Outside the shell there is no setter and the hook does nothing, so a page mounted
 * on its own (a test) renders exactly what it rendered before.
 *
 * The title is cleared when the page unmounts or its value goes back to null, so leaving a
 * project never leaves its name on the next screen.
 */
export const PageTitleContext = createContext<((title: string | null) => void) | null>(null);

export function usePageTitle(title: string | null): void {
  const setTitle = useContext(PageTitleContext);
  useEffect(() => {
    if (setTitle === null) return;
    setTitle(title);
    return () => setTitle(null);
  }, [setTitle, title]);
}

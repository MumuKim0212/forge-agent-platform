import { useEffect } from "react";

/**
 * Sets `document.title` while the caller is mounted. The string is built by the caller
 * (`shellNav.documentTitle`) so the wording is testable without React. The previous title is not
 * restored: the next page sets its own, and a restore would flash the old one between routes.
 */
export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title;
  }, [title]);
}

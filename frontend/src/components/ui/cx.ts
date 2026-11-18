/**
 * Joins conditional class names, dropping anything falsy. Deliberately not a
 * dependency (`clsx`/`tailwind-merge`): the primitives never generate
 * conflicting utility classes for the same property in one call site, so the
 * "last one wins" behaviour those libraries add over plain string
 * concatenation buys nothing here (the frontend keeps its dependency
 * surface to what a dependency actually costs).
 */
export function cx(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

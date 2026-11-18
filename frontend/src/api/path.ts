/**
 * The one way an id becomes a segment of a request path.
 *
 * `encodeURIComponent` turns `/`, `?`, `#` and `%` into escapes, so an id can never end the segment
 * early, start a query string, or smuggle a fragment -- the three ways a raw `${id}` rewrote the
 * request. Every `api/*.ts` module that puts an id in a path calls this; `pathEncoding.test.ts`
 * fails when one does not.
 *
 * Encoding cannot save the segments `.` and `..`: a URL parser treats them (and their `%2e`
 * spellings) as dot segments and walks the path up a level before the request is sent. No real id
 * is one -- ids are UUIDs -- so this refuses them, and the empty string (`/projects/` is the list
 * route, not "the project with an empty id"), instead of sending a request for a different
 * resource. The refusal is thrown, so a call site's own `catch` turns it into its failure message.
 */
export function pathSegment(id: string): string {
  if (id === "" || id === "." || id === "..") {
    throw new RangeError("not usable as a path segment");
  }
  return encodeURIComponent(id);
}

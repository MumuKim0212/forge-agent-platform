/**
 * The project page's "open the service" link, as a named decision (ADR 0021).
 *
 * `GET /projects/{id}` carries `service_url` (`backend/app/services/deploy.service_url`): an
 * absolute Traefik URL in prod, or the relative dev route `/api/projects/{id}/service/` that the SPA's
 * own origin serves through the Vite proxy. It is null unless the project has an ACTIVE deployment,
 * so null is "nothing to open", not an error.
 *
 * PRESENTATION only: the service route has no identity (ADR 0028), the URL is not a secret, and
 * hiding the link is not a control.
 */

/** The link's visible text. */
export const OPEN_SERVICE_LABEL = "서비스 열기";

/**
 * The href the page offers, or null when it offers none. The backend builds the value from
 * settings, so it is trusted; the shape check is the cheap belt: only an `http(s)://` URL or a
 * same-origin absolute path (`/...`, not protocol-relative `//host`) becomes a link, so a stray
 * `javascript:` or `data:` value can never be put in an `href`.
 */
export function serviceLinkHref(project: { service_url: string | null } | null): string | null {
  if (project === null) return null;
  const url = project.service_url;
  if (typeof url !== "string" || url === "") return null;
  if (/^https?:\/\/\S/i.test(url)) return url;
  if (url.startsWith("/") && !url.startsWith("//")) return url;
  return null;
}

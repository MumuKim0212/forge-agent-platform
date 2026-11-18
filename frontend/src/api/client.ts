const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

/** Absolute (well, base-prefixed) URL for a backend path. Exported so a
 * *browser navigation* to a backend endpoint — the OIDC login start, which is
 * a redirect and not a `fetch` — still goes through the one place that knows
 * where the API lives. */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

/** The `/openapi.json` URL that belongs to an API base. FastAPI serves
 * the schema at its app root, one level above the `/api` prefix, so the base's trailing `/api` is
 * dropped: `/api` -> `/openapi.json`, `https://host:8000/api` -> `https://host:8000/openapi.json`.
 * A split dev setup (absolute base) therefore asks the API it is configured for, not whatever origin
 * served the page. Pure and parameterised so both shapes are testable; `openApiUrl` binds it to the
 * configured base. */
export function openApiUrlForBase(base: string): string {
  const trimmed = base.replace(/\/+$/, "");
  const root = trimmed.endsWith("/api") ? trimmed.slice(0, -"/api".length) : trimmed;
  return `${root}/openapi.json`;
}

export function openApiUrl(): string {
  return openApiUrlForBase(API_BASE_URL);
}

/** Dev mode only. The backend's `dev` auth mode resolves identity from the
 * `X-Forge-User-Id` header, so
 * the chosen identity has to survive a page reload. */
const DEV_IDENTITY_STORAGE_KEY = "forge.devUserId";

/**
 * The identity header is attached from this in-memory value only, and only
 * `IdentityContext` sets it — after the bootstrap has confirmed the backend is
 * in `dev` mode. In `oidc` mode it stays `null` for the process lifetime, so
 * the header is never sent even if a stale `localStorage` key is left over
 * from a previous dev session: no identity header in
 * `oidc` mode. Reading `localStorage` directly inside `request` is what made
 * that guarantee impossible to state before.
 */
let devUserId: string | null = null;

export function readStoredDevUserId(): string | null {
  return localStorage.getItem(DEV_IDENTITY_STORAGE_KEY);
}

export function setDevUserId(userId: string | null): void {
  devUserId = userId;
  if (userId === null) {
    localStorage.removeItem(DEV_IDENTITY_STORAGE_KEY);
  } else {
    localStorage.setItem(DEV_IDENTITY_STORAGE_KEY, userId);
  }
}

/** Called on any `401`, so a session that expired mid-session drops the app
 * into its signed-out state instead of leaving stale data on screen. The
 * `ApiError` is still thrown, so per-call error handling is unaffected. */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/**
 * What kind of failure an `ApiError` is. `request()` throws nothing else
 * for a failed call -- a screen never sees a raw `TypeError` or `SyntaxError`:
 *
 * - `"http"`: the server answered with a non-2xx status. `status` is that
 *   status, `detail` is the body's `detail` as received (`undefined` when the
 *   body had none or was not JSON).
 * - `"network"`: `fetch` itself rejected (server down, offline, CORS refusal,
 *   DNS). There is no response, so `status` is `NETWORK_ERROR_STATUS` (`0`).
 * - `"parse"`: the server answered 2xx but the body was not JSON. `status` is
 *   the 2xx that arrived -- kept for diagnostics, and it can never collide with
 *   the 4xx/5xx a per-status message branches on.
 *
 * An aborted request is deliberately **not** an `ApiError`: cancelling is the
 * caller's own decision, not a failure. The `AbortError` is rethrown as it came
 * and recognised by `isAbortError`.
 */
export type ApiErrorKind = "http" | "network" | "parse";

/** `ApiError.status` when no response arrived at all. */
export const NETWORK_ERROR_STATUS = 0;

/** The Korean copy for a failure that has no response to describe:
 * the one place it is worded. `ApiError.message` carries the same text for the
 * two transport kinds, so even a screen that renders `.message` shows Korean,
 * never the browser's "Failed to fetch". */
export const NETWORK_ERROR_COPY = "서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
export const PARSE_ERROR_COPY = "서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.";

export class ApiError extends Error {
  status: number;
  /** The response's `detail` exactly as received. `message` is only the string
   * form of it, so a caller that needs the structure (`tooLongField`,
   * a pydantic 422 array) reads it here. */
  detail: unknown;
  kind: ApiErrorKind;

  constructor(status: number, message: string, detail?: unknown, kind: ApiErrorKind = "http") {
    super(message);
    this.status = status;
    this.detail = detail;
    this.kind = kind;
  }
}

/** The Korean sentence for a transport failure (`"network"` / `"parse"`), or
 * `null` for anything else -- an `http` error is worded by the screen that knows
 * what it was doing, and a non-`ApiError` has no wording at all. Screens call
 * this instead of rendering `err.message`. */
export function transportErrorMessage(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  if (err.kind === "network") return NETWORK_ERROR_COPY;
  if (err.kind === "parse") return PARSE_ERROR_COPY;
  return null;
}

/** True for the rejection a `fetch` (or a body read) gives when its
 * `AbortSignal` fires. Matched by `name`, not `instanceof DOMException`: the
 * class differs between a browser, node and jsdom, the name does not. */
export function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError";
}

export interface RequestOptions {
  /** Aborting rejects the call with an `AbortError` (see `isAbortError`). */
  signal?: AbortSignal;
}

/** A body that could not be read as JSON. A symbol, so no JSON value can equal it. */
const UNREADABLE = Symbol("unreadable body");

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch (err) {
    // An abort while the body streams is still the caller's cancel, not a bad body.
    if (isAbortError(err)) throw err;
    return UNREADABLE;
  }
}

async function request<T>(
  method: "GET" | "POST" | "PATCH",
  path: string,
  body: unknown,
  options: RequestOptions,
): Promise<T> {
  const hasBody = body !== undefined;
  const headers = new Headers();
  // Only a request that sends JSON says so. A forced `Content-Type:
  // application/json` on every call makes each GET a CORS preflight under the
  // supported cross-origin `VITE_API_BASE_URL` setup (ADR 0028).
  if (hasBody) {
    headers.set("Content-Type", "application/json");
  }
  if (devUserId) {
    headers.set("X-Forge-User-Id", devUserId);
  }

  const init: RequestInit = {
    headers,
    // The platform session is an httpOnly cookie (ADR 0018 §2), so every
    // request has to be credentialed or `oidc` mode is anonymous.
    credentials: "include",
  };
  if (method !== "GET") init.method = method;
  if (hasBody) init.body = JSON.stringify(body);
  if (options.signal) init.signal = options.signal;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init);
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network");
  }

  if (!response.ok) {
    const parsed = await readJson(response);
    const detail =
      typeof parsed === "object" && parsed !== null ? (parsed as { detail?: unknown }).detail : undefined;
    if (response.status === 401) {
      unauthorizedHandler?.();
    }
    const said = typeof detail === "string" && detail !== "" ? detail : response.statusText;
    throw new ApiError(response.status, said, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const parsed = await readJson(response);
  if (parsed === UNREADABLE) {
    throw new ApiError(response.status, PARSE_ERROR_COPY, undefined, "parse");
  }
  return parsed as T;
}

export const api = {
  get: <T>(path: string, options: RequestOptions = {}) => request<T>("GET", path, undefined, options),
  post: <T>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>("POST", path, body, options),
  // For `PATCH /users/{id}/role`. The role-management console itself
  // is a later addition; the verb lands here so that screen does not
  // arrive alongside a change to the request plumbing.
  patch: <T>(path: string, body: unknown, options: RequestOptions = {}) =>
    request<T>("PATCH", path, body, options),
};

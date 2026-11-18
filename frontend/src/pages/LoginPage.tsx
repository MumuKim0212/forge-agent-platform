import { Navigate, useSearchParams } from "react-router-dom";

import { authApi } from "../api/auth";
import { AuthPageShell } from "../components/AuthPageShell";
import { useDocumentTitle } from "../components/documentTitle";
import { LOGIN_TITLE, documentTitle } from "../components/shellNav";
import { Button } from "../components/ui/Button";
import { Skeleton } from "../components/ui/Skeleton";
import { useIdentity } from "../context/IdentityContext";
import { IdentitySwitcherPage } from "./IdentitySwitcherPage";
import { LocalLoginForm } from "./LocalLoginForm";
import { loginViewFor } from "./loginView";

export const DEFAULT_POST_LOGIN_PATH = "/projects";

/** Matches the backend's `_safe_next_path` rule
 * on its own — `?next=` is attacker-supplied in the
 * sense that anyone can hand out a `/login?next=...` link, so it never
 * reaches a navigation raw, and this function must not lean on a library
 * property (a router refusing a cross-origin navigation) for its safety.
 *
 * Rejects: empty, not `/`-prefixed, `//`-prefixed (protocol-relative), a
 * backslash anywhere, or any ASCII control character (U+0000-U+001F,
 * U+007F) anywhere. Everything else passes through unchanged.
 *
 * The backslash check: `/\host` starts with `/` and not `//`,
 * so the original two checks alone admit it, and a URL parser normalises a
 * leading `/\` to `//` (protocol-relative).
 *
 * It carries the control-character check: react-router's `useSearchParams`
 * hands back the decoded value, so `?next=/%09/evil.example` arrives here
 * as a real tab — and the WHATWG URL parser strips tab/LF/CR anywhere in a
 * URL before resolving it, so a tab character in the path resolves the same
 * way, another origin. Not exploitable today: `loginPathFor` (`App.tsx`)
 * `encodeURIComponent`s a pathname the browser already keeps
 * percent-encoded, so the platform's own round trip never hands this
 * function a raw control character — but a hand-built link can. */
const CONTROL_CHARACTER_PATTERN = "[\\x00-\\x1f\\x7f]";
const CONTROL_CHARACTER = new RegExp(CONTROL_CHARACTER_PATTERN);

export function safeNextPath(raw: string | null): string {
  if (
    !raw ||
    !raw.startsWith("/") ||
    raw.startsWith("//") ||
    raw.includes("\\") ||
    CONTROL_CHARACTER.test(raw)
  ) {
    return DEFAULT_POST_LOGIN_PATH;
  }
  return raw;
}

/**
 * `/login`. Which login this is depends on the mode the **backend** reports
 * (`IdentityContext.mode`), so the dev switcher / local login form / SSO
 * panel are not merely hidden for the other two modes — only one of them is
 * ever mounted, decided by `loginViewFor`.
 */
export function LoginPage() {
  const { mode, currentUser, loading } = useIdentity();
  const [searchParams] = useSearchParams();
  const next = safeNextPath(searchParams.get("next"));
  const view = loginViewFor({ loading, mode, signedIn: currentUser !== null });
  useDocumentTitle(documentTitle(LOGIN_TITLE));

  if (view === "loading") {
    return (
      <AuthPageShell>
        <Skeleton label="로그인 화면을 불러오는 중" />
      </AuthPageShell>
    );
  }

  if (view === "dev-switcher") {
    // Deliberately reachable while signed in: this page is also the 전환
    // (switch identity) surface in dev mode.
    return (
      <AuthPageShell>
        <IdentitySwitcherPage next={next} />
      </AuthPageShell>
    );
  }

  if (view === "redirect") return <Navigate to={next} replace />;

  if (view === "local-login") {
    return (
      <AuthPageShell>
        <LocalLoginForm next={next} />
      </AuthPageShell>
    );
  }

  return (
    <AuthPageShell>
      <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">로그인</h2>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        사내 SSO(OIDC)로 로그인합니다. 로그인 후 원래 보려던 화면으로 돌아갑니다.
      </p>
      {/*
        A real browser navigation, not a `fetch`: the IdP round-trip is a
        redirect chain the browser has to own. It is a click
        rather than an automatic redirect on mount for two reasons — an
        automatic one loops indefinitely against the IdP whenever the session
        cookie cannot be stored, and it would make an explicit logout
        immediately bounce back through the IdP's still-live SSO session,
        which reads as a broken logout. See the task Result for the tradeoff.
      */}
      <Button
        className="mt-4 w-full"
        onClick={() => window.location.assign(authApi.loginStartUrl(next))}
      >
        SSO로 로그인
      </Button>
    </AuthPageShell>
  );
}

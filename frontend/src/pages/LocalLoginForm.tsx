import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { localAccountsApi } from "../api/localAccounts";
import { ApiError } from "../api/client";
import { Alert } from "../components/ui/Alert";
import { Button } from "../components/ui/Button";
import { Field } from "../components/ui/Field";
import { Input } from "../components/ui/Input";
import { TextLink } from "../components/ui/TextLink";
import { useIdentity } from "../context/IdentityContext";
import {
  classifyLocalLoginFailure,
  localLoginErrorMessage,
  type LocalLoginFailure,
} from "./localLogin";

/**
 * `local`-mode login.
 * Rendered only by `LoginPage` when the backend reports `local` mode
 * (`loginView.loginViewFor`), so this never mounts against a `dev`/`oidc`
 * backend.
 *
 * The password field is plain component state used only as the POST body:
 * never logged, never put in a URL, never written to `localStorage`, and
 * never echoed back in any message this component renders, including on
 * validation failure.
 */
export function LocalLoginForm({ next }: { next: string }) {
  const { refreshCurrentUser } = useIdentity();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<LocalLoginFailure | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFailure(null);
    setSubmitting(true);
    try {
      await localAccountsApi.login({ email, password });
      // The backend already set the session cookie; this just re-reads
      // `/users/me` so `currentUser` (and therefore `RequireIdentity`)
      // reflects it — the same function the bootstrap effect uses.
      await refreshCurrentUser();
      navigate(next, { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setFailure(classifyLocalLoginFailure(err.status, err.message));
      } else {
        setFailure({
          kind: "error",
          detail: err instanceof Error ? err.message : "로그인 요청에 실패했습니다.",
        });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">로그인</h2>
      <form onSubmit={handleSubmit} className="mt-4">
        <Field label="이메일" htmlFor="local-login-email">
          <Input
            id="local-login-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </Field>
        <Field label="비밀번호" htmlFor="local-login-password">
          <Input
            id="local-login-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </Field>
        <Button
          type="submit"
          className="w-full"
          busy={submitting}
          disabled={!email || !password}
        >
          {submitting ? "로그인 중..." : "로그인"}
        </Button>
      </form>

      {/* The awaiting-approval outcome is a normal
          informational state, not an error toast — a pending user hits this
          repeatedly since nothing notifies them (ADR 0022 §8). Rendered with
          its own, non-crimson styling so it reads as distinct from a real
          failure below. */}
      {failure?.kind === "awaiting_approval" && (
        <Alert variant="info" className="mt-3">
          계정이 관리자 승인을 기다리고 있습니다. 승인 후 같은 정보로 다시 로그인해 주세요.
        </Alert>
      )}
      {/* The backend deliberately merges "unknown email /
          wrong password / locked / never had a password" into one message,
          so which cause it was cannot be guessed from the response alone.
          Interpolating `failure.detail` here would NOT protect that
          property — it would make the property depend on the backend's
          wording forever, and a later, more specific `detail` (a perfectly
          reasonable diagnostic-field change) would silently start leaking
          which cause it was, with nothing here to notice. `localLoginErrorMessage`
          renders one fixed Korean sentence instead and never reads
          `failure.detail`, which keeps the merge true regardless of what
          `detail` ever says — the guarantee now lives here, not borrowed
          from another layer. */}
      {failure?.kind === "error" && (
        <Alert variant="error" live className="mt-3">
          {localLoginErrorMessage(failure)}
        </Alert>
      )}

      <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
        계정이 없으신가요?{" "}
        <TextLink to="/signup">가입 요청</TextLink>
      </p>
    </div>
  );
}

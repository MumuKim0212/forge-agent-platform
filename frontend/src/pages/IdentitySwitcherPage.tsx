import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Alert } from "../components/ui/Alert";
import { Button } from "../components/ui/Button";
import { visibleControls } from "../components/ui/visibleControls";
import { useIdentity } from "../context/IdentityContext";

/**
 * The dev-mode identity switcher. Rendered **only** by `LoginPage` when the
 * backend reports `dev` mode, so in `oidc` mode this component never mounts
 *.
 *
 * Identities come from `GET /dev/identities`, which returns id + display name
 * only — so, unlike before, no role is shown here. The role arrives with
 * `GET /users/me` once an identity is chosen.
 */
/**
 * What the switcher says when picking a dev identity fails.
 *
 * `selectDevIdentity` re-reads `/users/me`, so a rejection here is an
 * `ApiError` from `api/client.ts` — an `Error` subclass, so the previous
 * `err instanceof Error ? err.message : "<Korean>"` always took the
 * `err.message` branch and put the backend's `detail` on screen. The
 * Korean fallback is now the only branch.
 */
export function identitySwitchFailureMessage(_err: unknown, fallback: string): string {
  return fallback;
}

export function IdentitySwitcherPage({ next }: { next: string }) {
  const { devIdentities, selectDevIdentity } = useIdentity();
  const navigate = useNavigate();
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSelect(userId: string) {
    setError(null);
    setSelecting(true);
    try {
      await selectDevIdentity(userId);
      navigate(next, { replace: true });
    } catch (err) {
      setError(identitySwitchFailureMessage(err, "아이덴티티 선택에 실패했습니다."));
    } finally {
      setSelecting(false);
    }
  }

  return (
    <div>
      <Alert variant="warning" className="mb-4">
        개발 모드: 아이덴티티 스위처 — 백엔드가 <code>AUTH_MODE=dev</code>로 동작할 때만
        표시됩니다. 운영 환경에서는 SSO(OIDC) 로그인만 사용됩니다.
      </Alert>
      <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">사용자 선택</h2>
      {devIdentities.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
          선택할 수 있는 사용자가 없습니다.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {devIdentities.map((identity) => (
            <li key={identity.id}>
              <Button
                variant="secondary"
                className="w-full justify-start"
                busy={selecting}
                onClick={() => handleSelect(identity.id)}
              >
                {visibleControls(identity.name)}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
    </div>
  );
}

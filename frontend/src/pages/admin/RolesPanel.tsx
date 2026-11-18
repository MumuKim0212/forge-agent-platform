import { useEffect, useState } from "react";

import { ApiError } from "../../api/client";
import { usersApi } from "../../api/users";
import { Role, canGrantRoles, isSelfRoleChange } from "../../auth/roles";
import { useIdentity } from "../../context/IdentityContext";
import { Alert } from "../../components/ui/Alert";
import { Badge, type BadgeTone } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { Table } from "../../components/ui/Table";
import { visibleControls } from "../../components/ui/visibleControls";
import type { User } from "../../types/api";
import { panelErrorMessage } from "./panelError";
import {
  ROLE_OPTIONS,
  SELF_CHANGE_NOTICE,
  adminCountSentence,
  refreshIdentityAfterRoleChange,
  roleApplyLabel,
  roleApplyText,
  roleChangeFailureMessage,
} from "./roleChange";

/** Presentation-only tone per role, beside the wire value (c4's discipline —
 * the same one `StatusBadge.tsx`'s `STATUS_TONES` uses). Not exported: no
 * caller outside this panel needs it, and the wire values themselves stay
 * `auth/roles.Role`'s alone to spell. */
const ROLE_TONES: Record<string, BadgeTone> = {
  [Role.ADMIN]: "warning",
  [Role.APPROVER]: "accent",
  [Role.REQUESTER]: "neutral",
};

function RoleTag({ role }: { role: string }) {
  return <Badge tone={ROLE_TONES[role] ?? "neutral"}>{role}</Badge>;
}

/**
 * Role management.
 *
 * There is **no user creation and no deactivation** here: users are provisioned
 * JIT on first login (ADR 0018 §3) and deactivation is not modelled, so a form
 * for either would be a control that cannot work.
 *
 * Every change is audited backend-side with actor, target and both old and new
 * values — the console adds a UI over that API, not a new capability.
 */
export function RolesPanel() {
  const { currentUser, refreshCurrentUser } = useIdentity();
  const mayGrant = canGrantRoles(currentUser);

  const [users, setUsers] = useState<User[]>([]);
  // The backend's number, or null while unknown / unreadable. Never derived from `users`.
  const [adminCount, setAdminCount] = useState<number | null>(null);
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changeError, setChangeError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setUsers(await usersApi.list());
    } catch (err) {
      setError(panelErrorMessage(err, "사용자 목록을 불러오지 못했습니다."));
    } finally {
      setLoading(false);
    }
  }

  // Admin-only read: a viewer who may not grant roles gets no count rather than a 403. A failed
  // read leaves the count unknown (the sentence is dropped) instead of falling back to a recount.
  async function loadAdminCount() {
    if (!mayGrant) {
      setAdminCount(null);
      return;
    }
    try {
      setAdminCount((await usersApi.usableAdminCount()).count);
    } catch {
      setAdminCount(null);
    }
  }

  useEffect(() => {
    load();
  }, []);

  // Keyed on the grant right, not run once on mount: the identity may resolve after the panel does.
  useEffect(() => {
    loadAdminCount();
  }, [mayGrant]);

  async function applyRole(target: User) {
    const newRole = selected[target.id] ?? target.role;
    setChangeError(null);
    setNotice(null);
    if (newRole === target.role) {
      setChangeError(`${visibleControls(target.name)}의 역할이 이미 ${target.role}입니다.`);
      return;
    }

    setSaving(target.id);
    try {
      const updated = await usersApi.changeRole(target.id, { role: newRole });
      setNotice(`${visibleControls(updated.name)}의 역할을 ${target.role} → ${updated.role}로 변경했습니다.`);
      // The backend
      // re-reads `role` per request regardless, but an admin who demotes
      // themselves otherwise keeps seeing the old nav/console access until a
      // reload. Only re-reads identity when the change targeted the acting
      // user — a change to someone else does not touch this at all.
      await refreshIdentityAfterRoleChange(isSelfRoleChange(currentUser, target), refreshCurrentUser);
      await load();
      await loadAdminCount();
    } catch (err) {
      // The last-admin refusal (409) is explained rather than shown raw — the
      // backend rejects it, and "cannot demote the last remaining admin" next to
      // a select box reads as a console bug instead of a platform invariant.
      setChangeError(
        err instanceof ApiError
          ? roleChangeFailureMessage(err.status, err.message)
          : panelErrorMessage(err, "역할 변경에 실패했습니다."),
      );
    } finally {
      setSaving(null);
    }
  }

  return (
    <div>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        역할 사다리는 requester &lt; approver &lt; admin이며, 이 세 값 외에는 지정할 수 없습니다.
        {` ${adminCountSentence(adminCount)} `}마지막 admin은 강등할 수 없습니다.
      </p>
      {!mayGrant && (
        <Alert variant="warning" className="mt-3">
          역할 변경은 admin만 가능합니다. 아래 목록은 조회 전용입니다.
        </Alert>
      )}

      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {changeError && (
        <Alert variant="error" live className="mt-3">
          {changeError}
        </Alert>
      )}
      {notice && (
        <Alert variant="success" live className="mt-3">
          {notice}
        </Alert>
      )}

      {loading ? (
        <Skeleton label="사용자 목록 불러오는 중" className="mt-4" />
      ) : (
        <Table className="mt-3">
          <Table.Head>
            <Table.Row>
              <Table.HeaderCell>사용자</Table.HeaderCell>
              <Table.HeaderCell>이메일</Table.HeaderCell>
              <Table.HeaderCell>현재 역할</Table.HeaderCell>
              {mayGrant && <Table.HeaderCell>변경</Table.HeaderCell>}
            </Table.Row>
          </Table.Head>
          <Table.Body>
            {users.map((user) => {
              const isSelf = isSelfRoleChange(currentUser, user);
              return (
                <Table.Row key={user.id}>
                  <Table.Cell>
                    {visibleControls(user.name)}
                    {isSelf && <strong className="text-accent dark:text-indigo-400"> (본인)</strong>}
                    <div className="text-[11px] text-slate-500 dark:text-slate-400">{user.id}</div>
                  </Table.Cell>
                  <Table.Cell>{visibleControls(user.email)}</Table.Cell>
                  <Table.Cell>
                    <RoleTag role={user.role} />
                  </Table.Cell>
                  {mayGrant && (
                    <Table.Cell>
                      <div className="flex items-center gap-2">
                        <Select
                          value={selected[user.id] ?? user.role}
                          onChange={(e) => setSelected({ ...selected, [user.id]: e.target.value })}
                          aria-label={`${visibleControls(user.name)} 역할`}
                          className="w-auto"
                        >
                          {ROLE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </Select>
                        <Button
                          variant="secondary"
                          busy={saving === user.id}
                          aria-label={roleApplyLabel(visibleControls(user.name), saving === user.id)}
                          onClick={() => applyRole(user)}
                        >
                          {roleApplyText(saving === user.id)}
                        </Button>
                      </div>
                      {isSelf && (
                        <Alert variant="warning" className="mt-2">
                          {SELF_CHANGE_NOTICE}
                        </Alert>
                      )}
                    </Table.Cell>
                  )}
                </Table.Row>
              );
            })}
          </Table.Body>
        </Table>
      )}
    </div>
  );
}

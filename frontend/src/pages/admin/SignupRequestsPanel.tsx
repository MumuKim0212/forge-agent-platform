import { useState } from "react";

import { localAccountsApi } from "../../api/localAccounts";
import { canDeactivateUsers, canReviewSignups } from "../../auth/roles";
import { useIdentity } from "../../context/IdentityContext";
import { useLoaderOnChange } from "../../loading/useLoader";
import { Alert } from "../../components/ui/Alert";
import { Badge, type BadgeTone } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { Table } from "../../components/ui/Table";
import { Tabs } from "../../components/ui/Tabs";
import { visibleControls } from "../../components/ui/visibleControls";
import type { PendingSignup } from "../../types/api";
import { panelErrorMessage } from "./panelError";
import { ROLE_OPTIONS } from "./roleChange";
import {
  AccountStatus,
  accountStatusLabel,
  canApprove,
  canDeactivate,
  canReject,
  canReopen,
  signupActionFailureMessage,
  type AccountStatusValue,
} from "./signupRequests";

const STATUS_TABS: ReadonlyArray<{ id: AccountStatusValue; label: string }> = [
  { id: AccountStatus.PENDING, label: "대기중" },
  { id: AccountStatus.ACTIVE, label: "활성" },
  { id: AccountStatus.REJECTED, label: "거부됨" },
];

const STATUS_TONES: Record<AccountStatusValue, BadgeTone> = {
  [AccountStatus.PENDING]: "warning",
  [AccountStatus.ACTIVE]: "success",
  [AccountStatus.REJECTED]: "danger",
};

/**
 * `가입 승인` tab. Lists
 * `User` rows by `status` and offers the transitions each status allows —
 * `canApprove`/`canReject`/`canReopen`/`canDeactivate` in `signupRequests.ts`
 * decide which buttons a row gets, so a row can never be offered a
 * transition the backend would reject as illegal.
 *
 * Two separate role gates, same rung today, same reasoning as
 * `RolesPanel`'s `mayGrant`: `canReviewSignups` (approve/reject/reopen) and
 * `canDeactivateUsers` (deactivate) are different backend endpoint groups,
 * so they stay independently named even though both currently require
 * exactly `admin` — the same rung `AdminConsolePage` already required to
 * reach this panel at all, kept anyway per the standing "a future rung
 * change must not move both silently" discipline.
 *
 * the status strip below is `Tabs kind="filter"`, not
 * `kind="tablist"` — it narrows this list in place rather than switching to
 * a different view, so it keeps the `disabled`+`aria-current` idiom it
 * already had rather than acquiring `role="tab"`/`aria-selected`.
 */
export function SignupRequestsPanel() {
  const { currentUser } = useIdentity();
  const mayReview = canReviewSignups(currentUser);
  const mayDeactivate = canDeactivateUsers(currentUser);
  const mayAct = mayReview || mayDeactivate;

  const [statusFilter, setStatusFilter] = useState<AccountStatusValue>(AccountStatus.PENDING);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedRole, setSelectedRole] = useState<Record<string, string>>({});
  const [reason, setReason] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  // One read per filter, newest wins. The rows are cleared the moment the filter changes
  // and on every reload: a row is an action surface (approve, deactivate), and one left over from
  // another filter -- or from before the action that just changed its status -- would offer
  // buttons the backend answers with a 400.
  const list = useLoaderOnChange<PendingSignup[]>(
    (signal) => localAccountsApi.listSignupRequests(statusFilter, { signal }),
    [statusFilter],
    (err) => panelErrorMessage(err, "가입 요청 목록을 불러오지 못했습니다."),
  );
  const { loading, error } = list;
  const rows = list.data;

  async function runAction(userId: string, action: () => Promise<unknown>, successMessage: string) {
    setActionError(null);
    setNotice(null);
    setBusy(userId);
    try {
      await action();
      setNotice(successMessage);
      // Re-reads the *current* filter (`reload` runs the latest load, so a filter changed while
      // the action was in flight is the one re-read) — an approved row leaves the pending
      // list, a deactivated row leaves the active list, so whichever list is
      // on screen no longer shows a row that just changed status
      // (criterion: "the pending list refreshes after an action, so an
      // approved row does not linger as apparently-pending").
      await list.reload();
    } catch (err) {
      setActionError(signupActionFailureMessage(err, "요청을 처리하지 못했습니다."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        가입 요청과 계정 상태를 검토합니다. 부서는 승인 전 확인용 참고 정보이며 권한과는 무관합니다.
      </p>
      {!mayAct && (
        <Alert variant="warning" className="mt-3">
          승인/거부/재개/비활성화는 admin만 가능합니다. 아래 목록은 조회 전용입니다.
        </Alert>
      )}

      <Tabs
        idBase="signup-status"
        kind="filter"
        tabs={STATUS_TABS}
        activeId={statusFilter}
        onChange={setStatusFilter}
        className="mt-3"
      />

      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {actionError && (
        <Alert variant="error" live className="mt-3">
          {actionError}
        </Alert>
      )}
      {notice && (
        <Alert variant="success" live className="mt-3">
          {notice}
        </Alert>
      )}

      {loading ? (
        <Skeleton label="가입 요청 목록 불러오는 중" className="mt-4" />
      ) : rows === null ? null : rows.length === 0 ? (
        <EmptyState className="mt-4" title="표시할 요청이 없습니다" />
      ) : (
        <Table className="mt-3">
          <Table.Head>
            <Table.Row>
              <Table.HeaderCell>이메일</Table.HeaderCell>
              <Table.HeaderCell>이름</Table.HeaderCell>
              <Table.HeaderCell>부서</Table.HeaderCell>
              <Table.HeaderCell>요청 시각</Table.HeaderCell>
              <Table.HeaderCell>상태</Table.HeaderCell>
              {mayAct && <Table.HeaderCell>작업</Table.HeaderCell>}
            </Table.Row>
          </Table.Head>
          <Table.Body>
            {rows.map((row) => {
              const noAction =
                !(mayReview && canApprove(row.status)) &&
                !(mayReview && canReject(row.status)) &&
                !(mayReview && canReopen(row.status)) &&
                !(mayDeactivate && canDeactivate(row.status));

              return (
                <Table.Row key={row.id}>
                  <Table.Cell>{visibleControls(row.email)}</Table.Cell>
                  <Table.Cell>{visibleControls(row.name)}</Table.Cell>
                  <Table.Cell>{row.department === null ? "—" : visibleControls(row.department)}</Table.Cell>
                  <Table.Cell>{new Date(row.requested_at).toLocaleString()}</Table.Cell>
                  <Table.Cell>
                    <Badge tone={STATUS_TONES[row.status as AccountStatusValue] ?? "neutral"}>
                      {accountStatusLabel(row.status)}
                    </Badge>
                  </Table.Cell>
                  {mayAct && (
                    <Table.Cell>
                      <div className="flex flex-col gap-2">
                        {mayReview && canApprove(row.status) && (
                          <div className="flex items-center gap-2">
                            <Select
                              value={selectedRole[row.id] ?? ROLE_OPTIONS[0].value}
                              onChange={(e) => setSelectedRole({ ...selectedRole, [row.id]: e.target.value })}
                              aria-label={`${visibleControls(row.name)} 승인 역할`}
                              className="w-auto"
                            >
                              {ROLE_OPTIONS.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </Select>
                            <Button
                              variant="primary"
                              busy={busy === row.id}
                              onClick={() =>
                                runAction(
                                  row.id,
                                  () =>
                                    localAccountsApi.approveSignup(row.id, {
                                      role: selectedRole[row.id] ?? null,
                                    }),
                                  `${visibleControls(row.name)}의 가입을 승인했습니다.`,
                                )
                              }
                            >
                              승인
                            </Button>
                          </div>
                        )}
                        {mayReview && canReject(row.status) && (
                          <div className="flex items-center gap-2">
                            <Input
                              placeholder="거부 사유 (선택)"
                              value={reason[row.id] ?? ""}
                              onChange={(e) => setReason({ ...reason, [row.id]: e.target.value })}
                              aria-label={`${visibleControls(row.name)} 거부 사유`}
                              className="w-36"
                            />
                            <Button
                              variant="danger"
                              busy={busy === row.id}
                              onClick={() =>
                                runAction(
                                  row.id,
                                  () => localAccountsApi.rejectSignup(row.id, { reason: reason[row.id] || null }),
                                  `${visibleControls(row.name)}의 가입 요청을 거부했습니다.`,
                                )
                              }
                            >
                              거부
                            </Button>
                          </div>
                        )}
                        {mayReview && canReopen(row.status) && (
                          <Button
                            variant="secondary"
                            busy={busy === row.id}
                            onClick={() =>
                              runAction(
                                row.id,
                                () => localAccountsApi.reopenSignup(row.id),
                                `${visibleControls(row.name)}의 요청을 다시 대기 상태로 되돌렸습니다.`,
                              )
                            }
                          >
                            재개
                          </Button>
                        )}
                        {mayDeactivate && canDeactivate(row.status) && (
                          <Button
                            variant="danger"
                            busy={busy === row.id}
                            onClick={() => {
                              if (
                                !window.confirm(
                                  `${visibleControls(row.name)}(${visibleControls(row.email)})의 접근 권한을 비활성화하시겠습니까?`,
                                )
                              ) {
                                return;
                              }
                              runAction(
                                row.id,
                                () => localAccountsApi.deactivateUser(row.id),
                                `${visibleControls(row.name)}의 계정을 비활성화했습니다.`,
                              );
                            }}
                          >
                            비활성화
                          </Button>
                        )}
                        {noAction && (
                          <span className="text-xs text-slate-500 dark:text-slate-400">처리할 작업 없음</span>
                        )}
                      </div>
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

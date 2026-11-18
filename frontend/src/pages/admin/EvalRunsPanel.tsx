import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/client";
import { evalsApi } from "../../api/evals";
import { canRunEvals } from "../../auth/roles";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { EmptyState } from "../../components/ui/EmptyState";
import { ListRow } from "../../components/ui/ListRow";
import { Skeleton } from "../../components/ui/Skeleton";
import { Table } from "../../components/ui/Table";
import { TextLink } from "../../components/ui/TextLink";
import { useIdentity } from "../../context/IdentityContext";
import type { RunOptions } from "../../loading/loader";
import { useLoader, usePolling } from "../../loading/useLoader";
import type { EvalRun, EvalRunDetail, EvalStatus } from "../../types/api";
import {
  POLL_INTERVAL_MS,
  REASON_LOADING,
  budgetHeadline,
  budgetNote,
  caseRows,
  confirmationView,
  evalStartFailureMessage,
  runOutcomeNote,
  runRows,
  shouldPoll,
  startControlState,
  startSuccessMessage,
} from "./evalRuns";
import { panelErrorMessage } from "./panelError";

const START_BUTTON_ID = "eval-start-button";
const START_NOTE_ID = "eval-start-note";

/**
 * Eval runs: start a run against the selected
 * engine and model, read runs newest first, and read one run's cases.
 *
 * Starting a run spends real money, so the start control states the budget beside it (from the backend's status read), is disabled
 * with the reason shown (a run in progress, an inactive executor, the selected engine unavailable) and
 * asks for an in-page confirmation that names the engine, the model and the most it can spend. It
 * is not `window.confirm`, which an automated browser dismisses without a person seeing it.
 *
 * The write is `POST /api/evals/runs`, not a console path: the console router stays read-only.
 * While a run is in progress the list and the open run are re-read every few seconds; polling stops
 * when none is in progress.
 *
 * Live regions: a successful start is the acting admin's own action, so it renders in an
 * always-mounted polite status; a refused start and a load failure are alerts.
 */
export function EvalRunsPanel() {
  const { currentUser } = useIdentity();
  const mayRun = canRunEvals(currentUser);

  // Three reads, each its own newest-wins loader (`loading/loader.ts`). Before, a slow
  // answer could land after a newer one and overwrite it, and one shared `loadError` was cleared by
  // whichever read succeeded last, hiding a failure of another.
  const runsLoader = useLoader<EvalRun[]>((err) => panelErrorMessage(err, "평가 실행 목록을 불러오지 못했습니다."));
  const statusLoader = useLoader<EvalStatus>((err) => panelErrorMessage(err, "평가 실행 상태를 불러오지 못했습니다."));
  const detailLoader = useLoader<EvalRunDetail>((err) => panelErrorMessage(err, "평가 실행 상세를 불러오지 못했습니다."));
  const runs = runsLoader.data;
  const status = statusLoader.data;
  const detail = detailLoader.data;

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [starting, setStarting] = useState(false);
  // Bumped when the confirmation is left (cancelled, or a start that ended), so the effect below
  // moves focus back once the re-render has settled the start button's disabled state.
  const [focusBack, setFocusBack] = useState(0);

  // The failure to show: the first loader that has one. It is announced (`live`) only when the
  // reader asked for the read -- opening the tab, starting a run. A poll that fails in the
  // background is a quiet note, not an interruption (a live region is an intent, not a tone).
  const failing = [runsLoader, statusLoader, detailLoader].find((loader) => loader.error !== null);
  const loadError = failing?.error ?? null;
  const loadErrorIsBackground = failing?.errorIsBackground ?? false;

  // The run whose cases are open, read by the poll, which is set up once and must see the run that
  // is open now, not the one that was open when it was created.
  const selectedRef = useRef<string | null>(null);
  const confirmRef = useRef<HTMLDivElement | null>(null);

  function selectRun(id: string) {
    selectedRef.current = id;
    setSelectedId(id);
    // Whatever detail read is out belongs to the run that was open.
    detailLoader.abort();
  }

  const readRuns = (options?: RunOptions) => runsLoader.run((signal) => evalsApi.listRuns({ signal }), options);
  const readStatus = (options?: RunOptions) => statusLoader.run((signal) => evalsApi.status({ signal }), options);
  const readDetail = (id: string, options?: RunOptions) =>
    detailLoader.run((signal) => evalsApi.getRun(id, { signal }), options);

  // First load: runs and the status read, and the newest run's cases opened.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetched] = await Promise.all([readRuns(), readStatus()]);
      if (cancelled || fetched.kind !== "ok" || fetched.data.length === 0) return;
      selectRun(fetched.data[0].id);
      void readDetail(fetched.data[0].id);
    })();
    return () => {
      cancelled = true;
    };
    // Mount only: the reads and `selectRun` close over stable loader methods and refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Polling: only while a run is in progress, and the next read is scheduled only after the last
  // one settled (`loading/poller.ts`), so two never overlap. The reads are `background`: they yield
  // to a read the user just asked for instead of aborting it, and show no skeleton.
  const polling = runs !== null && shouldPoll(runs);
  usePolling(
    polling,
    async () => {
      const openId = selectedRef.current;
      await Promise.all([
        readRuns({ background: true }),
        readStatus({ background: true }),
        openId !== null ? readDetail(openId, { background: true }) : undefined,
      ]);
    },
    POLL_INTERVAL_MS,
  );

  const control = startControlState(status, runs);
  const confirmation = status ? confirmationView(status) : null;
  const showConfirmation = mayRun && confirming && !control.disabled && confirmation !== null;

  useEffect(() => {
    if (showConfirmation) confirmRef.current?.focus();
  }, [showConfirmation]);

  // Focus goes back to the start control when the confirmation is left. It runs after the render,
  // because the button is disabled while the confirmation is open and a disabled button cannot take
  // focus; and when the outcome leaves it disabled (a run is now in progress), the note beside it,
  // which says why, takes focus instead of the page. `Button` does not forward a ref, hence the ids.
  useEffect(() => {
    if (focusBack === 0) return;
    const button = document.getElementById(START_BUTTON_ID);
    if (button instanceof HTMLButtonElement && !button.disabled) button.focus();
    else document.getElementById(START_NOTE_ID)?.focus();
  }, [focusBack]);

  async function requestStart() {
    setStartError(null);
    setNotice(null);
    // The confirmation names the engine that will run, so read the status again rather than
    // trusting what was loaded when the tab opened. A read that failed has already put its message
    // on screen; one that was merely superseded by a newer read does not stop the confirmation.
    const [freshRuns, freshSelection] = await Promise.all([
      readRuns({ keepData: true }),
      readStatus({ keepData: true }),
    ]);
    if (freshRuns.kind === "error" || freshSelection.kind === "error") return;
    setConfirming(true);
  }

  function cancelStart() {
    setConfirming(false);
    setFocusBack((n) => n + 1);
  }

  async function confirmStart() {
    if (control.disabled || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const started = await evalsApi.startRun();
      setConfirming(false);
      setNotice(startSuccessMessage(started));
      selectRun(started.id);
      detailLoader.update(() => started);
      await Promise.all([readRuns({ keepData: true }), readStatus({ keepData: true })]);
    } catch (err) {
      setConfirming(false);
      setStartError(
        err instanceof ApiError
          ? evalStartFailureMessage(err.status)
          : panelErrorMessage(err, "평가 실행을 시작하지 못했습니다."),
      );
      await Promise.all([readRuns({ keepData: true }), readStatus({ keepData: true })]);
    } finally {
      setStarting(false);
      setFocusBack((n) => n + 1);
    }
  }

  function open(id: string) {
    selectRun(id);
    void readDetail(id);
  }

  const rows = runs ? runRows(runs) : [];
  const detailRun = detail && detail.id === selectedId ? detail : null;
  const detailRow = detailRun ? runRows([detailRun])[0] : null;
  const outcomeNote = detailRun ? runOutcomeNote(detailRun) : null;

  return (
    <div>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        요청 세트를 현재 선택된 에이전트 엔진과 모델로 실행해 통과율, 비용, 소요 시간을 봅니다. 실행은
        수동으로만 시작하며, 실행 예산을 넘기지 않습니다. 같은 요청 세트 버전끼리 눈으로 비교하세요.
      </p>

      {!mayRun && (
        <Alert variant="warning" className="mt-3">
          평가 실행은 admin만 할 수 있습니다.
        </Alert>
      )}

      {mayRun && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              id={START_BUTTON_ID}
              variant="primary"
              disabled={control.disabled || confirming || starting}
              aria-describedby={START_NOTE_ID}
              onClick={requestStart}
            >
              평가 실행 시작
            </Button>
            <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
              {status ? budgetHeadline(status) : "예산 불러오는 중"}
            </span>
          </div>
          <p id={START_NOTE_ID} tabIndex={-1} className="mt-2 text-sm text-slate-600 dark:text-slate-400 outline-none">
            {control.disabled && control.reason ? control.reason : status ? budgetNote(status) : REASON_LOADING}
          </p>
        </div>
      )}

      {showConfirmation && confirmation && (
        <Card className="mt-3 p-4">
          <div ref={confirmRef} tabIndex={-1} role="group" aria-labelledby="eval-confirm-title" className="outline-none">
            <p id="eval-confirm-title" className="text-sm font-semibold text-slate-900 dark:text-slate-100">
              {confirmation.title}
            </p>
            <ul className="mt-2 space-y-1 text-sm text-slate-700 dark:text-slate-300">
              <li>엔진: {confirmation.engineText}</li>
              <li>모델: {confirmation.modelText}</li>
              <li>최대 지출: {confirmation.maxSpendText}</li>
              <li>{confirmation.setText}</li>
            </ul>
            <Alert variant="warning" className="mt-3">
              {confirmation.warning}
            </Alert>
            <div className="mt-3 flex gap-2">
              <Button variant="danger" busy={starting} onClick={confirmStart}>
                {starting ? "시작하는 중..." : "시작"}
              </Button>
              <Button variant="secondary" disabled={starting} onClick={cancelStart}>
                취소
              </Button>
            </div>
          </div>
        </Card>
      )}

      {startError && (
        <Alert variant="error" live className="mt-3">
          {startError}
        </Alert>
      )}
      {loadError && (
        <Alert variant="error" live={!loadErrorIsBackground} className="mt-3">
          {loadError}
        </Alert>
      )}
      {/* A successful start is the acting admin's own action: a polite status, not an alert. The
          region is always mounted so that its text change is announced. */}
      <div role="status" className="mt-3 empty:hidden">
        {notice && <Alert variant="success">{notice}</Alert>}
      </div>

      <h3 className="mt-6 text-sm font-semibold text-slate-800 dark:text-slate-200">실행 목록 (최신순)</h3>
      {runs === null ? (
        runsLoader.error ? null : <Skeleton label="평가 실행 불러오는 중" className="mt-3" />
      ) : rows.length === 0 ? (
        <EmptyState
          className="mt-3"
          title="아직 평가 실행이 없습니다"
          description="위의 버튼으로 첫 실행을 시작하세요."
        />
      ) : (
        <Table className="mt-2">
          <Table.Head>
            <Table.Row>
              <Table.HeaderCell>엔진 / 모델</Table.HeaderCell>
              <Table.HeaderCell>세트</Table.HeaderCell>
              <Table.HeaderCell>상태</Table.HeaderCell>
              <Table.HeaderCell>통과 / 전체</Table.HeaderCell>
              <Table.HeaderCell>지출 / 예산</Table.HeaderCell>
              <Table.HeaderCell>시작한 사람</Table.HeaderCell>
              <Table.HeaderCell>소요 시간</Table.HeaderCell>
            </Table.Row>
          </Table.Head>
          <Table.Body>
            {rows.map((row) => (
              <ListRow
                key={row.id}
                as="tr"
                selected={row.id === selectedId}
                ariaLabel={`${row.engineText} ${row.modelText} ${row.startedAtText} 실행의 케이스 보기`}
                onActivate={() => open(row.id)}
              >
                <Table.Cell>
                  {row.engineText}
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">{row.modelText}</div>
                </Table.Cell>
                <Table.Cell>
                  {row.versionText}
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">프롬프트 {row.fingerprintText}</div>
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">{row.repairSettingText}</div>
                </Table.Cell>
                <Table.Cell>
                  <Badge tone={row.statusTone}>{row.statusLabel}</Badge>
                </Table.Cell>
                <Table.Cell>{row.passedText}</Table.Cell>
                <Table.Cell>{row.spendText}</Table.Cell>
                <Table.Cell>
                  {row.startedByText}
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">{row.startedAtText}</div>
                </Table.Cell>
                <Table.Cell>{row.durationText}</Table.Cell>
              </ListRow>
            ))}
          </Table.Body>
        </Table>
      )}
      {polling && (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          진행 중인 실행이 있어 {POLL_INTERVAL_MS / 1000}초마다 새로 읽습니다. 지출은 판정이 끝난 케이스까지만
          합산됩니다.
        </p>
      )}

      {selectedId !== null && (
        <section className="mt-6" aria-label="선택한 실행의 케이스">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">
            선택한 실행의 케이스
            {detailRow && (
              <span className="ml-2 font-normal text-slate-600 dark:text-slate-400">
                {detailRow.engineText} · {detailRow.modelText} · {detailRow.versionText} · 프롬프트 {detailRow.fingerprintText} · {detailRow.repairSettingText} · {detailRow.statusLabel}
              </span>
            )}
          </h3>
          {outcomeNote && (
            <Alert variant="warning" className="mt-2">
              {outcomeNote}
            </Alert>
          )}
          {detailRun === null ? (
            detailLoader.error ? null : <Skeleton label="케이스 불러오는 중" className="mt-3" />
          ) : (
            <Table className="mt-2">
              <Table.Head>
                <Table.Row>
                  <Table.HeaderCell>케이스</Table.HeaderCell>
                  <Table.HeaderCell>결과</Table.HeaderCell>
                  <Table.HeaderCell>검사 결과</Table.HeaderCell>
                  <Table.HeaderCell>비용</Table.HeaderCell>
                  <Table.HeaderCell>토큰</Table.HeaderCell>
                  <Table.HeaderCell>수리 시도</Table.HeaderCell>
                  <Table.HeaderCell>소요 시간</Table.HeaderCell>
                  <Table.HeaderCell>빌드</Table.HeaderCell>
                </Table.Row>
              </Table.Head>
              <Table.Body>
                {caseRows(detailRun.cases).map((c) => (
                  <Table.Row key={c.id}>
                    <Table.Cell>
                      <code className="text-xs">{c.caseId}</code>
                    </Table.Cell>
                    <Table.Cell>
                      <Badge tone={c.resultTone}>{c.resultLabel}</Badge>
                      {c.failureText && <div className="mt-1 text-xs text-slate-700 dark:text-slate-300">{c.failureText}</div>}
                    </Table.Cell>
                    <Table.Cell>
                      {c.checkLines.length > 0 ? (
                        <ul className="space-y-0.5 text-xs text-slate-700 dark:text-slate-300">
                          {c.checkLines.map((line, index) => (
                            <li key={index}>{line}</li>
                          ))}
                        </ul>
                      ) : (
                        <span className="text-xs text-slate-500 dark:text-slate-400">—</span>
                      )}
                      {c.policyText && <div className="mt-1 text-xs text-slate-600 dark:text-slate-400">{c.policyText}</div>}
                    </Table.Cell>
                    <Table.Cell>{c.costText}</Table.Cell>
                    <Table.Cell>{c.tokensText}</Table.Cell>
                    <Table.Cell>{c.repairAttemptsText}</Table.Cell>
                    <Table.Cell>{c.durationText}</Table.Cell>
                    <Table.Cell>
                      {c.buildPath ? (
                        <TextLink to={c.buildPath} className="underline">
                          빌드 보기
                        </TextLink>
                      ) : (
                        <span className="text-xs text-slate-500 dark:text-slate-400">—</span>
                      )}
                    </Table.Cell>
                  </Table.Row>
                ))}
              </Table.Body>
            </Table>
          )}
        </section>
      )}
    </div>
  );
}

/**
 * The agent engine panel's decision logic (ADR 0035 §Decision 5).
 * Pure functions over the API's shape, so the rules an operator
 * relies on have a truth table (ADR 0021: nothing can be mounted in the node test environment).
 *
 * The rules this file carries:
 * - An unavailable engine offers NO control (rule 2): `canSelect` is false for it, so the panel
 *   never draws a button that would answer 422.
 * - Availability is the only fact about a key shown. There is no key field in the response, and
 *   none here.
 * - The change applies to builds claimed after it, never to one already running (rule 3).
 * - Who selected the current engine, and when, is stated beside it (rule 5) for a stored
 *   selection. With nothing stored the page says nothing about it, rather than crediting anyone.
 */
import { engineLabel } from "../../components/engineDisplay";
import { visibleControls } from "../../components/ui/visibleControls";
import type { AgentEngineSelection } from "../../types/api";

/** Shown on every unavailable engine, in place of a control. */
export const UNAVAILABLE_REASON = "키가 설정되지 않음";

/** Rule 3: a standing fact about the control, always on screen. */
export const CHANGE_SCOPE_NOTICE =
  "엔진 변경은 그 이후에 시작되는(워커가 가져가는) 빌드부터 적용됩니다. 이미 실행 중인 빌드는 시작할 때 기록된 엔진으로 끝까지 실행되며 바뀌지 않습니다.";

/** What an operator needs to know about a selected engine whose key has gone: builds fail, and
 * the platform does not run them on the other engine (ADR 0035 §Decision 5, no silent fallback). */
export const SELECTED_BUT_UNAVAILABLE_NOTICE =
  "선택된 엔진의 키가 설정되어 있지 않아, 지금 시작되는 빌드는 실패합니다. 다른 엔진으로 대신 실행하지 않습니다. 키를 설정하거나 사용 가능한 엔진을 선택하세요.";

export const NO_MODEL_TEXT = "엔진 기본값 (지정 안 함)";

export interface EngineRowView {
  engine: string;
  label: string;
  available: boolean;
  /** `사용 가능` or UNAVAILABLE_REASON. */
  availabilityText: string;
  modelText: string;
  image: string;
  selected: boolean;
  /** True only for an available engine that is not already the selected one. */
  canSelect: boolean;
  /** Selected, but its key is not configured. */
  selectedButUnavailable: boolean;
}

export function engineRows(selection: AgentEngineSelection): EngineRowView[] {
  return selection.engines.map((e) => ({
    engine: e.engine,
    label: engineLabel(e.engine),
    available: e.available === true,
    availabilityText: e.available === true ? "사용 가능" : UNAVAILABLE_REASON,
    modelText: typeof e.model === "string" && e.model !== "" ? e.model : NO_MODEL_TEXT,
    image: e.image,
    selected: e.selected === true,
    canSelect: e.available === true && e.selected !== true,
    selectedButUnavailable: e.selected === true && e.available !== true,
  }));
}

function defaultFormat(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/**
 * Who selected the current engine and when. `source` is the contract: `"stored"` is an admin's
 * choice (with a name and a time), `"default"` is nobody having chosen yet and yields `null`
 * (nothing is said, so nobody is credited and no element is drawn; the table's 선택됨 badge shows
 * which engine is in effect), and anything else is said to be unknown rather than guessed. The
 * name is a person's, shown as plain text.
 */
export function selectionProvenance(
  selection: AgentEngineSelection,
  format: (iso: string) => string = defaultFormat,
): string | null {
  if (selection.source === "stored") {
    const who = selection.set_by_name ? visibleControls(selection.set_by_name) : "알 수 없는 사용자";
    const line = `선택한 사람: ${who}`;
    return selection.set_at ? `${line} · 선택 시각: ${format(selection.set_at)}` : line;
  }
  if (selection.source === "default") return null;
  return "선택 출처를 알 수 없습니다.";
}

/** The status line after a successful change: the acting user's own action, said politely. */
export function changeSuccessMessage(selection: AgentEngineSelection): string {
  return `에이전트 엔진을 변경했습니다: ${engineLabel(selection.selected)}. 이후 시작되는 빌드부터 적용됩니다.`;
}

/**
 * The message when `PATCH` is refused. Status codes are the contract
 * (`routers/agent_engine.py`: 422 unknown or unavailable engine, 403 not an admin), each
 * explained in Korean; the server's `detail` is never put on screen.
 */
export function engineChangeFailureMessage(status: number): string {
  switch (status) {
    case 422:
      return "이 엔진은 선택할 수 없습니다. 키가 설정되지 않았거나 알 수 없는 엔진입니다. 목록을 새로 고친 뒤 사용 가능한 엔진을 선택하세요.";
    case 403:
      return "에이전트 엔진 선택은 admin만 할 수 있습니다. 방금 본인 권한이 바뀌었다면 새로 고침 후 다시 확인하세요.";
    default:
      return "에이전트 엔진 변경에 실패했습니다. 기존 선택은 그대로입니다.";
  }
}

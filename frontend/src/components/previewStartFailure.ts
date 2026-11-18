// how a preview that failed to start is worded, in the approval console (list and detail) and
// on the timeline. The backend sends a platform-authored code (`Build.preview_start_failure`), never
// exception text; the Korean label is looked up here from the code. One module, so a surface cannot
// word a code differently from another. `previewStartFailure.test.ts` pins one literal per code.
//
// This is not the smoke check's "not measured": that says the platform could not look at a running
// preview, this says there was no running preview to look at.

export const PREVIEW_START_FAILURE_LABELS: Record<string, string> = {
  NO_IMAGE: "프리뷰를 시작하지 못함 (빌드 이미지 없음)",
  IMAGE_MISSING: "프리뷰를 시작하지 못함 (필요한 이미지가 서버에 없음)",
  NETWORK_FAILED: "프리뷰를 시작하지 못함 (프리뷰 네트워크 생성 실패)",
  DATA_SERVICES_FAILED: "프리뷰를 시작하지 못함 (프리뷰용 데이터 서비스 생성 실패)",
  CONTAINER_START_FAILED: "프리뷰를 시작하지 못함 (컨테이너 시작 실패)",
  SIGNING_SECRET_MISSING: "프리뷰를 시작하지 못함 (프리뷰 서명 설정 누락)",
  UNEXPECTED: "프리뷰를 시작하지 못함 (예기치 못한 오류)",
};

/** Shown for a code this client does not know (a newer backend): never a blank. */
export const UNKNOWN_PREVIEW_START_FAILURE_LABEL = "프리뷰를 시작하지 못함";

/** What the operator can do, only where the platform knows the fix. Others have none. */
export const PREVIEW_START_FAILURE_NEXT_STEPS: Record<string, string> = {
  IMAGE_MISSING:
    "운영자 조치: 서버에서 프리뷰에 필요한 이미지(데이터 서비스 이미지 포함)를 내려받은 뒤 새 빌드를 요청하세요.",
  SIGNING_SECRET_MISSING: "운영자 조치: 프리뷰 서명 비밀값 설정을 확인하세요.",
};

export function previewStartFailureLabel(code: string): string {
  return Object.prototype.hasOwnProperty.call(PREVIEW_START_FAILURE_LABELS, code)
    ? PREVIEW_START_FAILURE_LABELS[code]
    : UNKNOWN_PREVIEW_START_FAILURE_LABEL;
}

export function previewStartFailureNextStep(code: string): string | null {
  return Object.prototype.hasOwnProperty.call(PREVIEW_START_FAILURE_NEXT_STEPS, code)
    ? PREVIEW_START_FAILURE_NEXT_STEPS[code]
    : null;
}

/** Reads the code out of an untyped value (a timeline entry's `data`); null for anything else. */
export function asPreviewStartFailure(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

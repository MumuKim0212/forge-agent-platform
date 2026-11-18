import { Badge, type BadgeTone } from "./ui/Badge";

const STATUS_LABELS: Record<string, string> = {
  SUBMITTED: "제출됨",
  DRAFT: "초안",
  BUILD_PENDING: "빌드 대기",
  PREVIEW_READY: "프리뷰 준비됨",
  AWAITING_APPROVAL: "승인 대기",
  ACTIVE: "운영중",
  ARCHIVED: "보관됨",
  NOT_IMPLEMENTED: "빌드 미구현",
  QUEUED: "대기열",
  RUNNING: "실행중",
  FAILED: "실패",
  SUCCEEDED: "성공",
  DEPLOYING: "배포중",
  SUPERSEDED: "이전 버전",
  STOPPED: "중지됨",
};

/**
 * which `Badge` tone each status reads as. Kept in this file, not in
 * the `Badge` primitive (c4 — primitives carry no status knowledge), because
 * this is exactly where the status→label knowledge already lived; adding a
 * status→tone map beside it keeps both facts about a status in one place.
 * Every key/label above is unchanged — only the shape wrapping them moved.
 */
const STATUS_TONES: Record<string, BadgeTone> = {
  SUBMITTED: "neutral",
  DRAFT: "neutral",
  BUILD_PENDING: "neutral",
  PREVIEW_READY: "accent",
  AWAITING_APPROVAL: "warning",
  ACTIVE: "success",
  ARCHIVED: "neutral",
  NOT_IMPLEMENTED: "warning",
  QUEUED: "neutral",
  RUNNING: "accent",
  FAILED: "danger",
  SUCCEEDED: "success",
  DEPLOYING: "accent",
  SUPERSEDED: "neutral",
  STOPPED: "neutral",
};

export function StatusBadge({ status }: { status: string }) {
  const label = STATUS_LABELS[status] ?? status;
  const tone = STATUS_TONES[status] ?? "neutral";
  return <Badge tone={tone}>{label}</Badge>;
}

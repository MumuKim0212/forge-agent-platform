// why a QUEUED build has not started, in words a requester can read.
//
// The backend sends `queue` on a build timeline entry only while the build is QUEUED
// (`build_queue.queue_wait`): `{ ahead, at_cap }`, two platform-measured values. Every other
// status carries `null`, so this function needs no status comparison -- a value means "waiting".
// It makes no promise about time: the platform cannot say when a worker will be free.

export interface QueueWait {
  /** Builds, across the whole platform, QUEUED or RUNNING and created before this one. */
  ahead: number;
  /** Whether the running builds already fill the platform's concurrency cap. */
  atCap: boolean;
}

/** The server's value, or null for anything that is not exactly `{ahead: int >= 0, at_cap: bool}`. */
export function asQueueWait(value: unknown): QueueWait | null {
  if (typeof value !== "object" || value === null) return null;
  const { ahead, at_cap: atCap } = value as Record<string, unknown>;
  if (typeof ahead !== "number" || !Number.isInteger(ahead) || ahead < 0) return null;
  if (typeof atCap !== "boolean") return null;
  return { ahead, atCap };
}

/** The sentence under a QUEUED build, or null when the entry carries no queue value. */
export function queueWaitLine(value: unknown): string | null {
  const wait = asQueueWait(value);
  if (wait === null) return null;
  if (wait.ahead > 0) {
    const base = `앞에 ${wait.ahead}개의 빌드가 대기 중입니다.`;
    return wait.atCap ? `${base} 지금은 동시에 실행할 수 있는 빌드가 가득 차 있습니다.` : base;
  }
  return wait.atCap
    ? "동시에 실행할 수 있는 빌드가 가득 차 있어 순서를 기다리는 중입니다."
    : "빌드를 시작할 작업자를 기다리는 중입니다.";
}

/**
 * the sentence under a QUEUED build. Decision logic, not rendering (ADR 0021): what the
 * backend's `queue` value becomes, and that anything that is not that value becomes nothing.
 */
import { describe, expect, it } from "vitest";

import { asQueueWait, queueWaitLine } from "./buildQueue";

describe("queueWaitLine", () => {
  it("says how many builds are ahead", () => {
    expect(queueWaitLine({ ahead: 1, at_cap: false })).toBe("앞에 1개의 빌드가 대기 중입니다.");
    expect(queueWaitLine({ ahead: 7, at_cap: false })).toBe("앞에 7개의 빌드가 대기 중입니다.");
  });

  it("adds that the concurrency cap is full when it is", () => {
    expect(queueWaitLine({ ahead: 3, at_cap: true })).toBe(
      "앞에 3개의 빌드가 대기 중입니다. 지금은 동시에 실행할 수 있는 빌드가 가득 차 있습니다.",
    );
  });

  it("with nothing ahead says it waits for a worker, and promises no time", () => {
    const line = queueWaitLine({ ahead: 0, at_cap: false });

    expect(line).toBe("빌드를 시작할 작업자를 기다리는 중입니다.");
    expect(line).not.toMatch(/곧|분|초|시간|잠시/);
  });

  it("with nothing ahead and the cap full says so", () => {
    expect(queueWaitLine({ ahead: 0, at_cap: true })).toBe(
      "동시에 실행할 수 있는 빌드가 가득 차 있어 순서를 기다리는 중입니다.",
    );
  });

  it("renders nothing for an entry without a queue value (every status but QUEUED)", () => {
    for (const value of [null, undefined, "QUEUED", 3, [], {}]) {
      expect(queueWaitLine(value), String(value)).toBeNull();
    }
  });

  it("renders nothing for a value that is not exactly {ahead: int >= 0, at_cap: boolean}", () => {
    const bad = [
      { ahead: -1, at_cap: false },
      { ahead: 1.5, at_cap: false },
      { ahead: "2", at_cap: false },
      { ahead: 2, at_cap: "yes" },
      { ahead: 2 },
      { at_cap: true },
    ];
    for (const value of bad) {
      expect(queueWaitLine(value), JSON.stringify(value)).toBeNull();
    }
  });
});

describe("asQueueWait", () => {
  it("maps the wire names to camelCase", () => {
    expect(asQueueWait({ ahead: 2, at_cap: true })).toEqual({ ahead: 2, atCap: true });
  });
});

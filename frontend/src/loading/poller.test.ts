/**
 * The poller's one promise: the next read starts only after the previous one has settled.
 * Driven with fake timers so a read can take as long as the test says, including longer than the
 * interval -- the case `setInterval` gets wrong.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createPoller } from "./poller";

const INTERVAL = 1000;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** A read that takes `duration` ms of fake time and records how many ran at once. */
function slowRead(duration: number) {
  const stats = { started: 0, finished: 0, active: 0, maxActive: 0 };
  const read = async () => {
    stats.started += 1;
    stats.active += 1;
    stats.maxActive = Math.max(stats.maxActive, stats.active);
    // A read that takes no time must not borrow a timer: a fake timer of 0 ms still takes a tick.
    if (duration > 0) await new Promise<void>((resolve) => setTimeout(resolve, duration));
    stats.active -= 1;
    stats.finished += 1;
  };
  return { read, stats };
}

describe("createPoller", () => {
  it("does not read at once: the first read is one interval after start", async () => {
    const { read, stats } = slowRead(0);
    createPoller(read, INTERVAL).start();

    await vi.advanceTimersByTimeAsync(INTERVAL - 1);
    expect(stats.started).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(stats.started).toBe(1);
  });

  it("never overlaps reads, even when a read takes several intervals", async () => {
    const { read, stats } = slowRead(3500);
    createPoller(read, INTERVAL).start();

    await vi.advanceTimersByTimeAsync(30_000);

    expect(stats.finished).toBeGreaterThan(2);
    expect(stats.maxActive).toBe(1);
  });

  it("schedules the next read one interval after the previous one finished, not after it started", async () => {
    const { read, stats } = slowRead(3500);
    createPoller(read, INTERVAL).start();

    // Read 1: starts at 1000, finishes at 4500. Read 2 therefore starts at 5500.
    await vi.advanceTimersByTimeAsync(5499);
    expect(stats.started).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(stats.started).toBe(2);
  });

  it("keeps a steady cadence when reads are quick", async () => {
    const { read, stats } = slowRead(0);
    createPoller(read, INTERVAL).start();

    await vi.advanceTimersByTimeAsync(5 * INTERVAL);

    expect(stats.started).toBe(5);
  });

  it("stop cancels the pending read", async () => {
    const { read, stats } = slowRead(0);
    const poller = createPoller(read, INTERVAL);
    poller.start();

    await vi.advanceTimersByTimeAsync(INTERVAL / 2);
    poller.stop();
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);

    expect(stats.started).toBe(0);
  });

  it("lets a read that is in flight finish on stop, and schedules nothing after it", async () => {
    const { read, stats } = slowRead(3500);
    const poller = createPoller(read, INTERVAL);
    poller.start();
    await vi.advanceTimersByTimeAsync(INTERVAL + 100);
    expect(stats.active).toBe(1);

    poller.stop();
    await vi.advanceTimersByTimeAsync(20 * INTERVAL);

    expect(stats.finished).toBe(1);
    expect(stats.started).toBe(1);
  });

  it("waits for a read still in flight when stopped and started again, so a restart cannot overlap it", async () => {
    const { read, stats } = slowRead(3500);
    const poller = createPoller(read, INTERVAL);
    poller.start();
    await vi.advanceTimersByTimeAsync(INTERVAL + 100); // read 1 is out, until 4500
    poller.stop();
    poller.start();

    // No second read while the first is out; the next starts one interval after it finishes (5500).
    await vi.advanceTimersByTimeAsync(4300); // 1100 -> 5400: read 1 ended at 4500
    expect(stats.started).toBe(1);
    await vi.advanceTimersByTimeAsync(100); // 5500
    expect(stats.started).toBe(2);
    await vi.advanceTimersByTimeAsync(40 * INTERVAL);
    expect(stats.maxActive).toBe(1);
  });

  it("does not run two chains when started again after a stop that left no read in flight", async () => {
    const { read, stats } = slowRead(0);
    const poller = createPoller(read, INTERVAL);
    poller.start();
    poller.stop();
    poller.start();

    await vi.advanceTimersByTimeAsync(3 * INTERVAL);

    expect(stats.started).toBe(3);
  });

  it("ignores a second start while running", async () => {
    const { read, stats } = slowRead(0);
    const poller = createPoller(read, INTERVAL);
    poller.start();
    poller.start();

    await vi.advanceTimersByTimeAsync(3 * INTERVAL);

    expect(stats.started).toBe(3);
  });

  it("keeps polling after a read that throws", async () => {
    let calls = 0;
    const poller = createPoller(async () => {
      calls += 1;
      throw new Error("boom");
    }, INTERVAL);
    poller.start();

    await vi.advanceTimersByTimeAsync(3 * INTERVAL);

    expect(calls).toBe(3);
  });
});

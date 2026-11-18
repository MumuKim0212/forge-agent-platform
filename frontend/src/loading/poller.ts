/**
 * Repeating a read without ever overlapping it.
 *
 * A `setInterval` fires on the clock, so a response slower than the interval means the next read
 * starts while the last is still out: two requests in flight, answers arriving in either order,
 * and a load on a server that is already slow. This schedules the next read with a `setTimeout`
 * only after the previous one has settled, so there is never more than one, by construction --
 * including across a `stop()` and a `start()` while a read is still out: the restart waits for it.
 *
 * No React and no timers captured at import, so a `node` test can drive it with fake timers
 * (ADR 0021). `useLoader.ts` has the hook.
 */
export interface Poller {
  /** Schedule the first read one interval from now (the page has just loaded the first itself), or
   * one interval after the read that is still in flight. A second `start()` while running changes
   * nothing. */
  start(): void;
  /** Stop: cancel the pending read and let one that is in flight finish without scheduling another. */
  stop(): void;
}

export function createPoller(poll: () => Promise<unknown>, intervalMs: number): Poller {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  // True from the moment a read starts until it settles, whether or not polling was stopped meanwhile.
  let reading = false;

  function schedule(): void {
    if (timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      void tick();
    }, intervalMs);
  }

  async function tick(): Promise<void> {
    reading = true;
    try {
      await poll();
    } catch {
      // A read that throws must not end the polling; the caller words its own failures.
    }
    reading = false;
    if (running) schedule();
  }

  return {
    start() {
      if (running) return;
      running = true;
      // A read still out from before a stop schedules the next one itself when it settles.
      if (!reading) schedule();
    },
    stop() {
      running = false;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}

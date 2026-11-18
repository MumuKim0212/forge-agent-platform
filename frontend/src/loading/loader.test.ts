/**
 * The shared loader's decision logic, with no React and no network (ADR 0021): which
 * response a screen may show, and what a cancelled load is.
 *
 * The loads here are controlled by hand (`deferred`), and the stale-result tests use a load that
 * **ignores its `AbortSignal`**, like a request already past the point of no return. That is on
 * purpose: those tests have to reach the sequence check, not the abort. With an obedient load the
 * abort would reject the old promise and the check would never be what stopped the overwrite.
 */
import { describe, expect, it, vi } from "vitest";

import { createLoader, type LoadState } from "./loader";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A load that honours its signal the way `fetch` does: reject with an AbortError once it fires. */
function abortable<T>(signal: AbortSignal, finish: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
    finish.then(resolve, reject);
  });
}

function makeLoader<T = string>(options: { initialLoading?: boolean } = {}) {
  const states: LoadState<T>[] = [];
  const errorMessage = vi.fn((err: unknown) => `failed: ${(err as Error).message}`);
  const loader = createLoader<T>({
    errorMessage,
    onChange: (state) => states.push(state),
    initialLoading: options.initialLoading,
  });
  return { loader, states, errorMessage };
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe("a stale result is ignored", () => {
  it("does not let an older load overwrite a newer one that answered first", async () => {
    const { loader, states } = makeLoader();
    const first = deferred<string>();
    const second = deferred<string>();

    const a = loader.run(() => first.promise);
    const b = loader.run(() => second.promise);
    second.resolve("second");
    await b;
    const settledStates = states.length;
    first.resolve("first");

    expect(await a).toEqual({ kind: "stale" });
    expect(loader.getState().data).toBe("second");
    // The late answer produced no state at all, not merely an equal one.
    expect(states.length).toBe(settledStates);
  });

  it("does not let an older load's failure replace a newer load's result either", async () => {
    const { loader, errorMessage } = makeLoader();
    const first = deferred<string>();
    const second = deferred<string>();

    const a = loader.run(() => first.promise);
    const b = loader.run(() => second.promise);
    second.resolve("second");
    await b;
    first.reject(new Error("boom"));

    expect(await a).toEqual({ kind: "stale" });
    expect(loader.getState()).toMatchObject({ data: "second", error: null, loading: false });
    expect(errorMessage).not.toHaveBeenCalled();
  });

  it("takes the newer answer even when it arrives last", async () => {
    const { loader } = makeLoader();
    const first = deferred<string>();
    const second = deferred<string>();

    const a = loader.run(() => first.promise);
    const b = loader.run(() => second.promise);
    first.resolve("first");
    await a;
    expect(loader.getState().data).toBeNull();
    second.resolve("second");

    expect(await b).toEqual({ kind: "ok", data: "second" });
    expect(loader.getState().data).toBe("second");
  });
});

describe("a new load aborts the previous one", () => {
  it("fires the previous run's signal, and leaves its own live", () => {
    const { loader } = makeLoader();
    const signals: AbortSignal[] = [];
    const never = new Promise<string>(() => undefined);

    void loader.run((signal) => {
      signals.push(signal);
      return never;
    });
    void loader.run((signal) => {
      signals.push(signal);
      return never;
    });

    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
  });

  it("hands each run its own signal", () => {
    const { loader } = makeLoader();
    const signals: AbortSignal[] = [];

    void loader.run((signal) => {
      signals.push(signal);
      return Promise.resolve("a");
    });
    void loader.run((signal) => {
      signals.push(signal);
      return Promise.resolve("b");
    });

    expect(signals[0]).not.toBe(signals[1]);
  });
});

describe("an aborted load is never an error", () => {
  it("resolves stale and raises no error when a newer run aborts it", async () => {
    const { loader, states, errorMessage } = makeLoader();
    const never = new Promise<string>(() => undefined);
    const second = deferred<string>();

    const a = loader.run((signal) => abortable(signal, never));
    const b = loader.run(() => second.promise);
    second.resolve("second");

    expect(await a).toEqual({ kind: "stale" });
    expect(await b).toEqual({ kind: "ok", data: "second" });
    expect(states.every((s) => s.error === null)).toBe(true);
    expect(errorMessage).not.toHaveBeenCalled();
  });

  it("raises no error and stops loading when the screen abandons the load", async () => {
    const { loader, states, errorMessage } = makeLoader();
    const never = new Promise<string>(() => undefined);

    const a = loader.run((signal) => abortable(signal, never));
    loader.abort();

    expect(await a).toEqual({ kind: "stale" });
    expect(loader.getState()).toMatchObject({ loading: false, error: null });
    expect(states.every((s) => s.error === null)).toBe(true);
    expect(errorMessage).not.toHaveBeenCalled();
  });

  it("keeps the data it had when a load is abandoned", async () => {
    const { loader } = makeLoader();
    await loader.run(() => Promise.resolve("kept"));
    const never = new Promise<string>(() => undefined);

    void loader.run((signal) => abortable(signal, never), { keepData: true });
    loader.abort();

    expect(loader.getState()).toMatchObject({ data: "kept", loading: false, error: null });
  });

  it("settles loading, with no error, when the newest run is aborted by someone else", async () => {
    // Nothing in the loader does this -- but if a load throws an AbortError of its own on the
    // newest run, it must not become an error banner or leave a skeleton up forever.
    const { loader, errorMessage } = makeLoader();

    const outcome = await loader.run(() => Promise.reject(new DOMException("x", "AbortError")));

    expect(outcome).toEqual({ kind: "stale" });
    expect(loader.getState()).toMatchObject({ loading: false, error: null });
    expect(errorMessage).not.toHaveBeenCalled();
  });

  it("still reports a real failure", async () => {
    const { loader, errorMessage } = makeLoader();

    const outcome = await loader.run(() => Promise.reject(new Error("boom")));

    expect(outcome).toEqual({ kind: "error", message: "failed: boom" });
    expect(loader.getState()).toMatchObject({ error: "failed: boom", loading: false, errorIsBackground: false });
    expect(errorMessage).toHaveBeenCalledTimes(1);
  });

  it("treats a load that throws before returning a promise as a failure, not a crash", async () => {
    const { loader } = makeLoader();

    const outcome = await loader.run(() => {
      throw new RangeError("not usable as a path segment");
    });

    expect(outcome.kind).toBe("error");
    expect(loader.getState().loading).toBe(false);
  });
});

describe("what a run does to the state", () => {
  it("starts loading, clears the previous data and error, then shows the result", async () => {
    const { loader, states } = makeLoader();
    await loader.run(() => Promise.reject(new Error("first fails")));
    states.length = 0;
    const pending = deferred<string>();

    const run = loader.run(() => pending.promise);
    expect(states[0]).toEqual({ data: null, loading: true, error: null, errorIsBackground: false });
    pending.resolve("ok");
    await run;

    expect(states[states.length - 1]).toEqual({ data: "ok", loading: false, error: null, errorIsBackground: false });
  });

  it("clears the previous data when the run starts, so nothing from the old inputs shows under the new", async () => {
    const { loader } = makeLoader();
    await loader.run(() => Promise.resolve("old"));
    const pending = deferred<string>();

    void loader.run(() => pending.promise);

    expect(loader.getState().data).toBeNull();
  });

  it("keeps the previous data while a keepData run is in flight, and after it fails", async () => {
    const { loader } = makeLoader();
    await loader.run(() => Promise.resolve("old"));
    const pending = deferred<string>();

    const run = loader.run(() => pending.promise, { keepData: true });
    expect(loader.getState()).toMatchObject({ data: "old", loading: true });
    pending.reject(new Error("boom"));
    await run;

    expect(loader.getState()).toMatchObject({ data: "old", loading: false, error: "failed: boom" });
  });

  it("starts in loading when asked to, for a screen that loads on mount", () => {
    expect(makeLoader({ initialLoading: true }).loader.getState().loading).toBe(true);
    expect(makeLoader().loader.getState().loading).toBe(false);
  });

  it("clears a failure with clearError, and only then", async () => {
    const { loader, states } = makeLoader();
    await loader.run(() => Promise.reject(new Error("boom")));
    const before = states.length;

    loader.clearError();
    expect(loader.getState().error).toBeNull();
    loader.clearError();

    // The second call had nothing to clear and emitted nothing.
    expect(states.length).toBe(before + 1);
  });

  it("changes the data in place with update, without disturbing a load in flight", async () => {
    const { loader } = makeLoader();
    await loader.run(() => Promise.resolve("first"));
    const pending = deferred<string>();
    const run = loader.run(() => pending.promise, { keepData: true });

    loader.update((current) => `${current}+edited`);
    expect(loader.getState()).toMatchObject({ data: "first+edited", loading: true });
    pending.resolve("second");

    // The load that was in flight still lands: `update` is not a newer load.
    expect(await run).toEqual({ kind: "ok", data: "second" });
    expect(loader.getState().data).toBe("second");
  });
});

describe("a background run (a poll)", () => {
  it("is skipped while a foreground load is in flight, and does not abort it", async () => {
    const { loader } = makeLoader();
    const pending = deferred<string>();
    let foregroundSignal: AbortSignal | undefined;

    const foreground = loader.run((signal) => {
      foregroundSignal = signal;
      return pending.promise;
    });
    const poll = await loader.run(() => Promise.resolve("poll"), { background: true });

    expect(poll).toEqual({ kind: "skipped" });
    expect(foregroundSignal?.aborted).toBe(false);
    pending.resolve("foreground");
    expect(await foreground).toEqual({ kind: "ok", data: "foreground" });
    expect(loader.getState().data).toBe("foreground");
  });

  it("is superseded by a foreground load, which aborts it", async () => {
    const { loader } = makeLoader();
    const poll = deferred<string>();
    const foreground = deferred<string>();
    let pollSignal: AbortSignal | undefined;

    const backgroundRun = loader.run(
      (signal) => {
        pollSignal = signal;
        return poll.promise;
      },
      { background: true },
    );
    const foregroundRun = loader.run(() => foreground.promise);
    foreground.resolve("foreground");
    await foregroundRun;
    poll.resolve("poll");

    expect(pollSignal?.aborted).toBe(true);
    expect(await backgroundRun).toEqual({ kind: "stale" });
    expect(loader.getState().data).toBe("foreground");
  });

  it("does not flash loading, and keeps the data it is refreshing", async () => {
    const { loader, states } = makeLoader();
    await loader.run(() => Promise.resolve("old"));
    states.length = 0;
    const pending = deferred<string>();

    const run = loader.run(() => pending.promise, { background: true });
    expect(states).toEqual([]);
    expect(loader.getState()).toMatchObject({ data: "old", loading: false });
    pending.resolve("new");
    await run;

    expect(loader.getState()).toMatchObject({ data: "new", loading: false, error: null });
  });

  it("flags its failure as a background one, keeps the data, and a later success clears it", async () => {
    const { loader } = makeLoader();
    await loader.run(() => Promise.resolve("old"));

    await loader.run(() => Promise.reject(new Error("boom")), { background: true });
    expect(loader.getState()).toMatchObject({ data: "old", error: "failed: boom", errorIsBackground: true });

    await loader.run(() => Promise.resolve("new"), { background: true });
    expect(loader.getState()).toMatchObject({ data: "new", error: null, errorIsBackground: false });
  });

  it("can run when nothing is in flight, from a loader that never had a foreground run", async () => {
    const { loader } = makeLoader();

    expect(await loader.run(() => Promise.resolve("first"), { background: true })).toEqual({
      kind: "ok",
      data: "first",
    });
  });
});

describe("unmounting", () => {
  it("emits nothing after dispose, aborts what is in flight, and resolves it stale", async () => {
    const { loader, states } = makeLoader();
    const pending = deferred<string>();
    let signal: AbortSignal | undefined;

    const run = loader.run((s) => {
      signal = s;
      return pending.promise;
    });
    const emittedBefore = states.length;
    loader.dispose();
    pending.resolve("late");

    expect(signal?.aborted).toBe(true);
    expect(await run).toEqual({ kind: "stale" });
    expect(states.length).toBe(emittedBefore);
  });

  it("does not start a run once disposed", async () => {
    const { loader } = makeLoader();
    loader.dispose();
    const load = vi.fn(() => Promise.resolve("x"));

    expect(await loader.run(load)).toEqual({ kind: "stale" });
    expect(load).not.toHaveBeenCalled();
  });

  it("works again after activate, replaying the state the screen missed (StrictMode remount)", async () => {
    const { loader, states } = makeLoader({ initialLoading: true });
    const pending = deferred<string>();
    void loader.run(() => pending.promise);
    loader.dispose();
    states.length = 0;

    loader.activate();
    expect(states).toEqual([loader.getState()]);
    const run = loader.run(() => Promise.resolve("again"));

    expect(await run).toEqual({ kind: "ok", data: "again" });
    expect(loader.getState().data).toBe("again");
  });

  it("ignores activate when it was never disposed", () => {
    const { loader, states } = makeLoader();

    loader.activate();

    expect(states).toEqual([]);
  });
});

describe("run resolves for every caller", () => {
  it("never leaves an awaiting caller hanging when its load was superseded", async () => {
    const { loader } = makeLoader();
    const never = new Promise<string>(() => undefined);

    const a = loader.run((signal) => abortable(signal, never));
    void loader.run(() => Promise.resolve("b"));
    await tick();

    await expect(a).resolves.toEqual({ kind: "stale" });
  });
});

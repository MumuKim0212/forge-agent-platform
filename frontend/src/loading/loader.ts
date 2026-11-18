/**
 * The shared loader's decision logic: which response a screen may show, and what a
 * cancelled load is. Plain TypeScript with no React, so a `node` test reaches all of it (ADR 0021);
 * `useLoader.ts` is the thin hook that feeds `onChange` into `useState`.
 *
 * It exists because every page hand-rolled `load()` -- set loading, await, set data, catch, clear
 * loading -- and none of them handled the second call arriving before the first came back. The
 * rules, all of them enforced here and nowhere else:
 *
 * - **Latest wins.** Each `run` takes the next sequence number; a result is shown only if its
 *   number is still the newest. The check does not rely on the abort having worked: a `fetch` that
 *   ignores its signal, or a body that was already read, still cannot overwrite a newer result.
 * - **A new run aborts the previous one** (its `AbortSignal` fires), so a superseded request stops
 *   costing the server and the connection pool, not only the screen.
 * - **A cancelled load is never an error.** Superseded, aborted or disposed, it resolves as
 *   `"stale"` and touches no state.
 * - **A background run yields.** A poll must not cancel a load the user just asked for, so a
 *   `background` run is skipped while a foreground one is in flight, and a foreground run always
 *   supersedes a background one. Its failures are flagged (`errorIsBackground`) so the screen can
 *   show them quietly -- a failed poll is not news the user acted to get.
 */
import { isAbortError } from "../api/client";

export interface LoadState<T> {
  /** The last result that is allowed to be on screen; `null` before the first. */
  data: T | null;
  /** A foreground load is in flight. A background run never sets this: a poll must not flash a skeleton. */
  loading: boolean;
  /** The message for the failure of the newest run (already worded by `errorMessage`), else `null`. */
  error: string | null;
  /** `error` came from a background run. */
  errorIsBackground: boolean;
}

export type LoadOutcome<T> =
  /** The load succeeded, was the newest, and is now `state.data`. */
  | { kind: "ok"; data: T }
  /** The load failed and was the newest; `message` is now `state.error`. */
  | { kind: "error"; message: string }
  /** The load no longer counts: a newer run superseded it, or it was aborted or disposed. No state changed. */
  | { kind: "stale" }
  /** A background run that did not start because a foreground load was in flight. */
  | { kind: "skipped" };

export interface RunOptions {
  /** Keep showing the previous data while this run is in flight. Default: clear it, so nothing
   * from the previous inputs is on screen under the new ones. Ignored for a background run, which
   * always keeps it. */
  keepData?: boolean;
  /** A poll or other refresh nobody is waiting on; see the file docblock. */
  background?: boolean;
}

export interface Loader<T> {
  getState(): LoadState<T>;
  /** `load` is called with the signal of this run, and may throw synchronously or reject. */
  run(load: (signal: AbortSignal) => Promise<T>, options?: RunOptions): Promise<LoadOutcome<T>>;
  /** Abandon whatever is in flight and stop its loading state. The data stays. */
  abort(): void;
  /** Change the data without loading: for a result the page already holds (a write's response).
   * Does not touch an in-flight load, so a later `run` still lands over it. */
  update(change: (current: T | null) => T | null): void;
  clearError(): void;
  /** Unmount: abort and stop emitting. Reversible by `activate()`, because React's StrictMode
   * unmounts and remounts a component once in development. */
  dispose(): void;
  /** (Re)start emitting; replays the current state, which may have moved while disposed. */
  activate(): void;
}

export interface LoaderConfig<T> {
  /** Words a failure. Called only for a real failure of the newest run, never for an abort. */
  errorMessage: (err: unknown) => string;
  onChange: (state: LoadState<T>) => void;
  /** `loading` before the first run (a screen that loads on mount starts in its skeleton). */
  initialLoading?: boolean;
}

export function createLoader<T>(config: LoaderConfig<T>): Loader<T> {
  let state: LoadState<T> = {
    data: null,
    loading: config.initialLoading ?? false,
    error: null,
    errorIsBackground: false,
  };
  let sequence = 0;
  let inFlight: { controller: AbortController; background: boolean } | null = null;
  let disposed = false;

  function emit(next: LoadState<T>): void {
    state = next;
    if (!disposed) config.onChange(next);
  }

  function abandon(): void {
    sequence += 1;
    inFlight?.controller.abort();
    inFlight = null;
  }

  async function run(
    load: (signal: AbortSignal) => Promise<T>,
    options: RunOptions = {},
  ): Promise<LoadOutcome<T>> {
    const background = options.background === true;
    if (disposed) return { kind: "stale" };
    if (background && inFlight !== null && !inFlight.background) return { kind: "skipped" };

    abandon();
    const controller = new AbortController();
    const mine = sequence;
    inFlight = { controller, background };

    if (!background) {
      emit({
        data: options.keepData === true ? state.data : null,
        loading: true,
        error: null,
        errorIsBackground: false,
      });
    }

    try {
      const data = await load(controller.signal);
      if (mine !== sequence) return { kind: "stale" };
      inFlight = null;
      emit({ data, loading: false, error: null, errorIsBackground: false });
      return { kind: "ok", data };
    } catch (err) {
      if (mine !== sequence) return { kind: "stale" };
      inFlight = null;
      if (isAbortError(err)) {
        // Newest run, yet aborted: something other than this loader fired the signal. It is still
        // not a failure to show; only the loading state has to settle.
        emit({ ...state, loading: false });
        return { kind: "stale" };
      }
      const message = config.errorMessage(err);
      emit({ data: state.data, loading: false, error: message, errorIsBackground: background });
      return { kind: "error", message };
    }
  }

  return {
    getState: () => state,
    run,
    abort() {
      abandon();
      if (state.loading) emit({ ...state, loading: false });
    },
    update(change) {
      emit({ ...state, data: change(state.data) });
    },
    clearError() {
      if (state.error !== null) emit({ ...state, error: null, errorIsBackground: false });
    },
    dispose() {
      abandon();
      disposed = true;
    },
    activate() {
      if (!disposed) return;
      disposed = false;
      config.onChange(state);
    },
  };
}

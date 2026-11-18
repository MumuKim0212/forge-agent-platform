/**
 * The React side of the shared loader: a thin wrapper. Every decision -- latest wins,
 * abort, what a cancelled load is, what a background run does -- is in `loader.ts` and covered
 * there without mounting anything (ADR 0021).
 */
import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

import { createLoader, type LoadOutcome, type LoadState, type Loader, type RunOptions } from "./loader";
import { createPoller } from "./poller";

export interface LoaderHandle<T> extends LoadState<T> {
  run: Loader<T>["run"];
  abort: Loader<T>["abort"];
  update: Loader<T>["update"];
  clearError: Loader<T>["clearError"];
}

export interface LoaderHookOptions {
  /** Start in `loading` -- right for a screen whose first load runs on mount. */
  initialLoading?: boolean;
}

/** A loader the screen drives by hand with `run`. It is aborted when the component unmounts.
 * `errorMessage` words a failure; the latest function passed is the one used. */
export function useLoader<T>(
  errorMessage: (err: unknown) => string,
  options: LoaderHookOptions = {},
): LoaderHandle<T> {
  const errorMessageRef = useRef(errorMessage);
  errorMessageRef.current = errorMessage;

  const [state, setState] = useState<LoadState<T>>({
    data: null,
    loading: options.initialLoading ?? false,
    error: null,
    errorIsBackground: false,
  });

  const loaderRef = useRef<Loader<T> | null>(null);
  if (loaderRef.current === null) {
    loaderRef.current = createLoader<T>({
      errorMessage: (err) => errorMessageRef.current(err),
      onChange: setState,
      initialLoading: options.initialLoading,
    });
  }
  const loader = loaderRef.current;

  // Declared before any effect that calls `run`, so on a (re)mount it is active first.
  useEffect(() => {
    loader.activate();
    return () => loader.dispose();
  }, [loader]);

  return { ...state, run: loader.run, abort: loader.abort, update: loader.update, clearError: loader.clearError };
}

/** A loader that also runs `load` on mount and whenever `deps` change (a new run supersedes the one
 * before). `reload` re-runs the **latest** `load`, so an action that finishes after the inputs have
 * moved on reloads for the inputs now on screen, not the ones it started with. */
export function useLoaderOnChange<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  errorMessage: (err: unknown) => string,
): LoaderHandle<T> & { reload: (options?: RunOptions) => Promise<LoadOutcome<T>> } {
  const handle = useLoader<T>(errorMessage, { initialLoading: true });
  const loadRef = useRef(load);
  loadRef.current = load;
  const { run } = handle;

  useEffect(() => {
    void run((signal) => loadRef.current(signal));
    // `deps` is the caller's list on purpose: a run starts when those inputs change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const reload = useCallback(
    (options?: RunOptions) => run((signal) => loadRef.current(signal), options),
    [run],
  );

  return { ...handle, reload };
}

/** While `enabled`, calls `poll` every `intervalMs`, the next one scheduled only after the previous
 * settles (`poller.ts`). Stops when `enabled` goes false or the component unmounts. */
export function usePolling(enabled: boolean, poll: () => Promise<unknown>, intervalMs: number): void {
  const pollRef = useRef(poll);
  pollRef.current = poll;

  useEffect(() => {
    if (!enabled) return undefined;
    const poller = createPoller(() => pollRef.current(), intervalMs);
    poller.start();
    return () => poller.stop();
  }, [enabled, intervalMs]);
}

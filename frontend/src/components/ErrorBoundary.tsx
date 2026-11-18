import { Component, useEffect, useRef, type ErrorInfo, type ReactNode } from "react";

import { Alert } from "./ui/Alert";
import { Button } from "./ui/Button";

/**
 * Where a render exception stops. Without a boundary, one component that throws while
 * rendering unmounts the whole React tree: the nav, the logout button and the identity provider go
 * with it and the user is left on a blank page with no way to sign out or move on.
 *
 * `Layout` wraps its page outlet in one, keyed by the path, so the nav and the logout button stay
 * up when a page crashes and a click on another menu entry gets a fresh page instead of the same
 * fallback. `main.tsx` wraps the whole app in a second as the last resort, for what the first
 * cannot hold (the layout itself, the sign-in pages). A boundary catches errors thrown while
 * rendering and in lifecycle methods; it does **not** catch an event handler or an async callback
 * (a failed request is the loader's job, not this one's).
 *
 * The fallback says what happened in Korean and offers the one action that always works, reloading
 * the page. It is an `alert` and takes focus: the content the user was reading has just been
 * replaced, and with it the element that held keyboard focus -- left alone, focus drops to the
 * start of the page and a screen reader announces nothing.
 */
interface ErrorBoundaryProps {
  children: ReactNode;
  /** What the reload button does. Defaults to a full page reload; a test passes its own. */
  onReload?: () => void;
}

interface ErrorBoundaryState {
  failed: boolean;
}

function reloadPage(): void {
  window.location.reload();
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // The console is the only place the cause goes: the screen gets a fixed sentence, because a
    // stack trace or a library's message is neither Korean nor something the user can act on.
    console.error("A page failed to render.", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.failed) {
      return <ErrorFallback onReload={this.props.onReload ?? reloadPage} />;
    }
    return this.props.children;
  }
}

function ErrorFallback({ onReload }: { onReload: () => void }) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <div>
      <h2
        ref={headingRef}
        tabIndex={-1}
        className="mb-4 text-lg font-bold text-slate-900 outline-none dark:text-slate-100"
      >
        화면을 표시하지 못했습니다
      </h2>
      <Alert variant="error" live>
        이 화면을 그리는 중에 예기치 않은 오류가 발생했습니다. 페이지를 새로 고쳐 다시 시도해 주세요. 같은 문제가
        계속되면 관리자에게 알려 주세요.
      </Alert>
      <Button className="mt-3" onClick={onReload}>
        페이지 새로 고침
      </Button>
    </div>
  );
}

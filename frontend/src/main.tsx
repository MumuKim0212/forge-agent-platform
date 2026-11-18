import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { IdentityProvider } from "./context/IdentityContext";
// the one stylesheet, imported exactly once.
// Every screen gets it via this single import — nothing per-route.
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* the last resort. `Layout` holds a boundary of its own for the pages, so this one
        catches only what that cannot: the layout itself and the sign-in pages. */}
    <ErrorBoundary>
      <BrowserRouter>
        <IdentityProvider>
          <App />
        </IdentityProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);

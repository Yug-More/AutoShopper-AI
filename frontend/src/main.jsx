// ✅ Sentry integration for React + Vite frontend
import * as Sentry from "@sentry/react";
import { BrowserTracing } from "@sentry/react";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.jsx";

// Initialize Sentry as early as possible
Sentry.init({
  dsn: "https://f9c642c0fbed9851a68015038374a11d@o4510370374615040.ingest.us.sentry.io/4510370599862272",
  integrations: [new BrowserTracing()],
  tracesSampleRate: 1.0, // capture 100% of performance traces
  // You can add a release tag if you deploy builds:
  // release: "frontend@1.0.0",
});

createRoot(document.getElementById("root")).render(
  <StrictMode>
    {/* Optional: wrap the app with Sentry's error boundary */}
    <Sentry.ErrorBoundary fallback={<p>Something went wrong 😔</p>}>
      <App />
    </Sentry.ErrorBoundary>
  </StrictMode>
);

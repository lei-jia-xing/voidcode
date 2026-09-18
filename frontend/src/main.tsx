import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App.tsx";
import { queryClient } from "./lib/queries";
import "./index.css";
import "./i18n";

// The shell reads every server payload from this client's cache, and the store
// reads the same entries through the same instance, so the app is given the
// process-wide client rather than a second one.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);

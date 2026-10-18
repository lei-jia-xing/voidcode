import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App.tsx";
import { KernelApp } from "./kernel/KernelApp";
import { queryClient } from "./lib/queries";
import "./index.css";
import "./i18n";

// Coexistence until P4 cutover: `?kernel` or `/kernel` activates the new
// generic kernel client, while default routes remain on the legacy App.
const isKernelMode =
  typeof window !== "undefined" &&
  (window.location.search.includes("kernel") ||
    window.location.pathname.startsWith("/kernel"));

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {isKernelMode ? (
      <KernelApp />
    ) : (
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    )}
  </React.StrictMode>,
);

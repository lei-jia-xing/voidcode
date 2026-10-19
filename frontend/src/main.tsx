import React from "react";
import ReactDOM from "react-dom/client";
import { KernelApp } from "./kernel/KernelApp";
import "./index.css";
import "./i18n";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <KernelApp />
  </React.StrictMode>,
);

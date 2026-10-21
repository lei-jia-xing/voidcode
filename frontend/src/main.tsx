import React from "react";
import ReactDOM from "react-dom/client";
import { CoreApp } from "./core/CoreApp";
import "./index.css";
import "./i18n";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <CoreApp />
  </React.StrictMode>,
);

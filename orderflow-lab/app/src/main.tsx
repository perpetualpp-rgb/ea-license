import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);

// Offline shell cache for an installed PWA on its own origin. Inside an embedded preview
// (an iframe or a sandboxed host) registration is skipped or simply fails.
if ("serviceWorker" in navigator && window.top === window.self && location.protocol === "https:" && !navigator.webdriver) {
  navigator.serviceWorker.register("./sw.js").catch(() => undefined);
}

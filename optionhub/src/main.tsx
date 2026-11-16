import { createRoot } from "react-dom/client";
import { toast } from "sonner";
import App from "./App.tsx";
import { registerServiceWorker } from "@/lib/marketData/serviceWorker";
import "./index.css";

createRoot(document.getElementById("root")!).render(<App />);

// options-checklist: the dashboard's build has no offline worker (vite.config.ts selfDestroying). Remove any
// worker an earlier build installed, so a deploy shows on the next reload instead of a cached old shell.
const DASHBOARD_BUILD = import.meta.env.VITE_DASHBOARD_KITE === "1";
if (DASHBOARD_BUILD && typeof navigator !== "undefined" && "serviceWorker" in navigator) {
  void navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => void r.unregister())).catch(() => undefined);
  if (typeof caches !== "undefined") void caches.keys().then((ks) => ks.forEach((k) => void caches.delete(k))).catch(() => undefined);
}

// PWA: production builds with vite-plugin-pwa installed only (vite.config.ts sets the flag).
if (!DASHBOARD_BUILD && import.meta.env.PROD && import.meta.env.VITE_PWA_ENABLED === "true") {
  window.addEventListener("load", () => {
    void registerServiceWorker({
      onUpdateReady: (apply) => {
        toast("Update available", {
          id: "pwa-update",
          description: "A new version of the terminal is ready. Reload to use it.",
          duration: Infinity,
          action: { label: "Reload", onClick: apply },
        });
      },
      onOfflineReady: () => {
        toast("Ready to work offline", {
          id: "pwa-offline-ready",
          description: "The app is cached on this device. Live market data still needs the proxy.",
        });
      },
    });
  });
}

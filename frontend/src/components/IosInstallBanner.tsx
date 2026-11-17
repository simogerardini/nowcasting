"use client";

import { useEffect, useState } from "react";
import { isIos, isStandalone } from "@/lib/pushManager";

const DISMISS_KEY = "nowcast.iosBannerDismissed";

/** iOS/iPadOS only allow Web Push for Home-Screen-installed PWAs: guide the user there. */
export default function IosInstallBanner() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    const dismissed = Number(localStorage.getItem(DISMISS_KEY) || 0);
    const recentlyDismissed = Date.now() - dismissed < 3 * 86400_000;
    setShow(isIos() && !isStandalone() && !recentlyDismissed);
  }, []);

  if (!show) return null;
  return (
    <div className="absolute inset-x-2 top-2 z-30 flex items-start gap-3 rounded-2xl bg-sky-700/95 p-3 text-sm shadow-2xl sm:left-auto sm:right-3 sm:w-96">
      <span className="text-2xl" aria-hidden>📲</span>
      <p className="flex-1 leading-snug">
        Tocca <b>Condividi</b>{" "}
        <svg className="inline h-4 w-4 align-text-bottom" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-label="icona condividi">
          <path d="M12 3v12M8 7l4-4 4 4M5 12v7a2 2 0 002 2h10a2 2 0 002-2v-7" />
        </svg>{" "}
        → <b>Aggiungi a schermata Home</b> per attivare gli avvisi meteo push.
      </p>
      <button
        onClick={() => {
          localStorage.setItem(DISMISS_KEY, String(Date.now()));
          setShow(false);
        }}
        aria-label="Chiudi"
        className="text-sky-100"
      >
        ✕
      </button>
    </div>
  );
}

"use client";

/**
 * /analyze -> /screener compat redirect.
 *
 * The analyze view has been folded into /screener (pick the "Custom" tab,
 * type any NSE/BSE symbol). Live chart + 3-horizon signals + Pin + chart
 * timeframe tabs all live there now. This route is kept only so external
 * bookmarks land somewhere useful.
 */

import { useEffect } from "react";

export default function AnalyzeRedirect() {
  useEffect(() => {
    window.location.replace("/screener");
  }, []);
  return (
    <div className="fade-in" style={{ padding: 40, textAlign: "center" }}>
      <div style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
        /analyze moved into <a href="/screener" style={{ color: "var(--accent-cyan)" }}>/screener → Custom</a>.
        Redirecting…
      </div>
    </div>
  );
}

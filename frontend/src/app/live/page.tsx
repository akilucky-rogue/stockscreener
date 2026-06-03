"use client";

/**
 * /live -> /screener compat redirect.
 *
 * The live single-symbol chart UI moved into /screener (pick the "Custom" tab,
 * type any NSE symbol, get the live chart that polls every 1s). This route is
 * kept only so external bookmarks land somewhere useful.
 */

import { useEffect } from "react";

export default function LiveRedirect() {
  useEffect(() => {
    // Client-side redirect — Next 16 server redirect() semantics shifted, so
    // we stay portable by punting on the browser.
    window.location.replace("/screener");
  }, []);

  return (
    <div className="fade-in" style={{ padding: 40, textAlign: "center" }}>
      <div style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
        /live moved into <a href="/screener" style={{ color: "var(--accent-cyan)" }}>/screener → Custom</a>.
        Redirecting…
      </div>
    </div>
  );
}

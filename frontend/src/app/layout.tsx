import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "QSDE — Quantitative Stock Decision Engine",
  description: "Multi-factor ML signal generation for Indian equities (Nifty 200)",
};

const navItems = [
  { href: "/",          label: "Dashboard", icon: "◉" },
  // /analyze + /live both merged into /screener — pick the "Custom" tab
  // there for any single-symbol deep dive (live chart + 3-horizon signals
  // with precise entry/exit timing + Pin to track). The old routes are
  // kept as redirects for bookmarks.
  { href: "/screener",  label: "Screener",  icon: "⊞" },
  { href: "/research",  label: "Research",  icon: "◆" },
  { href: "/signals",   label: "Signals",   icon: "⚡" },
  { href: "/watchlist", label: "Watchlist", icon: "★" },
  { href: "/factors",   label: "Factors",   icon: "◈" },
  { href: "/backtest",  label: "Backtest",  icon: "▶" },
  // /paper renders the live-validation journal: open paper trades + the
  // realized model-vs-baselines scorecard from /api/paper/{track-record,drift}.
  // This is where you go to see whether the system has earned the right to
  // step up the risk cap.
  { href: "/paper",     label: "Paper",     icon: "◐" },
];

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="app-layout">
          <aside className="sidebar">
            <div className="sidebar-logo">
              QSDE
              <span>Quantitative Stock Decision Engine</span>
            </div>
            <nav className="sidebar-nav">
              {navItems.map((item) => (
                <a key={item.href} href={item.href} className="nav-link">
                  <span className="icon">{item.icon}</span>
                  {item.label}
                </a>
              ))}
            </nav>
            <div style={{ marginTop: "auto", paddingTop: "16px", borderTop: "1px solid var(--border-color)" }}>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
                Phase 0 — Layer 0 MVP
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.65rem", color: "var(--text-muted)", marginTop: "4px" }}>
                Nifty 200 · Swing Horizon
              </div>
            </div>
          </aside>
          <main className="main-content">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}

"use client";

import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8000/api";

// Paper-trade page — where you check whether the system has earned the right
// to step up the risk cap. Three reads:
//   * /api/paper/track-record   -> realized hit rate / Sharpe per (horizon,strategy)
//   * /api/paper/drift          -> drift verdict + per-horizon model-vs-baselines
//   * /api/paper/trades         -> raw list of trades, filterable by status/strategy
//   * /api/risk/cap             -> current tier + cap fraction per horizon

type Trade = {
  id: number;
  symbol: string;
  horizon: string;
  strategy: string;
  entry_date: string;
  entry_price: number;
  direction: number;
  target_price: number | null;
  stop_price: number | null;
  status: string;
  exit_date: string | null;
  exit_price: number | null;
  realized_ret_net: number | null;
  taken_at: string;
};

export default function PaperPage() {
  const [horizon, setHorizon] = useState<"intraday" | "swing" | "long">("intraday");
  const [track, setTrack]   = useState<any>(null);
  const [drift, setDrift]   = useState<any>(null);
  const [riskCap, setRiskCap] = useState<any>(null);
  const [openTrades,   setOpenTrades]   = useState<Trade[]>([]);
  const [closedTrades, setClosedTrades] = useState<Trade[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = () => {
    fetch(`${API_BASE}/paper/track-record`).then(r => r.json()).then(setTrack);
    fetch(`${API_BASE}/paper/drift`).then(r => r.json()).then(setDrift);
    fetch(`${API_BASE}/risk/cap`).then(r => r.json()).then(setRiskCap);
    fetch(`${API_BASE}/paper/trades?status=OPEN&limit=300`)
      .then(r => r.json()).then(d => setOpenTrades(d.trades || []));
    fetch(`${API_BASE}/paper/trades?limit=300`)
      .then(r => r.json()).then(d => setClosedTrades(
        (d.trades || []).filter((t: Trade) => t.status !== "OPEN").slice(0, 50)
      ));
  };

  useEffect(() => { refresh(); }, []);

  const onReconcile = async () => {
    setBusy(true); setNotice(null);
    try {
      const r = await fetch(`${API_BASE}/paper/reconcile`, { method: "POST" });
      const d = await r.json();
      setNotice(`reconcile: closed=${d.closed} (win=${d.win} loss=${d.loss} time=${d.time}) still_open=${d.still_open}`);
      refresh();
    } catch (e) {
      setNotice("reconcile failed");
    } finally {
      setBusy(false);
    }
  };

  const onAutoTake = async () => {
    setBusy(true); setNotice(null);
    try {
      const r = await fetch(`${API_BASE}/paper/auto-take`, { method: "POST" });
      const d = await r.json();
      setNotice(`auto-take: ${d.total_taken} new trades across 3 horizons`);
      refresh();
    } catch (e) {
      setNotice("auto-take failed");
    } finally {
      setBusy(false);
    }
  };

  const horizons = ["intraday", "swing", "long"] as const;
  const strategies = ["model", "baseline_top_momentum", "baseline_nifty", "baseline_random"] as const;
  const stratLabel: Record<string, string> = {
    model: "MODEL", baseline_top_momentum: "TOP MOM",
    baseline_nifty: "NIFTY", baseline_random: "RANDOM",
  };

  const dActionColor = (a?: string) =>
    a === "stop"   ? "var(--accent-red)"   :
    a === "keep"   ? "var(--accent-green)" :
    a === "shrink" ? "var(--accent-amber)" :
                     "var(--accent-cyan)";

  // Filter trades to selected horizon
  const fOpen   = openTrades.filter(t => t.horizon === horizon);
  const fClosed = closedTrades.filter(t => t.horizon === horizon);

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">◐ PAPER JOURNAL</h1>
          <p className="page-subtitle">
            Live validation loop · open trades, realized scorecard, model vs baselines
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            disabled={busy}
            onClick={onAutoTake}
            style={{
              cursor: busy ? "wait" : "pointer",
              padding: "6px 14px",
              background: "var(--accent-cyan-glow, rgba(0,229,255,0.08))",
              border: "1px solid var(--accent-cyan)",
              color: "var(--accent-cyan)",
              borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.78rem",
            }}
          >
            Auto-take top signals
          </button>
          <button
            disabled={busy}
            onClick={onReconcile}
            style={{
              cursor: busy ? "wait" : "pointer",
              padding: "6px 14px",
              background: "var(--accent-green-glow)",
              border: "1px solid var(--accent-green)",
              color: "var(--accent-green)",
              borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.78rem",
            }}
          >
            Reconcile open trades
          </button>
          {notice && (
            <span style={{
              marginLeft: 6, color: "var(--text-secondary)",
              fontFamily: "var(--font-mono)", fontSize: "0.72rem",
            }}>
              {notice}
            </span>
          )}
        </div>
      </div>

      {/* Risk + drift summary tiles */}
      <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="metric-card">
          <div className="metric-label">Risk Tier</div>
          <div className="metric-value" style={{
            color: riskCap?.current_tier === "T0" ? "var(--accent-amber)" : "var(--accent-green)",
          }}>
            {riskCap?.current_tier || "..."}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            cap {((riskCap?.horizons?.[horizon]?.cap_fraction ?? 0.01) * 100).toFixed(2)}% per trade
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Drift verdict</div>
          <div className="metric-value" style={{ color: dActionColor(drift?.action) }}>
            {(drift?.action || "...").toUpperCase()}
          </div>
          <div className="metric-change" style={{
            color: "var(--text-muted)", fontSize: "0.66rem",
            maxHeight: "30px", overflow: "hidden",
          }}>
            {drift?.summary || ""}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Open paper trades</div>
          <div className="metric-value" style={{ color: "var(--accent-cyan)" }}>
            {openTrades.length}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            {fOpen.length} on {horizon} · reconcile after each market close
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Paper sessions</div>
          <div className="metric-value">{riskCap?.paper_sessions ?? "—"}</div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            {riskCap?.next_tier
              ? `${riskCap.next_tier.sessions_remaining} more for ${riskCap.next_tier.name}`
              : "—"}
          </div>
        </div>
      </div>

      {/* Horizon picker */}
      <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="card-header">
          <span className="card-title">▸ Horizon</span>
          <div style={{ display: "flex", gap: 4 }}>
            {horizons.map(h => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                style={{
                  padding: "4px 10px",
                  background: horizon === h ? "var(--accent-cyan-glow, rgba(0,229,255,0.08))" : "var(--bg-surface)",
                  border: `1px solid ${horizon === h ? "var(--accent-cyan)" : "var(--border-color)"}`,
                  color: horizon === h ? "var(--accent-cyan)" : "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.72rem",
                  cursor: "pointer",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                {h.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        {/* Model vs Baselines table */}
        <div style={{ overflow: "auto" }}>
          <table style={{
            width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.78rem",
            borderCollapse: "collapse",
          }}>
            <thead>
              <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                <th style={{ padding: "8px 12px" }}>Strategy</th>
                <th style={{ padding: "8px 12px" }}>n</th>
                <th style={{ padding: "8px 12px" }}>Wins</th>
                <th style={{ padding: "8px 12px" }}>Losses</th>
                <th style={{ padding: "8px 12px" }}>Time-exits</th>
                <th style={{ padding: "8px 12px" }}>Win rate</th>
                <th style={{ padding: "8px 12px" }}>Avg net</th>
                <th style={{ padding: "8px 12px" }}>Realized Sharpe</th>
                <th style={{ padding: "8px 12px" }}>vs backtest</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map(strat => {
                const s = track?.[horizon]?.[strat] || {};
                const isModel = strat === "model";
                return (
                  <tr key={strat} style={{
                    borderTop: "1px solid var(--border-color)",
                    background: isModel ? "rgba(0,229,255,0.03)" : "transparent",
                  }}>
                    <td style={{
                      padding: "8px 12px", color: isModel ? "var(--accent-cyan)" : "var(--text-primary)",
                    }}>
                      <strong>{stratLabel[strat]}</strong>
                    </td>
                    <td style={{ padding: "8px 12px" }}>{s.n ?? 0}</td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-green)" }}>{s.wins ?? "—"}</td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-red)" }}>{s.losses ?? "—"}</td>
                    <td style={{ padding: "8px 12px", color: "var(--text-muted)" }}>{s.time_exits ?? "—"}</td>
                    <td style={{ padding: "8px 12px" }}>
                      {s.win_rate != null ? `${(s.win_rate * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td style={{
                      padding: "8px 12px",
                      color: (s.avg_net_ret_bps ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)",
                    }}>
                      {s.avg_net_ret_bps != null
                        ? `${s.avg_net_ret_bps >= 0 ? "+" : ""}${s.avg_net_ret_bps}bps`
                        : "—"}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-cyan)" }}>
                      {s.realized_net_sharpe ?? "—"}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--text-muted)" }}>
                      {s.backtested_edge_band || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Open trades on selected horizon */}
      <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="card-header">
          <span className="card-title">▸ Open paper trades ({horizon.toUpperCase()})</span>
          <span style={{ color: "var(--text-muted)", fontSize: "0.72rem" }}>
            {fOpen.length} trade(s) waiting for barrier resolution
          </span>
        </div>

        {fOpen.length === 0 ? (
          <div style={{ padding: "30px", textAlign: "center", color: "var(--text-muted)" }}>
            No open paper trades on {horizon}. Click "Auto-take top signals" to seed today.
          </div>
        ) : (
          <div style={{ overflow: "auto" }}>
            <table style={{
              width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.74rem",
              borderCollapse: "collapse",
            }}>
              <thead>
                <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                  <th style={{ padding: "8px 12px" }}>Symbol</th>
                  <th style={{ padding: "8px 12px" }}>Strategy</th>
                  <th style={{ padding: "8px 12px" }}>Entry date</th>
                  <th style={{ padding: "8px 12px" }}>Entry</th>
                  <th style={{ padding: "8px 12px" }}>Target</th>
                  <th style={{ padding: "8px 12px" }}>Stop</th>
                  <th style={{ padding: "8px 12px" }}>Dir</th>
                </tr>
              </thead>
              <tbody>
                {fOpen.map(t => (
                  <tr key={t.id} style={{ borderTop: "1px solid var(--border-color)" }}>
                    <td style={{ padding: "8px 12px" }}>
                      <a href={`/research/${t.symbol}`}
                         style={{ color: "var(--text-primary)", textDecoration: "none", fontWeight: 600 }}>
                        {t.symbol}
                      </a>
                    </td>
                    <td style={{
                      padding: "8px 12px",
                      color: t.strategy === "model" ? "var(--accent-cyan)" : "var(--text-muted)",
                    }}>
                      {stratLabel[t.strategy] || t.strategy}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>{t.entry_date}</td>
                    <td style={{ padding: "8px 12px" }}>₹{t.entry_price.toFixed(2)}</td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-green)" }}>
                      {t.target_price != null ? `₹${t.target_price.toFixed(2)}` : "—"}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-red)" }}>
                      {t.stop_price != null ? `₹${t.stop_price.toFixed(2)}` : "—"}
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      {t.direction > 0 ? "🟢 LONG" : "🔴 SHORT"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recent closures on selected horizon */}
      <div className="card">
        <div className="card-header">
          <span className="card-title">▸ Recent closures ({horizon.toUpperCase()})</span>
          <span style={{ color: "var(--text-muted)", fontSize: "0.72rem" }}>
            {fClosed.length} most recent closed trade(s)
          </span>
        </div>

        {fClosed.length === 0 ? (
          <div style={{ padding: "30px", textAlign: "center", color: "var(--text-muted)" }}>
            No closed trades on {horizon} yet. Reconcile runs after each market close.
          </div>
        ) : (
          <div style={{ overflow: "auto" }}>
            <table style={{
              width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.74rem",
              borderCollapse: "collapse",
            }}>
              <thead>
                <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                  <th style={{ padding: "8px 12px" }}>Symbol</th>
                  <th style={{ padding: "8px 12px" }}>Strategy</th>
                  <th style={{ padding: "8px 12px" }}>Entry → Exit</th>
                  <th style={{ padding: "8px 12px" }}>Entry</th>
                  <th style={{ padding: "8px 12px" }}>Exit</th>
                  <th style={{ padding: "8px 12px" }}>Status</th>
                  <th style={{ padding: "8px 12px" }}>Net return</th>
                </tr>
              </thead>
              <tbody>
                {fClosed.map(t => (
                  <tr key={t.id} style={{ borderTop: "1px solid var(--border-color)" }}>
                    <td style={{ padding: "8px 12px" }}>
                      <a href={`/research/${t.symbol}`}
                         style={{ color: "var(--text-primary)", textDecoration: "none", fontWeight: 600 }}>
                        {t.symbol}
                      </a>
                    </td>
                    <td style={{
                      padding: "8px 12px",
                      color: t.strategy === "model" ? "var(--accent-cyan)" : "var(--text-muted)",
                    }}>
                      {stratLabel[t.strategy] || t.strategy}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>
                      {t.entry_date} → {t.exit_date || "—"}
                    </td>
                    <td style={{ padding: "8px 12px" }}>₹{t.entry_price.toFixed(2)}</td>
                    <td style={{ padding: "8px 12px" }}>
                      {t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}
                    </td>
                    <td style={{
                      padding: "8px 12px",
                      color: t.status === "WIN"  ? "var(--accent-green)"
                          :  t.status === "LOSS" ? "var(--accent-red)"
                          :                        "var(--text-muted)",
                    }}>
                      {t.status}
                    </td>
                    <td style={{
                      padding: "8px 12px",
                      color: (t.realized_ret_net ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)",
                    }}>
                      {t.realized_ret_net != null
                        ? `${t.realized_ret_net >= 0 ? "+" : ""}${(t.realized_ret_net * 100).toFixed(2)}%`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

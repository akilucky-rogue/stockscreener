"use client";

import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8000/api";

interface HealthStatus {
  status: string;
  checks: Record<string, string>;
}

export default function Dashboard() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  // `time` is null on first render so SSR and client agree. Once mounted,
  // we fill it from the client clock and tick every second. This avoids
  // the Next.js hydration mismatch ("server text differs from client") that
  // any locale-dependent `new Date()` rendered on the server would cause.
  const [time, setTime] = useState<Date | null>(null);
  const [horizon, setHorizon] = useState("swing");
  const [signals, setSignals] = useState<any[]>([]);
  const [edge, setEdge] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [track, setTrack] = useState<any>(null);
  const [paperMsg, setPaperMsg] = useState<Record<string, string>>({});
  // Phase-1 live-validation widgets. These read REALIZED stats so the user
  // never has to dig into endpoints to know whether to trust today's signals.
  //   riskCap: current tier, per-horizon cap %, next-tier readiness.
  //   drift:   model vs baselines summary + drift flag per horizon.
  const [riskCap, setRiskCap] = useState<any>(null);
  const [drift,   setDrift]   = useState<any>(null);
  // Risk action UI state: escalation/de-escalation pending + last result.
  const [riskBusy,    setRiskBusy]    = useState(false);
  const [riskNotice,  setRiskNotice]  = useState<string | null>(null);

  useEffect(() => {
    setTime(new Date());
    const interval = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then(r => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "offline", checks: { api: "error" } }));
  }, []);

  useEffect(() => {
    setLoading(true);
    // Live tradeable view: liquid names only, ranked by model score
    // (sort_by_prediction). This reproduces the validated top-K-within-liquid
    // selection — the only universe where the backtested edge survives costs.
    fetch(`${API_BASE}/signals?horizon=${horizon}&limit=500&liquid_only=true&sort_by_prediction=true`)
      .then(r => r.json())
      .then(d => { setSignals(d.signals || []); setEdge(d.edge || null); setLoading(false); })
      .catch(() => setLoading(false));
    // Live paper-trade track record for this horizon.
    fetch(`${API_BASE}/paper/track-record?horizon=${horizon}`)
      .then(r => r.json())
      .then(setTrack)
      .catch(() => setTrack(null));
    // Risk cap + drift report. Both refresh whenever horizon changes since
    // the cap and drift state are horizon-aware.
    fetch(`${API_BASE}/risk/cap`)
      .then(r => r.json())
      .then(setRiskCap)
      .catch(() => setRiskCap(null));
    fetch(`${API_BASE}/paper/drift`)
      .then(r => r.json())
      .then(setDrift)
      .catch(() => setDrift(null));
  }, [horizon]);

  // Record a paper trade (long-only) for the live track record.
  const takePaper = (symbol: string) => {
    setPaperMsg(p => ({ ...p, [symbol]: "..." }));
    fetch(`${API_BASE}/paper/take?symbol=${encodeURIComponent(symbol)}&horizon=${horizon}`, { method: "POST" })
      .then(r => r.json())
      .then(d => setPaperMsg(p => ({ ...p, [symbol]: d.ok ? "✓ taken" : (d.error || "failed") })))
      .catch(() => setPaperMsg(p => ({ ...p, [symbol]: "failed" })));
  };

  // NSE cash-equity hours: 09:15 - 15:30 IST (Mon-Fri, excluding NSE holidays).
  // `time` is null pre-mount -> render closed to keep SSR/CSR markup identical
  // until the clock starts ticking. We DO check minutes — being "open" at 09:14
  // or "open" at 15:31 is wrong by exactly 1 minute and would show stale data
  // as if it were live.
  const marketOpen = (() => {
    if (time == null) return false;
    const dow = time.getDay();              // 0=Sun, 6=Sat
    if (dow === 0 || dow === 6) return false;
    const h = time.getHours(), m = time.getMinutes();
    const minsSinceMidnight = h * 60 + m;
    return minsSinceMidnight >= (9 * 60 + 15) && minsSinceMidnight < (15 * 60 + 30);
  })();

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">◉ MARKET OVERVIEW</h1>
          <p className="page-subtitle">
            {time
              ? time.toLocaleDateString("en-IN", { weekday: "long", year: "numeric", month: "long", day: "numeric" })
              : "Loading..."}
            {" · "}
            <span
              style={{ fontFamily: "var(--font-mono)", color: "var(--accent-green)" }}
              suppressHydrationWarning
            >
              {time ? `${time.toLocaleTimeString("en-IN", { hour12: false })} IST` : "--:--:-- IST"}
            </span>
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <span className={`status-badge ${marketOpen ? "live" : "shadow"}`}>
            {marketOpen ? "● MARKET OPEN" : "○ MARKET CLOSED"}
          </span>
          <span className="regime-indicator calm-trend">
            <span className="regime-dot" style={{ background: "var(--accent-green)" }}></span>
            CALM TREND
          </span>
        </div>
      </div>

      {/* System Status */}
      <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="metric-card">
          <div className="metric-label">System Status</div>
          <div className={`metric-value ${health?.status === "healthy" ? "positive" : "negative"}`}>
            {health?.status?.toUpperCase() || "LOADING..."}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            DB: {health?.checks?.database || "..."} · Redis: {health?.checks?.redis || "..."}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Universe</div>
          <div className="metric-value">NIFTY 200</div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            Phase 0 — Layer 0 LightGBM MVP
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Active Signals ({horizon.toUpperCase()})</div>
          {(() => {
            // Count buys vs sells from the live signals payload. We DON'T
            // count HOLDs here -- "active" means actionable.
            const buys  = signals.filter((s: any) => s.direction > 0).length;
            const sells = signals.filter((s: any) => s.direction < 0).length;
            const total = buys + sells;
            return (
              <>
                <div className="metric-value" style={{
                  color: total > 0 ? "var(--accent-green)" : "var(--text-muted)",
                }}>
                  {total > 0 ? total : "—"}
                </div>
                <div className="metric-change" style={{
                  color: "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.7rem",
                }}>
                  {total > 0
                    ? `${buys} BUY · ${sells} SELL · ${signals.length - total} HOLD`
                    : "Loading signals..."}
                </div>
              </>
            );
          })()}
        </div>

        <div className="metric-card">
          <div className="metric-label">Horizon Toggle</div>
          <div style={{ display: "flex", gap: "4px", marginTop: "4px" }}>
            {[
              { key: "intraday", label: "INTRADAY (1d)", accent: "var(--accent-amber)", glow: "rgba(255,184,0,0.12)" },
              { key: "swing",    label: "SWING (5d)",    accent: "var(--accent-green)", glow: "var(--accent-green-glow)" },
              { key: "long",     label: "LONG (20d)",    accent: "var(--accent-cyan)",  glow: "rgba(0,229,255,0.12)" },
            ].map(h => (
              <button
                key={h.key}
                onClick={() => setHorizon(h.key)}
                style={{
                  flex: 1, padding: "4px", cursor: "pointer",
                  background: horizon === h.key ? h.glow : "var(--bg-surface)",
                  border: `1px solid ${horizon === h.key ? h.accent : "var(--border-color)"}`,
                  color: horizon === h.key ? h.accent : "var(--text-muted)",
                  borderRadius: "var(--radius-sm)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.7rem",
                }}
              >
                {h.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Live Validation panel — risk cap tier + drift + model vs baselines.
          This is the "should I trust the signals below?" widget. Rendered
          only after at least one of riskCap or drift loads to keep the
          markup clean on first paint. */}
      {(riskCap || drift) && (
        <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
          <div className="card-header">
            <span className="card-title">▸ Live Validation · risk cap + edge vs baselines</span>
            {riskCap?.current_tier && (
              <span
                title={riskCap?.philosophy}
                style={{
                  fontFamily: "var(--font-mono)", fontSize: "0.72rem",
                  color: riskCap.current_tier === "T0" ? "var(--accent-amber)" : "var(--accent-green)",
                }}
              >
                tier {riskCap.current_tier} · cap {(riskCap.horizons?.[horizon]?.cap_fraction * 100 || 1).toFixed(2)}% per trade
              </span>
            )}
          </div>

          {/* Top row: overall recommendation + drift flag */}
          {drift && (
            <div style={{
              margin: "0 0 12px", padding: "8px 12px",
              background: drift.action === "stop" ? "rgba(255,51,102,0.10)"
                        : drift.action === "keep" ? "rgba(0,255,136,0.06)"
                        : "var(--bg-surface)",
              borderRadius: "var(--radius-sm)", fontFamily: "var(--font-mono)",
              fontSize: "0.74rem", color: "var(--text-secondary)",
              borderLeft: `3px solid ${
                drift.action === "stop"  ? "var(--accent-red)"
              : drift.action === "keep"  ? "var(--accent-green)"
              : drift.action === "shrink"? "var(--accent-amber)"
              :                            "var(--accent-cyan)"}`,
            }}>
              <strong style={{
                color: drift.action === "stop"  ? "var(--accent-red)"
                     : drift.action === "keep"  ? "var(--accent-green)"
                     : drift.action === "shrink"? "var(--accent-amber)"
                     :                            "var(--accent-cyan)",
              }}>
                {drift.action?.toUpperCase()}
              </strong>
              <span> · {drift.summary}</span>
            </div>
          )}

          {/* Per-horizon split: model vs baselines + cap fraction */}
          {drift?.horizons?.[horizon] && (
            <div style={{
              padding: "8px 12px", background: "var(--bg-surface)",
              borderRadius: "var(--radius-sm)", fontFamily: "var(--font-mono)",
              fontSize: "0.72rem", color: "var(--text-secondary)",
            }}>
              <div style={{ marginBottom: 6, color: "var(--text-primary)" }}>
                {horizon.toUpperCase()} — model vs baselines (net of cost)
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 8 }}>
                {(["model", "baseline_top_momentum", "baseline_nifty", "baseline_random"] as const).map(s => {
                  const b = s === "model"
                    ? drift.horizons[horizon].vs_baselines?.model
                    : drift.horizons[horizon].vs_baselines?.baselines?.[s];
                  const label = s === "model" ? "MODEL"
                              : s === "baseline_top_momentum" ? "TOP MOM"
                              : s === "baseline_nifty" ? "NIFTY"
                              : "RANDOM";
                  const isModel = s === "model";
                  return (
                    <div key={s} style={{
                      padding: "6px 8px",
                      background: isModel ? "rgba(0,229,255,0.04)" : "transparent",
                      border: `1px solid ${isModel ? "rgba(0,229,255,0.30)" : "var(--border-color)"}`,
                      borderRadius: "var(--radius-sm)",
                    }}>
                      <div style={{ fontSize: "0.62rem", color: "var(--text-muted)" }}>{label}</div>
                      <div>n={b?.n ?? 0}</div>
                      <div>win {b?.hit_rate != null ? (b.hit_rate * 100).toFixed(0) + "%" : "—"}</div>
                      <div style={{
                        color: (b?.avg_net_ret_bps ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)",
                      }}>
                        {b?.avg_net_ret_bps != null
                          ? `${b.avg_net_ret_bps >= 0 ? "+" : ""}${b.avg_net_ret_bps}bps`
                          : "—"}
                      </div>
                      <div style={{ color: "var(--accent-cyan)" }}>
                        Sh {b?.net_sharpe != null ? b.net_sharpe : "—"}
                      </div>
                    </div>
                  );
                })}
              </div>
              {drift.horizons[horizon].vs_baselines?.issues && (
                <div style={{ marginTop: 8, color: "var(--accent-amber)", fontSize: "0.68rem" }}>
                  · {drift.horizons[horizon].vs_baselines.issues.join("  ·  ")}
                </div>
              )}
            </div>
          )}

          {/* Next-tier readiness + action buttons.
              Escalation requires explicit user click AND backend readiness
              gate (`next_tier.ready_to_escalate`). De-escalation is always
              one click — you can always reduce risk. */}
          {riskCap?.next_tier && (
            <div style={{
              marginTop: 8, padding: "6px 12px",
              background: "var(--bg-surface)", borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)", fontSize: "0.70rem",
              color: riskCap.next_tier.ready_to_escalate ? "var(--accent-green)" : "var(--text-muted)",
              display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8,
            }}>
              <span>
                Next: <strong>{riskCap.next_tier.name}</strong> (cap {(riskCap.next_tier.hard_cap*100).toFixed(1)}%)
                {" · "}sessions remaining: <strong>{riskCap.next_tier.sessions_remaining}</strong>
                {" · "}horizons confirmed: <strong>{riskCap.next_tier.horizons_confirmed.length}/3</strong>
                {riskCap.next_tier.ready_to_escalate && (
                  <span style={{ marginLeft: 8, color: "var(--accent-green)" }}>· ready to escalate</span>
                )}
              </span>

              <span style={{ flex: 1 }} />

              {/* Escalate button — gated on backend readiness. Confirms then POSTs. */}
              {riskCap.next_tier.ready_to_escalate && (
                <button
                  disabled={riskBusy}
                  onClick={async () => {
                    const target = riskCap.next_tier.name;
                    const cap_pct = (riskCap.next_tier.hard_cap * 100).toFixed(1);
                    if (!confirm(
                      `Escalate position risk cap to ${target} (${cap_pct}% per trade)?\n\n` +
                      `This will allow larger paper trades immediately. ` +
                      `On drift, the system will auto-de-escalate to T0.`
                    )) return;
                    setRiskBusy(true);
                    setRiskNotice(null);
                    try {
                      const reason = encodeURIComponent(
                        `dashboard escalation; ${riskCap.next_tier.horizons_confirmed.length}/3 horizons confirmed`
                      );
                      const r = await fetch(
                        `${API_BASE}/risk/escalate?to=${target}&reason=${reason}`,
                        { method: "POST" }
                      );
                      const d = await r.json();
                      setRiskNotice(d.ok
                        ? `✓ escalated ${d.from} → ${d.to}`
                        : `✗ ${d.error || "escalation refused"}`);
                      // refresh cap state
                      fetch(`${API_BASE}/risk/cap`).then(r => r.json()).then(setRiskCap);
                    } catch (e) {
                      setRiskNotice("✗ network error");
                    } finally {
                      setRiskBusy(false);
                    }
                  }}
                  style={{
                    cursor: riskBusy ? "wait" : "pointer",
                    padding: "3px 10px",
                    background: "var(--accent-green-glow)",
                    border: "1px solid var(--accent-green)",
                    color: "var(--accent-green)",
                    borderRadius: "var(--radius-sm)",
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.68rem",
                  }}
                >
                  Escalate → {riskCap.next_tier.name}
                </button>
              )}

              {/* De-escalate button — always available unless already at T0. */}
              {riskCap.current_tier !== "T0" && (
                <button
                  disabled={riskBusy}
                  onClick={async () => {
                    const cur = riskCap.current_tier as string;
                    const idx = parseInt(cur.slice(1), 10);
                    const target = `T${Math.max(0, idx - 1)}`;
                    if (!confirm(
                      `De-escalate position risk cap from ${cur} to ${target}?\n\n` +
                      `This always succeeds — you can always reduce risk.`
                    )) return;
                    setRiskBusy(true);
                    setRiskNotice(null);
                    try {
                      const reason = encodeURIComponent("dashboard manual de-escalation");
                      const r = await fetch(
                        `${API_BASE}/risk/deescalate?to=${target}&reason=${reason}`,
                        { method: "POST" }
                      );
                      const d = await r.json();
                      setRiskNotice(d.ok
                        ? `↓ de-escalated ${d.from} → ${d.to}`
                        : `✗ ${d.error || "de-escalation refused"}`);
                      fetch(`${API_BASE}/risk/cap`).then(r => r.json()).then(setRiskCap);
                    } catch (e) {
                      setRiskNotice("✗ network error");
                    } finally {
                      setRiskBusy(false);
                    }
                  }}
                  style={{
                    cursor: riskBusy ? "wait" : "pointer",
                    padding: "3px 10px",
                    background: "transparent",
                    border: "1px solid var(--border-color)",
                    color: "var(--text-muted)",
                    borderRadius: "var(--radius-sm)",
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.68rem",
                  }}
                >
                  De-escalate ↓
                </button>
              )}

              {riskNotice && (
                <span style={{
                  marginLeft: 8,
                  color: riskNotice.startsWith("✓") || riskNotice.startsWith("↓")
                    ? "var(--accent-green)"
                    : "var(--accent-red)",
                  fontSize: "0.68rem",
                }}>
                  {riskNotice}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Signals Grid */}
      <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="card-header">
          <span className="card-title">▸ Live Signals ({horizon.toUpperCase()}) · liquid, top-ranked</span>
          {edge ? (
            <span
              title={(edge.caveats || []).join("  •  ")}
              style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--accent-amber)" }}
            >
              validated edge ≈ {edge.net_sharpe?.toFixed?.(2) ?? edge.net_sharpe} net Sharpe
              {edge.edge_band ? ` (${edge.edge_band})` : ""}
            </span>
          ) : (
            <span className="status-badge live">AI ACTIVE</span>
          )}
        </div>

        {/* Live paper track record vs the backtested edge */}
        {track && track.n > 0 && (
          <div style={{
            margin: "0 0 12px", padding: "8px 12px", background: "var(--bg-surface)",
            borderRadius: "var(--radius-sm)", fontFamily: "var(--font-mono)", fontSize: "0.72rem",
            color: "var(--text-secondary)", display: "flex", gap: 18, flexWrap: "wrap",
          }}>
            <span>📓 Live paper ({horizon}): <strong>{track.n}</strong> trades</span>
            <span>win <strong style={{ color: "var(--accent-green)" }}>{(track.win_rate * 100).toFixed(0)}%</strong></span>
            <span>avg <strong style={{ color: track.avg_net_ret_bps >= 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
              {track.avg_net_ret_bps >= 0 ? "+" : ""}{track.avg_net_ret_bps}bps net</strong></span>
            <span>realized Sharpe <strong style={{ color: "var(--accent-cyan)" }}>
              {track.realized_net_sharpe != null ? track.realized_net_sharpe : "—"}</strong>
              {track.backtested_edge_band ? ` vs backtest ${track.backtested_edge_band}` : ""}</span>
            {track.note && <span style={{ color: "var(--accent-amber)" }}>· {track.note}</span>}
          </div>
        )}
        
        {loading ? (
          <div style={{ padding: "40px", textAlign: "center", color: "var(--text-muted)" }}>Loading signals...</div>
        ) : signals.length === 0 ? (
          <div style={{ padding: "40px", textAlign: "center", color: "var(--text-muted)" }}>No signals generated yet.</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: "var(--gap-md)" }}>
            {signals.slice(0, 12).map((s, i) => (
              <div key={s.symbol} style={{
                background: "var(--bg-surface)", 
                borderRadius: "var(--radius-md)", 
                padding: "var(--gap-md)",
                border: `1px solid ${s.direction > 0 ? 'rgba(0,255,136,0.3)' : s.direction < 0 ? 'rgba(255,51,102,0.3)' : 'var(--border-color)'}`,
                animationDelay: `${i * 20}ms`
              }} className="fade-in">
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "12px" }}>
                  <a href={`/research/${s.symbol}`} style={{ fontSize: "1.2rem", fontWeight: "bold", color: "var(--text-primary)", textDecoration: "none" }}>
                    {s.symbol}
                  </a>
                  <span style={{ 
                    padding: "2px 8px", borderRadius: "12px", fontSize: "0.8rem", fontWeight: "bold",
                    background: s.direction > 0 ? "rgba(0,255,136,0.1)" : s.direction < 0 ? "rgba(255,51,102,0.1)" : "var(--bg-card)",
                    color: s.direction > 0 ? "var(--accent-green)" : s.direction < 0 ? "var(--accent-red)" : "var(--text-muted)"
                  }}>
                    {s.direction > 0 ? "🟢 BUY" : s.direction < 0 ? "🔴 SELL" : "⚪ HOLD"}
                  </span>
                </div>
                
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85rem", color: "var(--text-secondary)", marginBottom: "12px" }}>
                  <span title="Cross-sectional model rank today. The model outputs a relative score (not a return); direction comes from where the name ranks vs the universe.">
                    Model rank: <strong style={{ color: "var(--accent-cyan)" }}>
                      {s.ranking_score == null ? "—"
                        : s.ranking_score >= 0.5
                          ? `Top ${Math.max(0.1, (1 - s.ranking_score) * 100).toFixed(s.ranking_score > 0.99 ? 1 : 0)}%`
                          : `Bottom ${Math.max(0.1, s.ranking_score * 100).toFixed(s.ranking_score < 0.01 ? 1 : 0)}%`}
                    </strong>
                  </span>
                  <span title="Trailing-20d average daily value traded. Liquidity gate = Rs 10cr.">
                    ADV: <strong>{s.adv_20d != null ? `₹${(s.adv_20d / 1e7).toFixed(0)}cr` : "—"}</strong>
                  </span>
                </div>

                {/* Compact trade plan -- only when signal has a direction. */}
                {s.direction !== 0 && s.entry_price != null && (
                  <div style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr 1fr auto",
                    gap: "8px",
                    padding: "8px",
                    background: "var(--bg-surface)",
                    borderRadius: "var(--radius-sm)",
                    marginBottom: "12px",
                    fontFamily: "var(--font-mono)",
                  }}>
                    <div>
                      <div style={{ fontSize: "0.6rem", color: "var(--text-muted)" }}>ENTRY</div>
                      <div style={{ fontSize: "0.85rem", color: "var(--text-primary)" }}>₹{Number(s.entry_price).toFixed(2)}</div>
                    </div>
                    <div>
                      <div style={{ fontSize: "0.6rem", color: "var(--text-muted)" }}>TARGET</div>
                      <div style={{ fontSize: "0.85rem", color: "var(--accent-green)" }}>
                        {s.target_price != null ? `₹${Number(s.target_price).toFixed(2)}` : "—"}
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: "0.6rem", color: "var(--text-muted)" }}>STOP</div>
                      <div style={{ fontSize: "0.85rem", color: "var(--accent-red)" }}>
                        {s.stop_price != null ? `₹${Number(s.stop_price).toFixed(2)}` : "—"}
                      </div>
                    </div>
                    <div style={{ alignSelf: "center", textAlign: "right" }}>
                      <div style={{ fontSize: "0.6rem", color: "var(--text-muted)" }}>R:R</div>
                      <div style={{
                        fontSize: "0.85rem",
                        color: s.trade_quality === "good" ? "var(--accent-green)" : "var(--accent-amber)",
                      }}>
                        {s.risk_reward != null ? Number(s.risk_reward).toFixed(2) : "—"}
                      </div>
                    </div>
                  </div>
                )}

                <div style={{ fontSize: "0.75rem", fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                  <div style={{ marginBottom: "4px", color: "var(--text-secondary)" }}>Top Factors:</div>
                  {s.top_factors?.slice(0, 3).map((f: any) => (
                    <div key={f.name} style={{ display: "flex", justifyContent: "space-between", marginBottom: "2px" }}>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "180px" }}>
                        {f.name}
                      </span>
                      <span style={{ color: f.contribution > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                        {f.contribution > 0 ? "+" : ""}{f.contribution.toFixed(4)}
                      </span>
                    </div>
                  ))}
                </div>

                {/* Take (paper) — record for the live track record */}
                {s.direction > 0 && (
                  <button
                    onClick={() => takePaper(s.symbol)}
                    disabled={!!paperMsg[s.symbol]}
                    style={{
                      marginTop: "10px", width: "100%", padding: "6px",
                      background: paperMsg[s.symbol] === "✓ taken" ? "rgba(0,255,136,0.12)" : "transparent",
                      border: "1px solid var(--accent-green)", borderRadius: "var(--radius-sm)",
                      color: "var(--accent-green)", fontFamily: "var(--font-mono)", fontSize: "0.72rem",
                      cursor: paperMsg[s.symbol] ? "default" : "pointer",
                    }}
                    title="Record this as a paper trade. The daily EOD reconciles it against real prices to build your live track record."
                  >
                    {paperMsg[s.symbol] ? paperMsg[s.symbol] : "📓 Take (paper)"}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

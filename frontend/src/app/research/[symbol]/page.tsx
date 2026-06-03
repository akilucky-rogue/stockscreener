"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import SymbolAnalysisPanel from "../../../components/SymbolAnalysisPanel";

const API = "http://127.0.0.1:8000/api";

function fmt(v: number | null | undefined, s = "", d = 1): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(d)}${s}`;
}

function fmtLarge(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(0);
}

// ── Comps Section ──────────────────────────────────────────
function CompsSection({ data }: { data: Record<string, unknown> | null }) {
  if (!data) return <SectionShimmer />;
  if (data.error) {
    return (
      <div className="card" style={{ marginBottom: "var(--gap-lg)", border: "1px solid var(--accent-red)", background: "rgba(255,56,96,0.05)" }}>
        <div className="card-header">
          <span className="card-title" style={{ color: "var(--accent-red)" }}>▸ Comparable Company Analysis — Error</span>
        </div>
        <div style={{ padding: "var(--gap-md)", color: "var(--accent-red)", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
          {data.error as string}
        </div>
      </div>
    );
  }
  const peers = (data.peers as Record<string, unknown>[]) || [];
  const valStats = (data.valuation_stats as Record<string, Record<string, number>>) || {};
  const positioning = (data.positioning as Record<string, Record<string, number>>) || {};

  return (
    <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
      <div className="card-header">
        <span className="card-title">▸ Comparable Company Analysis</span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
          {peers.length} peers · {data.sector as string}
        </span>
      </div>

      {/* Positioning badges */}
      {Object.keys(positioning).length > 0 && (
        <div style={{ display: "flex", gap: "var(--gap-sm)", marginBottom: "var(--gap-md)", flexWrap: "wrap" }}>
          {Object.entries(positioning).map(([metric, pos]) => (
            <div key={metric} style={{
              padding: "6px 12px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.7rem",
            }}>
              <span style={{ color: "var(--text-muted)" }}>{metric.replace(/_/g, " ").toUpperCase()}: </span>
              <span style={{ color: "var(--accent-cyan)", fontWeight: 600 }}>{pos.value}</span>
              <span style={{ color: pos.vs_median > 0 ? "var(--accent-red)" : "var(--accent-green)", marginLeft: 6 }}>
                {pos.vs_median > 0 ? "+" : ""}{pos.vs_median}% vs median
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Peer table */}
      <div style={{ overflow: "auto", maxHeight: 400 }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Company</th>
              <th>Mkt Cap</th>
              <th>P/E</th>
              <th>P/B</th>
              <th>EV/Rev</th>
              <th>EV/EBITDA</th>
              <th>Gross%</th>
              <th>ROE%</th>
              <th>Rev Gr%</th>
            </tr>
          </thead>
          <tbody>
            {peers.slice(0, 15).map((p) => {
              const isTarget = p.symbol === data.symbol;
              return (
                <tr key={p.symbol as string} style={{
                  background: isTarget ? "rgba(0,229,255,0.05)" : undefined,
                  borderLeft: isTarget ? "2px solid var(--accent-cyan)" : undefined,
                }}>
                  <td style={{ fontWeight: isTarget ? 700 : 500, color: isTarget ? "var(--accent-cyan)" : "var(--text-primary)" }}>
                    {p.symbol as string}
                  </td>
                  <td style={{ maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {p.company_name as string || "—"}
                  </td>
                  <td>{fmtLarge(p.market_cap as number)}</td>
                  <td>{fmt(p.pe_ratio as number, "x")}</td>
                  <td>{fmt(p.pb_ratio as number, "x")}</td>
                  <td>{fmt(p.ev_to_revenue as number, "x")}</td>
                  <td>{fmt(p.ev_to_ebitda as number, "x")}</td>
                  <td>{fmt(p.gross_margin as number, "%")}</td>
                  <td>{fmt(p.roe as number, "%")}</td>
                  <td>{fmt(p.revenue_growth as number, "%")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Quartile stats */}
      {Object.keys(valStats).length > 0 && (
        <div style={{ marginTop: "var(--gap-md)", display: "flex", gap: "var(--gap-sm)", flexWrap: "wrap" }}>
          {Object.entries(valStats).map(([metric, stats]) => (
            <div key={metric} style={{
              padding: "8px 14px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.7rem",
              minWidth: 120,
            }}>
              <div style={{ color: "var(--text-muted)", marginBottom: 4 }}>{metric.replace(/_/g, " ").toUpperCase()}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <span>P25: <b style={{ color: "var(--accent-green)" }}>{stats.p25}</b></span>
                <span>Med: <b>{stats.median}</b></span>
                <span>P75: <b style={{ color: "var(--accent-amber)" }}>{stats.p75}</b></span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── DCF Section ────────────────────────────────────────────
function DCFSection({ data }: { data: Record<string, unknown> | null }) {
  if (!data) return <SectionShimmer />;
  if (data.error) {
    return (
      <div className="card" style={{ marginBottom: "var(--gap-lg)", border: "1px solid var(--accent-red)", background: "rgba(255,56,96,0.05)" }}>
        <div className="card-header">
          <span className="card-title" style={{ color: "var(--accent-red)" }}>▸ DCF Valuation Model — Error</span>
        </div>
        <div style={{ padding: "var(--gap-md)", color: "var(--accent-red)", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
          {data.error as string}
        </div>
      </div>
    );
  }
  const bridge = data.equity_bridge as Record<string, number> | undefined;
  const wacc = data.wacc as Record<string, number> | undefined;
  const projections = (data.projections as Record<string, number>[]) || [];
  const scenarios = (data.scenarios as Record<string, Record<string, number>>) || {};
  const sensitivity = data.sensitivity as Record<string, unknown> | undefined;
  const dcfSummary = data.dcf_summary as Record<string, number> | undefined;

  return (
    <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
      <div className="card-header">
        <span className="card-title">▸ DCF Valuation Model</span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
          5-year · Perpetuity growth · Mid-year convention
        </span>
      </div>

      {/* Valuation Summary Cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: "var(--gap-sm)", marginBottom: "var(--gap-md)" }}>
        {bridge && (
          <>
            <div className="metric-card">
              <div className="metric-label">Implied Price</div>
              <div className="metric-value positive">₹{bridge.implied_price?.toLocaleString()}</div>
              <div className="metric-change" style={{ color: bridge.upside_pct > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                {bridge.upside_pct > 0 ? "▲" : "▼"} {bridge.upside_pct}% vs current
              </div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Current Price</div>
              <div className="metric-value">₹{bridge.current_price?.toLocaleString()}</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Enterprise Value</div>
              <div className="metric-value">{fmtLarge(bridge.enterprise_value)}</div>
            </div>
          </>
        )}
        {wacc && (
          <div className="metric-card">
            <div className="metric-label">WACC</div>
            <div className="metric-value neutral">{(wacc.wacc * 100).toFixed(1)}%</div>
            <div className="metric-change" style={{ color: "var(--text-muted)" }}>
              β={wacc.beta} · Rf={fmt(wacc.risk_free_rate * 100, "%")}
            </div>
          </div>
        )}
      </div>

      {/* Scenario Summary */}
      {Object.keys(scenarios).length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--gap-sm)", marginBottom: "var(--gap-md)" }}>
          {(["bear", "base", "bull"] as const).map((sc) => {
            const s = scenarios[sc];
            if (!s) return null;
            const colors = { bear: "var(--accent-red)", base: "var(--accent-amber)", bull: "var(--accent-green)" };
            return (
              <div key={sc} style={{
                padding: "var(--gap-md)",
                background: "var(--bg-surface)",
                borderRadius: "var(--radius-md)",
                border: `1px solid ${sc === "base" ? "rgba(255,184,0,0.3)" : "var(--border-color)"}`,
                textAlign: "center",
              }}>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: colors[sc], textTransform: "uppercase", marginBottom: 4 }}>
                  {sc} Case
                </div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "1.3rem", fontWeight: 700, color: "var(--text-bright)" }}>
                  ₹{s.implied_price?.toLocaleString()}
                </div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem", color: s.upside_pct > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                  {s.upside_pct > 0 ? "+" : ""}{s.upside_pct}%
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* FCF Projections */}
      {projections.length > 0 && (
        <div style={{ overflow: "auto" }}>
          <table className="data-table" style={{ fontSize: "0.8rem" }}>
            <thead>
              <tr>
                <th>Year</th>
                <th>Revenue</th>
                <th>Growth</th>
                <th>EBIT</th>
                <th>EBIT Margin</th>
                <th>NOPAT</th>
                <th>FCF</th>
              </tr>
            </thead>
            <tbody>
              {projections.map((p) => (
                <tr key={p.year}>
                  <td style={{ color: "var(--accent-amber)" }}>FY{p.year}</td>
                  <td>{fmtLarge(p.revenue)}</td>
                  <td style={{ color: "var(--accent-green)" }}>{p.revenue_growth}%</td>
                  <td>{fmtLarge(p.ebit)}</td>
                  <td>{p.ebit_margin}%</td>
                  <td>{fmtLarge(p.nopat)}</td>
                  <td style={{ fontWeight: 600 }}>{fmtLarge(p.fcf)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Sensitivity Heatmap */}
      {sensitivity && (
        <div style={{ marginTop: "var(--gap-md)" }}>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem", color: "var(--text-muted)", marginBottom: 8 }}>
            SENSITIVITY: WACC vs Terminal Growth → Implied Price
          </div>
          <div style={{ overflow: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
              <thead>
                <tr>
                  <th style={{ padding: "6px 10px", color: "var(--text-muted)" }}>WACC ↓ \ TG →</th>
                  {((sensitivity.tg_axis as number[]) || []).map((tg) => (
                    <th key={tg} style={{ padding: "6px 10px", color: "var(--accent-amber)" }}>{tg}%</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {((sensitivity.grid as (number | null)[][]) || []).map((row, ri) => (
                  <tr key={ri}>
                    <td style={{ padding: "6px 10px", color: "var(--accent-cyan)", fontWeight: 600 }}>
                      {((sensitivity.wacc_axis as number[]) || [])[ri]}%
                    </td>
                    {row.map((cell, ci) => {
                      const isBase = ri === (sensitivity.base_wacc_idx as number) && ci === (sensitivity.base_tg_idx as number);
                      return (
                        <td key={ci} style={{
                          padding: "6px 10px",
                          textAlign: "center",
                          background: isBase ? "rgba(0,229,255,0.15)" : "var(--bg-surface)",
                          border: isBase ? "1px solid var(--accent-cyan)" : "1px solid var(--border-color)",
                          fontWeight: isBase ? 700 : 400,
                          color: cell === null ? "var(--text-muted)" : "var(--text-primary)",
                        }}>
                          {cell !== null ? `₹${cell.toLocaleString()}` : "—"}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Earnings Section ───────────────────────────────────────
function EarningsSection({ data }: { data: Record<string, unknown> | null }) {
  if (!data) return <SectionShimmer />;
  if (data.error) {
    return (
      <div className="card" style={{ marginBottom: "var(--gap-lg)", border: "1px solid var(--accent-red)", background: "rgba(255,56,96,0.05)" }}>
        <div className="card-header">
          <span className="card-title" style={{ color: "var(--accent-red)" }}>▸ Earnings Snapshot — Error</span>
        </div>
        <div style={{ padding: "var(--gap-md)", color: "var(--accent-red)", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
          {data.error as string}
        </div>
      </div>
    );
  }
  if (!(data.available)) return (
    <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
      <div className="card-header"><span className="card-title">▸ Earnings Snapshot</span></div>
      <div style={{ padding: 20, color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
        {data.note as string || "No earnings data available yet."}
      </div>
    </div>
  );

  const beatMiss = data.beat_miss as Record<string, unknown> | undefined;
  const results = (beatMiss?.results as Record<string, Record<string, unknown>>) || {};
  const margins = (data.margin_trends as Record<string, Record<string, unknown>>) || {};
  const takeaways = (data.key_takeaways as string[]) || [];

  return (
    <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
      <div className="card-header">
        <span className="card-title">▸ Earnings Snapshot</span>
      </div>

      {/* Key Takeaways */}
      {takeaways.length > 0 && (
        <div style={{ marginBottom: "var(--gap-md)", padding: "var(--gap-md)", background: "var(--bg-surface)", borderRadius: "var(--radius-md)", border: "1px solid var(--border-color)" }}>
          {takeaways.map((t, i) => (
            <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: "0.8rem", color: "var(--text-primary)", marginBottom: 4 }}>
              • {t}
            </div>
          ))}
        </div>
      )}

      {/* Beat/Miss */}
      {Object.keys(results).length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "var(--gap-sm)", marginBottom: "var(--gap-md)" }}>
          {Object.entries(results).map(([metric, r]) => (
            <div key={metric} style={{
              padding: "var(--gap-md)",
              background: "var(--bg-surface)",
              borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-color)",
            }}>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.65rem", color: "var(--text-muted)", textTransform: "uppercase" }}>
                {metric.replace(/_/g, " ")}
              </div>
              <div style={{
                fontFamily: "var(--font-mono)",
                fontSize: "1.1rem",
                fontWeight: 700,
                color: r.verdict === "BEAT" ? "var(--accent-green)" : r.verdict === "MISS" ? "var(--accent-red)" : "var(--accent-amber)",
                marginTop: 4,
              }}>
                {r.verdict as string}
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem", color: "var(--text-secondary)", marginTop: 2 }}>
                {(r.change_pct as number) > 0 ? "+" : ""}{r.change_pct as number}% QoQ
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Margin Trends */}
      {Object.keys(margins).length > 0 && (
        <div style={{ display: "flex", gap: "var(--gap-sm)", flexWrap: "wrap" }}>
          {Object.entries(margins).map(([metric, m]) => (
            <div key={metric} style={{
              padding: "8px 14px",
              background: "var(--bg-surface)",
              borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-color)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.75rem",
            }}>
              <span style={{ color: "var(--text-muted)" }}>{metric.replace(/_/g, " ").toUpperCase()}: </span>
              <span style={{ fontWeight: 600 }}>{m.current as number}%</span>
              <span style={{
                marginLeft: 6,
                color: m.trend === "expanding" ? "var(--accent-green)" : m.trend === "contracting" ? "var(--accent-red)" : "var(--text-muted)",
              }}>
                {m.trend === "expanding" ? "▲" : m.trend === "contracting" ? "▼" : "→"} {m.change_bps as number}bps
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Loading Shimmer ────────────────────────────────────────
function SectionShimmer() {
  return (
    <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
      <div className="loading-shimmer" style={{ height: 16, width: 200, marginBottom: 16 }} />
      <div className="loading-shimmer" style={{ height: 120, width: "100%" }} />
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────
export default function StockResearchPage() {
  const params = useParams();
  const symbol = (params.symbol as string || "").toUpperCase();

  const [comps, setComps] = useState<Record<string, unknown> | null>(null);
  const [dcf, setDcf] = useState<Record<string, unknown> | null>(null);
  const [earnings, setEarnings] = useState<Record<string, unknown> | null>(null);
  const [activeTab, setActiveTab] = useState("all");

  useEffect(() => {
    if (!symbol) return;
    fetch(`${API}/research/comps/${symbol}`).then(r => r.json()).then(setComps).catch(() => setComps({}));
    fetch(`${API}/research/dcf/${symbol}`).then(r => r.json()).then(setDcf).catch(() => setDcf({}));
    fetch(`${API}/research/earnings/${symbol}`).then(r => r.json()).then(setEarnings).catch(() => setEarnings({}));
  }, [symbol]);

  const tabs = [
    { key: "all", label: "All Analysis" },
    { key: "comps", label: "Comps" },
    { key: "dcf", label: "DCF" },
    { key: "earnings", label: "Earnings" },
  ];

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ color: "var(--accent-cyan)" }}>{symbol}</span>
            <span style={{ fontSize: "0.8rem", color: "var(--text-muted)", fontWeight: 400 }}>
              Equity Research
            </span>
          </h1>
          <p className="page-subtitle">
            Institutional-grade analysis · Adapted from Anthropic Financial Services
          </p>
        </div>
        <a
          href="/research"
          style={{
            padding: "8px 16px",
            background: "var(--bg-card)",
            border: "1px solid var(--border-color)",
            borderRadius: "var(--radius-md)",
            color: "var(--text-secondary)",
            fontFamily: "var(--font-mono)",
            fontSize: "0.8rem",
            textDecoration: "none",
          }}
        >
          ← Back
        </a>
      </div>

      {/* Top: live chart + 3-horizon signals + Pin (same panel as /screener Custom). */}
      {symbol && <SymbolAnalysisPanel symbol={symbol} />}

      {/* Tab Navigation */}
      <div style={{ display: "flex", gap: "var(--gap-xs)", marginBottom: "var(--gap-lg)", borderBottom: "1px solid var(--border-color)", paddingBottom: "var(--gap-sm)", marginTop: "var(--gap-lg)" }}>
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            style={{
              padding: "6px 16px",
              background: activeTab === t.key ? "var(--accent-green-glow)" : "transparent",
              border: activeTab === t.key ? "1px solid rgba(0,255,136,0.2)" : "1px solid transparent",
              borderRadius: "var(--radius-sm)",
              color: activeTab === t.key ? "var(--accent-green)" : "var(--text-muted)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.8rem",
              cursor: "pointer",
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Sections */}
      {(activeTab === "all" || activeTab === "comps") && <CompsSection data={comps} />}
      {(activeTab === "all" || activeTab === "dcf") && <DCFSection data={dcf} />}
      {(activeTab === "all" || activeTab === "earnings") && <EarningsSection data={earnings} />}
    </div>
  );
}

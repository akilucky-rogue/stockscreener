"use client";

import { useEffect, useState } from "react";

const API = "http://127.0.0.1:8000/api";

interface SectorInfo {
  sector: string;
  count: number;
}

interface FactorStats {
  ic_60d: number | null;
  hit_rate_top: number | null;
  hit_rate_bot: number | null;
  sharpe_ann: number | null;
  n_observations: number;
  composite_weight: number;
  ic_history: { date: string; ic: number | null }[];
}

interface StrategyPerf {
  n_closed: number;
  hit_rate: number | null;
  avg_net_ret_bps: number | null;
  realized_sharpe: number | null;
}

interface Tier1Diagnostics {
  as_of: string;
  factors: Record<string, { swing: FactorStats; long: FactorStats }>;
  composite_vs_baselines: Record<string, { swing?: StrategyPerf; long?: StrategyPerf }>;
  note: string | null;
}

const FACTOR_LABELS: Record<string, { name: string; desc: string }> = {
  jt:   { name: "Jegadeesh-Titman",  desc: "12-1 cross-sectional momentum" },
  mop:  { name: "MOP TS-Momentum",   desc: "Vol-scaled time-series momentum" },
  bab:  { name: "Betting-Against-β", desc: "Frazzini-Pedersen low-beta tilt" },
  rsi2: { name: "Connors RSI(2)",    desc: "Mean reversion above SMA(200)" },
};

function Sparkline({ data, width = 120, height = 28 }: {
  data: { date: string; ic: number | null }[];
  width?: number;
  height?: number;
}) {
  const points = data.filter((d) => d.ic != null) as { date: string; ic: number }[];
  if (points.length < 2) {
    return <div style={{ width, height, fontSize: "0.6rem", color: "var(--text-muted)" }}>—</div>;
  }
  const ics = points.map((p) => p.ic);
  const min = Math.min(...ics, 0);
  const max = Math.max(...ics, 0);
  const range = (max - min) || 1;
  const path = points.map((p, i) => {
    const x = (i / (points.length - 1)) * width;
    const y = height - ((p.ic - min) / range) * height;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const zeroY = height - ((0 - min) / range) * height;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <line x1={0} y1={zeroY} x2={width} y2={zeroY}
            stroke="var(--text-muted)" strokeWidth={0.5} strokeDasharray="2,2" />
      <path d={path} fill="none" stroke="var(--accent-cyan)" strokeWidth={1.5} />
    </svg>
  );
}

function FactorCard({ name, stats }: { name: string; stats: { swing: FactorStats; long: FactorStats } }) {
  const meta = FACTOR_LABELS[name] || { name, desc: "" };
  const fmt = (v: number | null, digits = 3, suffix = "") =>
    v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + v.toFixed(digits) + suffix;
  const fmtPct = (v: number | null) =>
    v == null || !isFinite(v) ? "—" : (v * 100).toFixed(0) + "%";

  return (
    <div style={{
      padding: "12px 14px", background: "var(--bg-card)",
      border: "1px solid var(--border-color)", borderRadius: "var(--radius-sm)",
    }}>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.8rem", fontWeight: 600,
                    color: "var(--accent-cyan)", marginBottom: 2 }}>
        {meta.name}
      </div>
      <div style={{ fontSize: "0.65rem", color: "var(--text-muted)", marginBottom: 10 }}>
        {meta.desc}
      </div>
      {(["swing", "long"] as const).map((hzn) => {
        const s = stats[hzn];
        if (!s) return null;
        return (
          <div key={hzn} style={{ marginBottom: 8, paddingBottom: 8,
                                  borderBottom: hzn === "swing" ? "1px dashed var(--border-color)" : "none" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
              <span style={{ fontSize: "0.7rem", color: "var(--text-secondary)", textTransform: "uppercase" }}>
                {hzn}
              </span>
              <Sparkline data={s.ic_history} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4,
                          fontFamily: "var(--font-mono)", fontSize: "0.7rem" }}>
              <div>IC<sub>60d</sub>: <span style={{ color: (s.ic_60d ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                {fmt(s.ic_60d, 4)}
              </span></div>
              <div>Sharpe: <span style={{ color: (s.sharpe_ann ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                {fmt(s.sharpe_ann, 2)}
              </span></div>
              <div>Hit↑: {fmtPct(s.hit_rate_top)}</div>
              <div>Hit↓: {fmtPct(s.hit_rate_bot)}</div>
              <div>n={s.n_observations}</div>
              <div>w={s.composite_weight.toFixed(3)}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StrategyComparison({ perf }: {
  perf: Record<string, { swing?: StrategyPerf; long?: StrategyPerf }>;
}) {
  const order = ["model", "tier1_composite", "tier1_jt", "tier1_mop", "tier1_bab", "tier1_rsi2",
                 "baseline_top_momentum", "baseline_nifty", "baseline_random"];
  const rows = order.filter((s) => perf[s]);
  if (rows.length === 0) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: "0.7rem", color: "var(--text-secondary)", marginBottom: 8,
                    textTransform: "uppercase", letterSpacing: 0.5 }}>
        Realized performance — closed paper trades
      </div>
      <table style={{ width: "100%", fontSize: "0.7rem", fontFamily: "var(--font-mono)",
                      borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ color: "var(--text-muted)", borderBottom: "1px solid var(--border-color)" }}>
            <th style={{ textAlign: "left", padding: "4px 6px" }}>Strategy</th>
            <th style={{ textAlign: "right", padding: "4px 6px" }}>n</th>
            <th style={{ textAlign: "right", padding: "4px 6px" }}>Hit</th>
            <th style={{ textAlign: "right", padding: "4px 6px" }}>Avg bps</th>
            <th style={{ textAlign: "right", padding: "4px 6px" }}>Sharpe</th>
            <th style={{ textAlign: "left", padding: "4px 6px" }}>Hzn</th>
          </tr>
        </thead>
        <tbody>
          {rows.flatMap((strat) =>
            (["swing", "long"] as const).filter((h) => perf[strat]?.[h]).map((h) => {
              const p = perf[strat][h]!;
              return (
                <tr key={`${strat}-${h}`} style={{ borderBottom: "1px solid var(--bg-surface)" }}>
                  <td style={{ padding: "3px 6px", color: strat.startsWith("tier1") ? "var(--accent-cyan)"
                                              : strat === "model" ? "var(--accent-amber)" : "var(--text-muted)" }}>
                    {strat}
                  </td>
                  <td style={{ textAlign: "right", padding: "3px 6px" }}>{p.n_closed}</td>
                  <td style={{ textAlign: "right", padding: "3px 6px" }}>
                    {p.hit_rate == null ? "—" : (p.hit_rate * 100).toFixed(0) + "%"}
                  </td>
                  <td style={{ textAlign: "right", padding: "3px 6px",
                               color: (p.avg_net_ret_bps ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                    {p.avg_net_ret_bps == null ? "—" : (p.avg_net_ret_bps > 0 ? "+" : "") + p.avg_net_ret_bps.toFixed(0)}
                  </td>
                  <td style={{ textAlign: "right", padding: "3px 6px",
                               color: (p.realized_sharpe ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
                    {p.realized_sharpe == null ? "—" : p.realized_sharpe.toFixed(2)}
                  </td>
                  <td style={{ padding: "3px 6px", color: "var(--text-muted)" }}>{h}</td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function ResearchPage() {
  const [sectors, setSectors] = useState<SectorInfo[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [tier1, setTier1] = useState<Tier1Diagnostics | null>(null);

  useEffect(() => {
    fetch(`${API}/research/sectors`)
      .then((r) => r.json())
      .then((d) => setSectors(d.sectors || []))
      .catch(() => {});
    fetch(`${API}/research/tier1/diagnostics`)
      .then((r) => r.json())
      .then((d: Tier1Diagnostics) => setTier1(d))
      .catch(() => setTier1(null));
  }, []);

  const nifty200 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "WIPRO", "ASIANPAINT",
    "MARUTI", "HCLTECH", "TATAMOTORS", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "BAJFINANCE", "NTPC", "POWERGRID", "M&M", "ADANIENT", "JSWSTEEL",
    "TATASTEEL", "NESTLEIND", "DIVISLAB", "DRREDDY",
  ];

  const filtered = searchQuery
    ? nifty200.filter((s) => s.toLowerCase().includes(searchQuery.toLowerCase()))
    : nifty200;

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">◆ EQUITY RESEARCH</h1>
          <p className="page-subtitle">
            Institutional-grade analysis · Comps · DCF · Earnings · Sector Overview
          </p>
        </div>
      </div>

      {/* Tier 1 Rule-Based Engine — IC validation + composite vs baselines */}
      {tier1 && (
        <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
          <div className="card-header">
            <span className="card-title">▸ TIER 1 RULE ENGINE — Factor Diagnostics</span>
            <span style={{ fontSize: "0.7rem", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              as of {tier1.as_of}
            </span>
          </div>
          {tier1.note && (
            <div style={{
              padding: "8px 12px", marginBottom: 12,
              background: "rgba(255,193,7,0.08)",
              border: "1px solid var(--accent-amber)",
              borderRadius: "var(--radius-sm)",
              fontSize: "0.75rem", color: "var(--accent-amber)",
              fontFamily: "var(--font-mono)",
            }}>
              ⚠ {tier1.note}
            </div>
          )}
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "var(--gap-sm)",
          }}>
            {Object.entries(tier1.factors).map(([fname, stats]) => (
              <FactorCard key={fname} name={fname} stats={stats} />
            ))}
          </div>
          <StrategyComparison perf={tier1.composite_vs_baselines} />
        </div>
      )}

      {/* Search */}
      <div style={{ marginBottom: "var(--gap-lg)" }}>
        <input
          type="text"
          placeholder="Search symbol (e.g. RELIANCE, TCS, INFY)..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && searchQuery) {
              window.location.href = `/research/${searchQuery.toUpperCase()}`;
            }
          }}
          style={{
            width: "100%",
            maxWidth: 500,
            padding: "12px 20px",
            background: "var(--bg-card)",
            border: "1px solid var(--border-color)",
            borderRadius: "var(--radius-md)",
            color: "var(--text-primary)",
            fontFamily: "var(--font-mono)",
            fontSize: "0.9rem",
            outline: "none",
          }}
        />
      </div>

      {/* Quick Access Grid */}
      <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="card-header">
          <span className="card-title">▸ Quick Research — Top Nifty Stocks</span>
        </div>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))",
          gap: "var(--gap-sm)",
        }}>
          {filtered.map((sym) => (
            <a
              key={sym}
              href={`/research/${sym}`}
              style={{
                display: "block",
                padding: "10px 14px",
                background: "var(--bg-surface)",
                border: "1px solid var(--border-color)",
                borderRadius: "var(--radius-sm)",
                color: "var(--accent-cyan)",
                fontFamily: "var(--font-mono)",
                fontSize: "0.8rem",
                fontWeight: 600,
                textDecoration: "none",
                textAlign: "center",
                transition: "all 0.15s ease",
              }}
              onMouseOver={(e) => {
                (e.target as HTMLElement).style.borderColor = "var(--accent-cyan)";
                (e.target as HTMLElement).style.background = "rgba(0,229,255,0.05)";
              }}
              onMouseOut={(e) => {
                (e.target as HTMLElement).style.borderColor = "var(--border-color)";
                (e.target as HTMLElement).style.background = "var(--bg-surface)";
              }}
            >
              {sym}
            </a>
          ))}
        </div>
      </div>

      {/* Sector Grid */}
      <div className="card">
        <div className="card-header">
          <span className="card-title">▸ Sectors</span>
        </div>
        {sectors.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
            Run universe sync to populate sectors
          </div>
        ) : (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
            gap: "var(--gap-sm)",
          }}>
            {sectors.map((s) => (
              <a
                key={s.sector}
                href={`/research/sector/${encodeURIComponent(s.sector)}`}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "12px 16px",
                  background: "var(--bg-surface)",
                  border: "1px solid var(--border-color)",
                  borderRadius: "var(--radius-sm)",
                  color: "var(--text-primary)",
                  fontFamily: "var(--font-sans)",
                  fontSize: "0.85rem",
                  textDecoration: "none",
                  transition: "all 0.15s ease",
                }}
              >
                <span>{s.sector}</span>
                <span style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.75rem",
                  color: "var(--accent-amber)",
                  background: "var(--accent-amber-glow)",
                  padding: "2px 8px",
                  borderRadius: 12,
                }}>{s.count}</span>
              </a>
            ))}
          </div>
        )}
      </div>

      {/* Analysis Types */}
      <div style={{ marginTop: "var(--gap-lg)" }}>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
          gap: "var(--gap-md)",
        }}>
          {[
            {
              title: "Comparable Company Analysis",
              desc: "Peer valuation with quartile stats — P/E, EV/EBITDA, margins, ROE benchmarking across sector peers",
              icon: "📊",
              source: "comps-analysis",
            },
            {
              title: "DCF Valuation Model",
              desc: "5-year cash flow projections, WACC via CAPM, terminal value, sensitivity grid, Bear/Base/Bull scenarios",
              icon: "📈",
              source: "dcf-model",
            },
            {
              title: "Earnings Analysis",
              desc: "Quarterly beat/miss analysis, margin trends, revenue trajectory, key takeaways",
              icon: "📋",
              source: "earnings-analysis",
            },
            {
              title: "Idea Generation & Screening",
              desc: "Value/Growth/Quality/Momentum presets with custom criteria across the Nifty 200 universe",
              icon: "💡",
              source: "idea-generation",
            },
          ].map((a) => (
            <div key={a.title} className="card">
              <div style={{ fontSize: "1.5rem", marginBottom: 8 }}>{a.icon}</div>
              <div style={{
                fontFamily: "var(--font-mono)",
                fontSize: "0.85rem",
                fontWeight: 600,
                color: "var(--text-bright)",
                marginBottom: 6,
              }}>{a.title}</div>
              <div style={{ fontSize: "0.8rem", color: "var(--text-secondary)", lineHeight: 1.5 }}>
                {a.desc}
              </div>
              <div style={{
                marginTop: 10,
                fontSize: "0.65rem",
                fontFamily: "var(--font-mono)",
                color: "var(--text-muted)",
              }}>
                Adapted from: {a.source}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

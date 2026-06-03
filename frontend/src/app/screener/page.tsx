"use client";

/**
 * /screener — multi-criteria fundamental screen with the full /analyze view
 * (chart + signals + fundamentals + Pin) inline on row expand.
 *
 *   1. Presets (Value/Growth/Quality/Momentum/Dividend) hit
 *      GET /api/research/screen and render the existing fundamental table.
 *   2. Click any row -> SymbolAnalysisPanel opens below the table:
 *      identity + Pin + live chart (1s poll) + historical tabs + 3 horizon
 *      signal cards (precise action tier + valid_until + exit_by + entry/
 *      target/stop) + fundamentals.
 *   3. New "Custom" preset: type any NSE symbol, get the same rich panel
 *      with no fundamental gate. Replaces the old /analyze + /live pages.
 *   4. Clicking the SYMBOL link in any row still navigates to
 *      /research/{symbol}; clicking anywhere else expands inline.
 *
 * Backend auto-subscribes the symbol to Kite ticks on first /api/analysis/
 * intraday call, so the user never has to run kite_stream.py manually.
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import SymbolAnalysisPanel from "../../components/SymbolAnalysisPanel";

const API = "http://127.0.0.1:8000/api";
const mono = "var(--font-mono)";

interface ScreenResult {
  symbol: string;
  company_name: string;
  sector: string;
  market_cap: number | null;
  pe_ratio: number | null;
  pb_ratio: number | null;
  gross_margin: number | null;
  operating_margin: number | null;
  roe: number | null;
  roic: number | null;
  dividend_yield: number | null;
  revenue_growth: number | null;
  debt_to_equity: number | null;
  current_price?: number | null;
  mom_1m?: number | null;
  mom_3m?: number | null;
}

interface ScreenResponse {
  screen: string;
  description: string;
  results: ScreenResult[];
  passing_count: number;
  total_screened: number;
  pass_rate: number;
  available_presets: string[];
  filters_applied?: Record<string, Record<string, unknown>>;
  note?: string;
}

const PRESETS = [
  { key: "value",    label: "Value",    icon: "💎", desc: "Undervalued + strong cash" },
  { key: "growth",   label: "Growth",   icon: "🚀", desc: "High-growth, expanding margins" },
  { key: "quality",  label: "Quality",  icon: "🏆", desc: "High ROIC compounders" },
  { key: "momentum", label: "Momentum", icon: "⚡", desc: "Price + earnings momentum" },
  { key: "dividend", label: "Dividend", icon: "💰", desc: "Sustainable high yield" },
  { key: "custom",   label: "Custom",   icon: "🔎", desc: "Search any NSE symbol live" },
];

function fmt(val: number | null | undefined, suffix = "", decimals = 1): string {
  if (val === null || val === undefined) return "—";
  return `${val.toFixed(decimals)}${suffix}`;
}
function fmtCr(val: number | null | undefined): string {
  if (val === null || val === undefined) return "—";
  if (val >= 1e5) return `₹${(val / 1e5).toFixed(0)}L Cr`;
  if (val >= 1e3) return `₹${(val / 1e3).toFixed(0)}K Cr`;
  return `₹${val.toFixed(0)} Cr`;
}
function momColor(val: number | null | undefined): string {
  if (val === null || val === undefined) return "var(--text-muted)";
  return val > 0 ? "var(--accent-green)" : val < 0 ? "var(--accent-red)" : "var(--text-secondary)";
}
function emptyMessage(preset: string, note?: string): string {
  if (note) return note;
  if (preset === "quality") return "Quality criteria matched zero names. Try Value or Momentum, or relax thresholds.";
  return "No results. Run data ingestion first.";
}

/** Normalize user input — strip .NS / .BO suffixes, uppercase. */
function normalizeSymbol(raw: string): string {
  const s = raw.trim().toUpperCase();
  if (s.endsWith(".NS") || s.endsWith(".BO")) return s.slice(0, -3);
  return s;
}

export default function ScreenerPage() {
  const router = useRouter();
  const [preset, setPreset] = useState("value");
  const [data, setData] = useState<ScreenResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortAsc, setSortAsc] = useState(true);

  // Custom-tab inline rich panel only. Preset rows route to /research/{sym}.
  const [customSymbol, setCustomSymbol] = useState<string | null>(null);
  const [customInput, setCustomInput] = useState("KEI");

  const runScreen = useCallback((p: string) => {
    setPreset(p);
    setCustomSymbol(null);
    if (p === "custom") {
      setData(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    fetch(`${API}/research/screen?preset=${p}&limit=100&include_momentum=true`)
      .then((r) => r.json())
      .then((d) => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  useEffect(() => { runScreen("value"); }, [runScreen]);

  const handleSort = (col: string) => {
    if (sortCol === col) setSortAsc(!sortAsc);
    else { setSortCol(col); setSortAsc(false); }
  };

  const sortedResults = data?.results ? [...data.results].sort((a, b) => {
    if (!sortCol) return 0;
    const av = (a as unknown as Record<string, unknown>)[sortCol] as number | null;
    const bv = (b as unknown as Record<string, unknown>)[sortCol] as number | null;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return sortAsc ? av - bv : bv - av;
  }) : [];

  const submitCustom = () => {
    const sym = normalizeSymbol(customInput);
    if (!sym) return;
    setCustomInput(sym);
    setCustomSymbol(sym);
  };
  const goToResearch = (sym: string) => router.push(`/research/${encodeURIComponent(sym)}`);

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">⊞ STOCK SCREENER</h1>
          <p className="page-subtitle">
            Multi-criteria screening · Click any row for the full analysis — live chart, 3-horizon signals
            with precise entry/exit timing, and Pin-to-track. Custom tab works on any NSE/BSE symbol.
          </p>
        </div>
        {data && preset !== "custom" && (
          <div style={{ fontFamily: mono, fontSize: "0.8rem", color: "var(--text-secondary)" }}>
            <span style={{ color: "var(--accent-green)" }}>{data.passing_count}</span>
            {" / "}
            {data.total_screened} pass
            <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>({data.pass_rate}%)</span>
          </div>
        )}
      </div>

      {/* Preset tabs */}
      <div style={{ display: "flex", gap: "var(--gap-sm)", marginBottom: "var(--gap-lg)", flexWrap: "wrap" }}>
        {PRESETS.map((p) => (
          <button key={p.key} onClick={() => runScreen(p.key)} style={{
            background: preset === p.key ? "var(--accent-green-glow)" : "var(--bg-card)",
            border: `1px solid ${preset === p.key ? "rgba(0,255,136,0.3)" : "var(--border-color)"}`,
            borderRadius: "var(--radius-md)",
            padding: "10px 20px",
            color: preset === p.key ? "var(--accent-green)" : "var(--text-secondary)",
            fontFamily: mono,
            fontSize: "0.85rem",
            cursor: "pointer",
            transition: "all 0.2s ease",
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-start",
            gap: 2,
            minWidth: 140,
          }}>
            <span style={{ fontSize: "1rem" }}>{p.icon} {p.label}</span>
            <span style={{ fontSize: "0.65rem", color: "var(--text-muted)", fontFamily: "var(--font-sans)" }}>{p.desc}</span>
          </button>
        ))}
      </div>

      {/* Filter chips */}
      {preset !== "custom" && data?.filters_applied && Object.keys(data.filters_applied).length > 0 && (
        <div style={{ display: "flex", gap: "var(--gap-sm)", marginBottom: "var(--gap-md)", flexWrap: "wrap" }}>
          {Object.entries(data.filters_applied).map(([key, val]) => (
            <span key={key} style={{
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: 20, padding: "4px 12px",
              fontFamily: mono, fontSize: "0.7rem", color: "var(--accent-amber)",
            }}>{(val as Record<string, unknown>).label as string || key}</span>
          ))}
        </div>
      )}

      {/* Custom search */}
      {preset === "custom" && (
        <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
          <div style={{ display: "flex", gap: "var(--gap-sm)", alignItems: "center", flexWrap: "wrap" }}>
            <input
              value={customInput}
              onChange={(e) => setCustomInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") submitCustom(); }}
              placeholder="Any symbol: KEI, RELIANCE.NS, 500325, TATAMOTORS.BO ..."
              style={{
                padding: "8px 12px",
                background: "var(--bg-surface)",
                border: "1px solid var(--border-color)",
                borderRadius: "var(--radius-sm)",
                color: "var(--text-primary)",
                fontFamily: mono,
                textTransform: "uppercase",
                flex: 1,
                minWidth: 240,
              }}
            />
            <button onClick={submitCustom} style={{
              padding: "8px 20px",
              background: "var(--accent-green-glow)",
              border: "1px solid rgba(0,255,136,0.3)",
              borderRadius: "var(--radius-sm)",
              color: "var(--accent-green)",
              fontFamily: mono, fontWeight: 700, cursor: "pointer",
            }}>▶ Analyze</button>
            <span style={{ fontFamily: mono, fontSize: "0.7rem", color: "var(--text-muted)" }}>
              Auto-subscribes Kite ticks · live chart + 3-horizon signals + Pin
            </span>
          </div>
        </div>
      )}

      {/* Results table (presets) */}
      {preset !== "custom" && (
        <div className="card" style={{ overflow: "auto", maxHeight: "calc(100vh - 280px)" }}>
          {loading ? (
            <div style={{ padding: 40, textAlign: "center" }}>
              <div className="loading-shimmer" style={{ height: 20, width: 200, margin: "0 auto 12px" }} />
              <div style={{ color: "var(--text-muted)", fontFamily: mono, fontSize: "0.8rem" }}>Running screen...</div>
            </div>
          ) : sortedResults.length === 0 ? (
            <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)", fontFamily: mono }}>
              {emptyMessage(preset, data?.note)}
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th style={{ width: 40 }}>#</th>
                  <th onClick={() => handleSort("symbol")} style={{ cursor: "pointer" }}>Symbol</th>
                  <th>Company</th>
                  <th>Sector</th>
                  <th onClick={() => handleSort("market_cap")} style={{ cursor: "pointer" }}>Mkt Cap</th>
                  <th onClick={() => handleSort("pe_ratio")} style={{ cursor: "pointer" }}>P/E</th>
                  <th onClick={() => handleSort("pb_ratio")} style={{ cursor: "pointer" }}>P/B</th>
                  <th onClick={() => handleSort("gross_margin")} style={{ cursor: "pointer" }}>Gross%</th>
                  <th onClick={() => handleSort("roe")} style={{ cursor: "pointer" }}>ROE%</th>
                  <th onClick={() => handleSort("revenue_growth")} style={{ cursor: "pointer" }}>Rev Gr%</th>
                  <th onClick={() => handleSort("dividend_yield")} style={{ cursor: "pointer" }}>Div%</th>
                  <th onClick={() => handleSort("mom_1m")} style={{ cursor: "pointer" }}>1M</th>
                  <th onClick={() => handleSort("mom_3m")} style={{ cursor: "pointer" }}>3M</th>
                </tr>
              </thead>
              <tbody>
                {sortedResults.map((r, i) => (
                  <tr key={r.symbol} onClick={() => goToResearch(r.symbol)} style={{
                    animationDelay: `${i * 20}ms`,
                    cursor: "pointer",
                  }}>
                    <td style={{ color: "var(--text-muted)" }}>{i + 1}</td>
                    <td>
                      <span style={{ color: "var(--accent-cyan)", fontWeight: 600 }}>{r.symbol}</span>
                    </td>
                    <td style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.company_name || "—"}
                    </td>
                    <td style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}>{r.sector || "—"}</td>
                    <td>{fmtCr(r.market_cap)}</td>
                    <td>{fmt(r.pe_ratio, "x")}</td>
                    <td>{fmt(r.pb_ratio, "x")}</td>
                    <td>{fmt(r.gross_margin, "%")}</td>
                    <td style={{ color: r.roe && r.roe > 15 ? "var(--accent-green)" : "inherit" }}>{fmt(r.roe, "%")}</td>
                    <td style={{ color: r.revenue_growth && r.revenue_growth > 15 ? "var(--accent-green)" : "inherit" }}>
                      {fmt(r.revenue_growth, "%")}
                    </td>
                    <td>{fmt(r.dividend_yield, "%")}</td>
                    <td style={{ color: momColor(r.mom_1m) }}>{fmt(r.mom_1m, "%")}</td>
                    <td style={{ color: momColor(r.mom_3m) }}>{fmt(r.mom_3m, "%")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Rich panel — Custom mode only. Preset row clicks navigate away. */}
      {preset === "custom" && customSymbol && (
        <SymbolAnalysisPanel symbol={customSymbol} onClose={() => setCustomSymbol(null)} />
      )}
    </div>
  );
}

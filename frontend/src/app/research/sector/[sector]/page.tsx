"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

const API = "http://127.0.0.1:8000/api";

type Company = {
  symbol: string;
  company_name: string | null;
  market_cap: number | null;
  revenue: number | null;
  pe_ratio: number | null;
  pb_ratio: number | null;
  roe: number | null;
  net_margin: number | null;
  dividend_yield: number | null;
};

type Stats = {
  median?: number;
  mean?: number;
  p25?: number;
  p75?: number;
  min?: number;
  max?: number;
};

type Leader = { symbol: string; company_name: string | null; value: number };

function fmtLarge(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "-";
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(0);
}

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "-";
  return v.toFixed(digits);
}

export default function SectorPage() {
  const params = useParams();
  const sector = decodeURIComponent((params.sector as string) || "");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!sector) return;
    setLoading(true);
    fetch(`${API}/research/sector/${encodeURIComponent(sector)}`)
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false); });
  }, [sector]);

  const companies: Company[] = data?.companies || [];
  const stats: Record<string, Stats> = data?.aggregate_stats || {};
  const leaders: Record<string, Leader[]> = data?.leaders || {};

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ color: "var(--accent-cyan)" }}>{sector}</span>
            <span style={{ fontSize: "0.8rem", color: "var(--text-muted)", fontWeight: 400 }}>
              Sector Overview
            </span>
          </h1>
          <p className="page-subtitle">
            Aggregate fundamentals, leaders, and company list for this sector.
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

      {loading ? (
        <div className="loading-shimmer" style={{ height: 400 }} />
      ) : !data?.available ? (
        <div className="card">
          <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
            {data?.note || "No companies found in this sector."}
          </div>
        </div>
      ) : (
        <>
          {/* Headline metrics */}
          <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
            <div className="metric-card">
              <div className="metric-label">Companies</div>
              <div className="metric-value">{data.company_count}</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Total Market Cap</div>
              <div className="metric-value">{fmtLarge(data.total_market_cap)}</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Total Revenue</div>
              <div className="metric-value">{fmtLarge(data.total_revenue)}</div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Median P/E</div>
              <div className="metric-value">{fmt(stats.pe_ratio?.median)}</div>
            </div>
          </div>

          {/* Leaders */}
          <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
            <div className="card-header">
              <span className="card-title">▸ Sector leaders</span>
            </div>
            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
              gap: "var(--gap-md)",
            }}>
              {Object.entries(leaders).map(([title, items]) => (
                <div key={title} style={{
                  padding: "var(--gap-md)",
                  background: "var(--bg-surface)",
                  border: "1px solid var(--border-color)",
                  borderRadius: "var(--radius-sm)",
                }}>
                  <div style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.7rem",
                    color: "var(--text-muted)",
                    textTransform: "uppercase",
                    marginBottom: 8,
                  }}>
                    {title}
                  </div>
                  {items.map((it, i) => (
                    <div key={i} style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      padding: "4px 0",
                      borderBottom: i < items.length - 1 ? "1px solid var(--border-color)" : "none",
                    }}>
                      <div>
                        <a
                          href={`/research/${it.symbol}`}
                          style={{
                            color: "var(--accent-cyan)",
                            fontWeight: 600,
                            textDecoration: "none",
                            fontFamily: "var(--font-mono)",
                            fontSize: "0.78rem",
                          }}
                        >
                          {it.symbol}
                        </a>
                        <div style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                          {it.company_name || ""}
                        </div>
                      </div>
                      <div style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: "0.85rem",
                        color: "var(--accent-amber)",
                      }}>
                        {fmt(it.value, 2)}
                      </div>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>

          {/* Aggregate quartiles */}
          <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
            <div className="card-header">
              <span className="card-title">▸ Aggregate stats (quartile distribution)</span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Metric</th>
                    <th>Min</th>
                    <th>P25</th>
                    <th>Median</th>
                    <th>Mean</th>
                    <th>P75</th>
                    <th>Max</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(stats).map(([k, s]) => (
                    <tr key={k}>
                      <td style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                        {k}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(s.min)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(s.p25)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem", color: "var(--accent-amber)" }}>{fmt(s.median)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(s.mean)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(s.p75)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(s.max)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Company table */}
          <div className="card">
            <div className="card-header">
              <span className="card-title">▸ Companies ({companies.length})</span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Company</th>
                    <th>Mkt Cap</th>
                    <th>Revenue</th>
                    <th>P/E</th>
                    <th>P/B</th>
                    <th>ROE%</th>
                    <th>Net Margin%</th>
                    <th>Div Yield%</th>
                  </tr>
                </thead>
                <tbody>
                  {companies.map(c => (
                    <tr
                      key={c.symbol}
                      onClick={() => window.location.href = `/research/${c.symbol}`}
                      style={{ cursor: "pointer" }}
                    >
                      <td style={{ fontWeight: 600, color: "var(--accent-cyan)" }}>{c.symbol}</td>
                      <td style={{
                        maxWidth: 220,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        color: "var(--text-secondary)",
                        fontSize: "0.75rem",
                      }}>{c.company_name || "-"}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmtLarge(c.market_cap)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmtLarge(c.revenue)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(c.pe_ratio, 1)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(c.pb_ratio, 1)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(c.roe, 1)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(c.net_margin, 1)}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>{fmt(c.dividend_yield, 2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

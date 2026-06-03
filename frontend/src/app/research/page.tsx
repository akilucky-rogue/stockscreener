"use client";

import { useEffect, useState } from "react";

const API = "http://127.0.0.1:8000/api";

interface SectorInfo {
  sector: string;
  count: number;
}

export default function ResearchPage() {
  const [sectors, setSectors] = useState<SectorInfo[]>([]);
  const [searchQuery, setSearchQuery] = useState("");

  useEffect(() => {
    fetch(`${API}/research/sectors`)
      .then((r) => r.json())
      .then((d) => setSectors(d.sectors || []))
      .catch(() => {});
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

"use client";

import { useEffect, useMemo, useState } from "react";

const API = "http://127.0.0.1:8000/api";

type Importance = {
  name: string;
  importance: number;
  category: string;
};

type ICRow = {
  name: string;
  ic: number;
  n_obs: number;
  category: string;
};

type Category = { name: string; count: number };

const CAT_COLORS: Record<string, string> = {
  technical:   "var(--accent-cyan)",
  fundamental: "var(--accent-amber)",
  flow:        "var(--accent-green)",
  macro:       "#a78bfa",
  other:       "var(--text-muted)",
};

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}
function fmt(v: number | null | undefined, digits = 4): string {
  if (v == null || Number.isNaN(v)) return "-";
  return v.toFixed(digits);
}

export default function FactorsPage() {
  const [horizon, setHorizon] = useState<"intraday" | "swing" | "long">("swing");
  const [imp, setImp] = useState<any>(null);
  const [ic, setIc] = useState<any>(null);
  const [cats, setCats] = useState<Category[]>([]);
  const [loadingImp, setLoadingImp] = useState(true);
  const [loadingIc, setLoadingIc] = useState(true);

  useEffect(() => {
    setLoadingImp(true);
    fetch(`${API}/factors/importance?horizon=${horizon}&limit=20`)
      .then(r => r.json())
      .then(d => { setImp(d); setLoadingImp(false); });

    setLoadingIc(true);
    fetch(`${API}/factors/ic?horizon=${horizon}&lookback_days=90`)
      .then(r => r.json())
      .then(d => { setIc(d); setLoadingIc(false); });
  }, [horizon]);

  useEffect(() => {
    fetch(`${API}/factors/categories`)
      .then(r => r.json())
      .then(d => setCats(d.categories || []));
  }, []);

  const importance: Importance[] = imp?.features || [];
  const maxImp = useMemo(
    () => Math.max(1, ...importance.map(f => f.importance)),
    [importance],
  );

  const icRows: ICRow[] = (ic?.factors || []).slice(0, 25);
  const maxAbsIc = useMemo(
    () => Math.max(0.01, ...icRows.map(r => Math.abs(r.ic))),
    [icRows],
  );

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">◈ FACTORS</h1>
          <p className="page-subtitle">
            Per-factor importance from the latest LightGBM run, and Spearman IC vs forward returns over the last 90 days.
          </p>
        </div>
      </div>

      {/* Category counts */}
      <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
        {cats.filter(c => c.count > 0).map(c => (
          <div key={c.name} className="metric-card">
            <div className="metric-label" style={{ textTransform: "uppercase" }}>
              {c.name} factors
            </div>
            <div className="metric-value" style={{ color: CAT_COLORS[c.name] || "var(--text-primary)" }}>
              {c.count}
            </div>
          </div>
        ))}
      </div>

      {/* Horizon toggle + model meta */}
      <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
        <div style={{ display: "flex", gap: "var(--gap-md)", alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: 4 }}>
            {(["intraday", "swing", "long"] as const).map(h => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                style={{
                  padding: "6px 14px",
                  background: horizon === h ? "var(--accent-green-glow)" : "transparent",
                  border: horizon === h ? "1px solid rgba(0,255,136,0.3)" : "1px solid var(--border-color)",
                  borderRadius: "var(--radius-sm)",
                  color: horizon === h ? "var(--accent-green)" : "var(--text-secondary)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                }}
              >
                {h === "intraday" ? "INTRADAY (1d)" : h === "swing" ? "SWING (5d)" : "LONG (20d)"}
              </button>
            ))}
          </div>

          {imp && !imp.error && (
            <div style={{
              fontFamily: "var(--font-mono)",
              fontSize: "0.7rem",
              color: "var(--text-muted)",
              display: "flex",
              gap: 16,
            }}>
              <span>Model trained: <span style={{ color: "var(--text-secondary)" }}>{(imp.trained_at || "").slice(0, 16) || "-"}</span></span>
              <span>Features: <span style={{ color: "var(--accent-cyan)" }}>{imp.n_features}</span></span>
              <span>Samples: <span style={{ color: "var(--accent-cyan)" }}>{(imp.n_samples || 0).toLocaleString()}</span></span>
              <span>IC: <span style={{ color: "var(--accent-amber)" }}>{fmt(imp.ic_mean)}</span></span>
              <span>Sharpe: <span style={{ color: "var(--accent-amber)" }}>{fmt(imp.sharpe, 2)}</span></span>
              <span>DSR: <span style={{
                color: (imp.deflated_sharpe || 0) > 0.95 ? "var(--accent-green)" : "var(--accent-amber)",
              }}>{fmt(imp.deflated_sharpe, 4)}</span></span>
            </div>
          )}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--gap-lg)" }}>
        {/* Feature importance */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">▸ Feature Importance (gain)</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
              Top 20
            </span>
          </div>
          {loadingImp ? (
            <div className="loading-shimmer" style={{ height: 400 }} />
          ) : importance.length === 0 ? (
            <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
              No model run logged yet for this horizon.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {importance.map((f, i) => (
                <div key={f.name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{
                    width: 24,
                    textAlign: "right",
                    color: "var(--text-muted)",
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.7rem",
                  }}>
                    {i + 1}
                  </div>
                  <div style={{
                    width: 180,
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.72rem",
                    color: CAT_COLORS[f.category] || "var(--text-primary)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}>
                    {f.name}
                  </div>
                  <div style={{ flex: 1, height: 14, background: "var(--bg-surface)", borderRadius: 2, position: "relative" }}>
                    <div style={{
                      width: `${(f.importance / maxImp) * 100}%`,
                      height: "100%",
                      background: CAT_COLORS[f.category] || "var(--accent-cyan)",
                      opacity: 0.7,
                      borderRadius: 2,
                    }} />
                  </div>
                  <div style={{
                    width: 60,
                    textAlign: "right",
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.7rem",
                    color: "var(--text-secondary)",
                  }}>
                    {f.importance.toFixed(0)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Per-factor IC */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">▸ Spearman IC vs forward returns</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
              90-day window, {horizon === "intraday" ? "1d" : horizon === "swing" ? "5d" : "20d"} fwd
            </span>
          </div>
          {loadingIc ? (
            <div className="loading-shimmer" style={{ height: 400 }} />
          ) : icRows.length === 0 ? (
            <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
              No factor IC data yet.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {icRows.map((r, i) => {
                const pos = r.ic > 0;
                const w = (Math.abs(r.ic) / maxAbsIc) * 50;   // % of half-width
                return (
                  <div key={r.name} style={{
                    display: "grid",
                    gridTemplateColumns: "30px 180px 1fr 60px",
                    alignItems: "center",
                    gap: 8,
                  }}>
                    <div style={{
                      textAlign: "right",
                      color: "var(--text-muted)",
                      fontFamily: "var(--font-mono)",
                      fontSize: "0.7rem",
                    }}>
                      {i + 1}
                    </div>
                    <div style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: "0.72rem",
                      color: CAT_COLORS[r.category] || "var(--text-primary)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}>
                      {r.name}
                    </div>
                    {/* Center-anchored diverging bar */}
                    <div style={{ position: "relative", height: 12, background: "var(--bg-surface)", borderRadius: 2 }}>
                      <div style={{
                        position: "absolute",
                        left: "50%",
                        top: 0,
                        width: 1,
                        height: "100%",
                        background: "var(--border-color)",
                      }} />
                      <div style={{
                        position: "absolute",
                        left:  pos ? "50%" : `${50 - w}%`,
                        width: `${w}%`,
                        top: 0, height: "100%",
                        background: pos ? "var(--accent-green)" : "var(--accent-red)",
                        opacity: 0.7,
                        borderRadius: 2,
                      }} />
                    </div>
                    <div style={{
                      textAlign: "right",
                      fontFamily: "var(--font-mono)",
                      fontSize: "0.72rem",
                      color: pos ? "var(--accent-green)" : "var(--accent-red)",
                    }}>
                      {(r.ic * 100).toFixed(2)}%
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <div style={{
        marginTop: "var(--gap-md)",
        fontFamily: "var(--font-mono)",
        fontSize: "0.7rem",
        color: "var(--text-muted)",
      }}>
        Left: feature importance is LightGBM gain from the latest production model.
        Right: cross-sectional Spearman IC of each factor vs realized {horizon === "intraday" ? "1-day" : horizon === "swing" ? "5-day" : "20-day"} forward returns, pooled over the last 90 days.
        Color = category (cyan technical, amber fundamental, green flow).
      </div>
    </div>
  );
}

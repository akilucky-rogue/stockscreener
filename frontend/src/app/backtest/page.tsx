"use client";

import { useEffect, useMemo, useState } from "react";

const API = "http://127.0.0.1:8000/api";

type Run = {
  run_id: number;
  horizon: string;
  model_type: string;
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  n_features: number;
  n_samples: number;
  ic_mean: number | null;
  sharpe: number | null;
  deflated_sharpe: number | null;
  psr: number | null;
  direction_accuracy: number | null;
  params: Record<string, unknown>;
  model_hash: string | null;
  created_at: string;
};

function fmt(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return "-";
  return v.toFixed(digits);
}

function dsrColor(d: number | null): string {
  if (d == null) return "var(--text-muted)";
  if (d >= 0.95) return "var(--accent-green)";
  if (d >= 0.80) return "var(--accent-amber)";
  return "var(--accent-red)";
}

export default function BacktestPage() {
  const [latest, setLatest] = useState<Run[]>([]);
  const [history, setHistory] = useState<Run[]>([]);
  const [selectedHorizon, setSelectedHorizon] = useState<string>("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/backtest/latest`)
      .then(r => r.json())
      .then(d => setLatest(d.runs || []));
  }, []);

  useEffect(() => {
    setLoading(true);
    fetch(`${API}/backtest/runs?horizon=${selectedHorizon}&limit=100`)
      .then(r => r.json())
      .then(d => { setHistory(d.runs || []); setLoading(false); });
  }, [selectedHorizon]);

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">▶ BACKTEST</h1>
          <p className="page-subtitle">
            Purged 5-fold cross-validation results from every training run.
            DSR is the Lopez de Prado promotion metric (threshold 0.95).
          </p>
        </div>
      </div>

      {/* Latest run per horizon -- big metric cards */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
        gap: "var(--gap-md)",
        marginBottom: "var(--gap-lg)",
      }}>
        {latest.length === 0 ? (
          <div className="card">
            <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
              No model runs logged yet. Run <code>python run_pipeline.py</code> first.
            </div>
          </div>
        ) : latest.map(r => (
          <div key={r.run_id} className="card">
            <div className="card-header">
              <span className="card-title" style={{ textTransform: "uppercase" }}>
                {r.horizon} ({r.horizon === "intraday" ? "1d" : r.horizon === "swing" ? "5d" : "20d"})
              </span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.65rem", color: "var(--text-muted)" }}>
                {(r.created_at || "").slice(0, 16)}
              </span>
            </div>

            <div style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr 1fr",
              gap: "var(--gap-sm)",
              marginBottom: "var(--gap-md)",
            }}>
              <div className="metric-card">
                <div className="metric-label">IC</div>
                <div className="metric-value" style={{
                  color: (r.ic_mean || 0) > 0 ? "var(--accent-green)" : "var(--accent-red)",
                  fontSize: "1.3rem",
                }}>
                  {fmt(r.ic_mean, 4)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">Sharpe (ann.)</div>
                <div className="metric-value" style={{ fontSize: "1.3rem" }}>
                  {fmt(r.sharpe, 2)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">DSR</div>
                <div className="metric-value" style={{
                  color: dsrColor(r.deflated_sharpe),
                  fontSize: "1.3rem",
                }}>
                  {fmt(r.deflated_sharpe, 4)}
                </div>
              </div>
            </div>

            {/* DSR progress bar */}
            <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)", marginBottom: 4 }}>
              DSR vs promotion threshold (0.95)
            </div>
            <div style={{ position: "relative", height: 8, background: "var(--bg-surface)", borderRadius: 2 }}>
              <div style={{
                position: "absolute",
                width: `${Math.min(100, (r.deflated_sharpe || 0) * 100)}%`,
                height: "100%",
                background: dsrColor(r.deflated_sharpe),
                opacity: 0.7,
                borderRadius: 2,
              }} />
              <div style={{
                position: "absolute",
                left: "95%",
                top: -2,
                width: 1,
                height: 12,
                background: "var(--accent-green)",
              }} />
            </div>

            <div style={{
              marginTop: "var(--gap-md)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.7rem",
              color: "var(--text-muted)",
              display: "grid",
              gridTemplateColumns: "auto 1fr",
              rowGap: 2,
              columnGap: 12,
            }}>
              <span>Model:</span>      <span style={{ color: "var(--text-secondary)" }}>{r.model_type}</span>
              <span>Features:</span>   <span style={{ color: "var(--text-secondary)" }}>{r.n_features}</span>
              <span>Samples:</span>    <span style={{ color: "var(--text-secondary)" }}>{(r.n_samples || 0).toLocaleString()}</span>
              <span>Train:</span>      <span style={{ color: "var(--text-secondary)" }}>{(r.train_start || "").slice(0,10)} ➜ {(r.train_end || "").slice(0,10)}</span>
              <span>Test:</span>       <span style={{ color: "var(--text-secondary)" }}>{(r.test_start || "").slice(0,10)} ➜ {(r.test_end || "").slice(0,10)}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Run history */}
      <div className="card">
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span className="card-title">▸ Run history</span>
          <div style={{ display: "flex", gap: 4 }}>
            {["all", "intraday", "swing", "long"].map(h => (
              <button
                key={h}
                onClick={() => setSelectedHorizon(h)}
                style={{
                  padding: "4px 12px",
                  background: selectedHorizon === h ? "var(--accent-green-glow)" : "transparent",
                  border: selectedHorizon === h ? "1px solid rgba(0,255,136,0.3)" : "1px solid var(--border-color)",
                  borderRadius: "var(--radius-sm)",
                  color: selectedHorizon === h ? "var(--accent-green)" : "var(--text-secondary)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.7rem",
                  cursor: "pointer",
                }}
              >
                {h.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="loading-shimmer" style={{ height: 200 }} />
        ) : history.length === 0 ? (
          <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
            No history.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Created</th>
                  <th>Horizon</th>
                  <th>Model</th>
                  <th>n_features</th>
                  <th>n_samples</th>
                  <th>IC</th>
                  <th>Sharpe</th>
                  <th>DSR</th>
                  <th>CV / Embargo</th>
                </tr>
              </thead>
              <tbody>
                {history.map((r, i) => {
                  const cv = (r.params as any)?.n_cv_splits;
                  const emb = (r.params as any)?.embargo_days;
                  return (
                    <tr key={r.run_id}>
                      <td style={{ color: "var(--text-muted)" }}>{r.run_id}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem" }}>
                        {(r.created_at || "").slice(0, 16)}
                      </td>
                      <td style={{ fontWeight: 600, color: "var(--accent-cyan)" }}>
                        {r.horizon}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-secondary)" }}>
                        {r.model_type}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)" }}>{r.n_features}</td>
                      <td style={{ fontFamily: "var(--font-mono)" }}>
                        {(r.n_samples || 0).toLocaleString()}
                      </td>
                      <td style={{
                        fontFamily: "var(--font-mono)",
                        color: (r.ic_mean || 0) > 0 ? "var(--accent-green)" : "var(--accent-red)",
                      }}>
                        {fmt(r.ic_mean, 4)}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)" }}>{fmt(r.sharpe, 2)}</td>
                      <td style={{
                        fontFamily: "var(--font-mono)",
                        color: dsrColor(r.deflated_sharpe),
                        fontWeight: 600,
                      }}>
                        {fmt(r.deflated_sharpe, 4)}
                      </td>
                      <td style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: "0.7rem",
                        color: "var(--text-muted)",
                      }}>
                        {cv ? `${cv}-fold` : "-"}{emb ? ` / ${emb}d emb.` : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={{
        marginTop: "var(--gap-md)",
        fontFamily: "var(--font-mono)",
        fontSize: "0.7rem",
        color: "var(--text-muted)",
      }}>
        Promotion gate (Bailey &amp; Lopez de Prado, blueprint §15.4): DSR &gt; 0.95. Green = passing, amber = approaching, red = failing.
      </div>
    </div>
  );
}

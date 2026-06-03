"use client";

import { useEffect, useState } from "react";

const API = "http://127.0.0.1:8000/api";

type Row = {
  id: number;
  symbol: string;
  added_at: string;
  source: string;
  notes: string | null;
  company_name: string | null;
  sector: string | null;
  direction: number | null;
  confidence: number | null;
  predicted_return: number | null;
  ranking_score: number | null;
};

function dirLabel(d: number | null): { label: string; color: string } {
  if (d == null)   return { label: "-",    color: "var(--text-muted)"  };
  if (d > 0)       return { label: "BUY",  color: "var(--accent-green)"};
  if (d < 0)       return { label: "SELL", color: "var(--accent-red)"  };
  return            { label: "HOLD", color: "var(--accent-amber)"};
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

export default function WatchlistPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [symbol, setSymbol] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function reload() {
    setLoading(true);
    fetch(`${API}/watchlist`)
      .then(r => r.json())
      .then(d => { setRows(d.watchlist || []); setLoading(false); });
  }

  useEffect(reload, []);

  async function add() {
    const s = symbol.trim().toUpperCase();
    if (!s) return;
    setSaving(true); setErr(null);
    try {
      const res = await fetch(`${API}/watchlist`, {
        method:  "POST",
        headers: { "content-type": "application/json" },
        body:    JSON.stringify({ symbol: s, notes: notes.trim() || null }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${res.status}`);
      }
      setSymbol(""); setNotes("");
      reload();
    } catch (e: any) {
      setErr(e.message || "Failed to add");
    } finally {
      setSaving(false);
    }
  }

  async function remove(sym: string) {
    if (!confirm(`Remove ${sym} from watchlist?`)) return;
    await fetch(`${API}/watchlist/${encodeURIComponent(sym)}`, { method: "DELETE" });
    reload();
  }

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">★ WATCHLIST</h1>
          <p className="page-subtitle">Tracked symbols and their latest swing signal.</p>
        </div>
      </div>

      {/* Add form */}
      <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
        <div style={{ display: "flex", gap: "var(--gap-sm)", alignItems: "center", flexWrap: "wrap" }}>
          <input
            type="text"
            placeholder="Symbol (e.g. RELIANCE)"
            value={symbol}
            onChange={e => setSymbol(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") add(); }}
            style={{
              padding: "6px 10px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.8rem",
              textTransform: "uppercase",
              width: 200,
            }}
          />
          <input
            type="text"
            placeholder="Notes (optional)"
            value={notes}
            onChange={e => setNotes(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") add(); }}
            style={{
              padding: "6px 10px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-sans)",
              fontSize: "0.8rem",
              flex: 1,
              minWidth: 200,
            }}
          />
          <button
            onClick={add}
            disabled={saving || !symbol.trim()}
            style={{
              padding: "6px 18px",
              background: "var(--accent-green-glow)",
              border: "1px solid rgba(0,255,136,0.3)",
              borderRadius: "var(--radius-sm)",
              color: "var(--accent-green)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: saving ? "not-allowed" : "pointer",
              opacity: saving || !symbol.trim() ? 0.5 : 1,
            }}
          >
            {saving ? "Adding..." : "+ Add"}
          </button>
          {err && (
            <span style={{ color: "var(--accent-red)", fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
              {err}
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">▸ Tracked ({rows.length})</span>
        </div>
        {loading ? (
          <div className="loading-shimmer" style={{ height: 200 }} />
        ) : rows.length === 0 ? (
          <div style={{ padding: 30, textAlign: "center", color: "var(--text-muted)" }}>
            No symbols yet. Add one above.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Company</th>
                  <th>Sector</th>
                  <th>Signal (swing)</th>
                  <th>Confidence</th>
                  <th>Pred. Return</th>
                  <th>Notes</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => {
                  const d = dirLabel(r.direction);
                  return (
                    <tr key={r.id}>
                      <td>
                        <a
                          href={`/research/${r.symbol}`}
                          style={{ color: "var(--accent-cyan)", fontWeight: 600, textDecoration: "none" }}
                        >
                          {r.symbol}
                        </a>
                      </td>
                      <td style={{
                        maxWidth: 200,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        color: "var(--text-secondary)",
                        fontSize: "0.75rem",
                      }}>
                        {r.company_name || "-"}
                      </td>
                      <td style={{ color: "var(--text-secondary)", fontSize: "0.7rem" }}>
                        {r.sector || "-"}
                      </td>
                      <td>
                        <span style={{
                          padding: "2px 8px",
                          borderRadius: "var(--radius-sm)",
                          background: `${d.color}22`,
                          color: d.color,
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.7rem",
                          fontWeight: 700,
                        }}>
                          {d.label}
                        </span>
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
                        {fmtPct(r.confidence)}
                      </td>
                      <td style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: "0.75rem",
                        color: (r.predicted_return || 0) > 0 ? "var(--accent-green)" :
                               (r.predicted_return || 0) < 0 ? "var(--accent-red)" :
                               "var(--text-secondary)",
                      }}>
                        {fmtPct(r.predicted_return, 2)}
                      </td>
                      <td style={{ fontSize: "0.75rem", color: "var(--text-secondary)" }}>
                        {r.notes || "-"}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
                        {(r.added_at || "").slice(0, 10)}
                      </td>
                      <td>
                        <button
                          onClick={() => remove(r.symbol)}
                          style={{
                            padding: "3px 10px",
                            background: "transparent",
                            border: "1px solid var(--accent-red)",
                            borderRadius: "var(--radius-sm)",
                            color: "var(--accent-red)",
                            fontFamily: "var(--font-mono)",
                            fontSize: "0.65rem",
                            cursor: "pointer",
                          }}
                        >
                          REMOVE
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

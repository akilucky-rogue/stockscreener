"use client";

/**
 * OrderTicketModal — semi-auto order flow UI (Phase 5 #9 surface).
 *
 * Build a SUGGESTED ticket from the current signal + budget/risk, show the
 * sized plan + confirm_token, then a human clicks "Confirm (dry-run)" to place.
 * Live placement is server-gated (QSDE_ENABLE_LIVE_ORDERS) and intentionally
 * NOT exposed as a one-click here — dry-run is the default, safe path.
 */

import { useState } from "react";

const API = "http://127.0.0.1:8000/api";

const box: React.CSSProperties = {
  position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
  display: "grid", placeItems: "center", zIndex: 50,
};
const mono = "var(--font-mono)";

export default function OrderTicketModal({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [budget, setBudget] = useState(200000);
  const [risk, setRisk] = useState(5000);
  const [ticket, setTicket] = useState<any>(null);
  const [confirm, setConfirm] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function build() {
    setBusy(true); setErr(null); setConfirm(null); setTicket(null);
    try {
      const r = await fetch(`${API}/orders/ticket`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, budget, risk_per_trade: risk }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || JSON.stringify(d));
      setTicket(d);
    } catch (e: any) { setErr(e.message || "build failed"); }
    finally { setBusy(false); }
  }

  async function confirmDryRun() {
    if (!ticket) return;
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`${API}/orders/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticket_id: ticket.ticket_id, confirm_token: ticket.confirm_token }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || JSON.stringify(d));
      setConfirm(d);
    } catch (e: any) { setErr(e.message || "confirm failed"); }
    finally { setBusy(false); }
  }

  const lbl: React.CSSProperties = { fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)", textTransform: "uppercase" };
  const val: React.CSSProperties = { fontFamily: mono, fontSize: "0.95rem", color: "var(--text-primary)", fontWeight: 600 };

  return (
    <div style={box} onClick={onClose}>
      <div className="card" style={{ width: 480, maxWidth: "92vw" }} onClick={e => e.stopPropagation()}>
        <div className="card-header">
          <span className="card-title">⊕ Order ticket — {symbol}</span>
          <button onClick={onClose} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: "1.1rem" }}>✕</button>
        </div>

        <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
          <label style={{ flex: 1 }}>
            <div style={lbl}>Budget (₹)</div>
            <input type="number" value={budget} onChange={e => setBudget(Number(e.target.value))}
              style={{ width: "100%", padding: 8, background: "var(--bg-surface)", border: "1px solid var(--border-color)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: mono }} />
          </label>
          <label style={{ flex: 1 }}>
            <div style={lbl}>Risk / trade (₹)</div>
            <input type="number" value={risk} onChange={e => setRisk(Number(e.target.value))}
              style={{ width: "100%", padding: 8, background: "var(--bg-surface)", border: "1px solid var(--border-color)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: mono }} />
          </label>
        </div>

        <button onClick={build} disabled={busy}
          style={{ width: "100%", padding: "8px 0", background: "var(--accent-cyan)", color: "#001", border: "none", borderRadius: "var(--radius-sm)", fontFamily: mono, fontWeight: 700, cursor: busy ? "not-allowed" : "pointer", opacity: busy ? 0.6 : 1 }}>
          {busy ? "..." : "Build ticket"}
        </button>

        {err && <div style={{ marginTop: 10, color: "var(--accent-red)", fontFamily: mono, fontSize: "0.75rem" }}>{err}</div>}

        {ticket && (
          <div style={{ marginTop: 14, padding: 12, background: "var(--bg-surface)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-color)" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
              <div><div style={lbl}>Side</div><div style={{ ...val, color: ticket.side === "BUY" ? "var(--accent-green)" : "var(--accent-red)" }}>{ticket.side}</div></div>
              <div><div style={lbl}>Qty</div><div style={val}>{ticket.qty}</div></div>
              <div><div style={lbl}>Capital</div><div style={val}>₹{Number(ticket.capital_required).toLocaleString()}</div></div>
              <div><div style={lbl}>Entry</div><div style={val}>₹{ticket.entry_price}</div></div>
              <div><div style={lbl}>Stop</div><div style={{ ...val, color: "var(--accent-red)" }}>₹{ticket.stop_price}</div></div>
              <div><div style={lbl}>Target</div><div style={{ ...val, color: "var(--accent-green)" }}>₹{ticket.target_price}</div></div>
            </div>
            <div style={{ marginTop: 8, ...lbl }}>confirm_token: <span style={{ color: "var(--text-secondary)" }}>{ticket.confirm_token}</span> · status {ticket.status}</div>

            {!confirm && (
              <button onClick={confirmDryRun} disabled={busy}
                style={{ marginTop: 10, width: "100%", padding: "8px 0", background: "var(--accent-amber)", color: "#100", border: "none", borderRadius: "var(--radius-sm)", fontFamily: mono, fontWeight: 700, cursor: busy ? "not-allowed" : "pointer" }}>
                Confirm (dry-run)
              </button>
            )}
            <div style={{ marginTop: 8, fontFamily: mono, fontSize: "0.62rem", color: "var(--text-muted)" }}>
              Dry-run only. Live placement requires QSDE_ENABLE_LIVE_ORDERS on the server + kill-switch off.
            </div>
          </div>
        )}

        {confirm && (
          <div style={{ marginTop: 12, padding: 10, background: confirm.status === "DRYRUN" ? "rgba(0,255,136,0.08)" : "rgba(255,184,0,0.10)", border: "1px solid var(--accent-green)", borderRadius: "var(--radius-sm)", fontFamily: mono, fontSize: "0.75rem", color: "var(--text-primary)" }}>
            <div>status: <b>{confirm.status}</b> · order id: {confirm.broker_order_id || "—"}</div>
            <div style={{ color: "var(--text-muted)", marginTop: 4 }}>
              live_enabled: {String(confirm.live_enabled)} · kill_switch: {String(confirm.kill_switch)}
              {confirm.reasons ? ` · ${(confirm.reasons as string[]).join("; ")}` : ""}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

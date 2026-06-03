"use client";

/**
 * SymbolAnalysisPanel — the "deep" per-symbol view, used by:
 *   - /screener -> Custom tab
 *   - /screener -> any preset row (when expanded)
 *   - /research/[symbol]
 *
 * What it shows for a single ticker:
 *   1. Identity header (symbol, exchange, company, sector + ★ Pin to track)
 *   2. Price strip (latest close + 1W / 1M / 1Y change cards)
 *   3. Chart card with timeframe tabs:
 *        Intraday (live, 1s poll) | 1W | 1M | 6M | 1Y | 5Y
 *      The live tab also overlays anchored-VWAP / POC / sweeps / entry-stop-
 *      target from the intraday signal. Historical tabs render plain candles.
 *   4. Three signal cards (intraday / swing / long) with the new precise
 *      timing fields: action tier (STRONG_BUY/BUY/WATCH_LONG/HOLD/...),
 *      valid_until, exit_by, entry/target/stop + top SHAP factors.
 *   5. Fundamentals snapshot (market cap / PE / margins / etc).
 *   6. Trade button -> OrderTicketModal (semi-auto, dry-run by default).
 *
 * Networking:
 *   - GET /api/analyze/{symbol}                  one-shot, ~5-15s on cold call
 *   - GET /api/analysis/intraday/{symbol}        polled every 1s while live tab is active
 *   - GET /api/analysis/historical/{symbol}?range=...  polled every 60s on historical tabs
 *   - POST /api/analyze/{symbol}/pin             on ★ Pin click
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import LiveChart, { type Bar, type Micro, type Sig, type ChartType } from "./LiveChart";
import OrderTicketModal from "./OrderTicketModal";

const API = "http://127.0.0.1:8000/api";
const LIVE_POLL_MS = 1000;
const HIST_POLL_MS = 60000;
const SPARSE_INTRADAY_THRESHOLD = 10;   // <10 bars -> auto-show 1M historical instead
const mono = "var(--font-mono)";

type Signal = {
  horizon: string;
  predicted_return: number;        // cross-sectional SCORE, not a return (triple-barrier era)
  rank_pct?: number | null;        // percentile [0,1] of this name vs the universe today
  edge?: { net_sharpe?: number; edge_band?: string; caveats?: string[]; tradeable?: boolean } | null;
  direction: number;
  confidence: number;
  action?: string;
  hold_sessions?: number;
  valid_sessions?: number;
  valid_until_label?: string;
  valid_until_date?: string;
  exit_by_date?: string;
  top_factors: Array<{ name: string; contribution: number }>;
  model_version: string;
  entry_price?: number | null;
  target_price?: number | null;
  stop_price?: number | null;
  risk_reward?: number | null;
  atr_pct?: number | null;
  trade_quality?: "good" | "low" | null;
  trade_notes?: string | null;
};

type AnalyzeResponse = {
  input_symbol: string;
  yf_symbol: string;
  internal_symbol: string;
  exchange: string;
  company_name: string | null;
  sector: string | null;
  industry: string | null;
  latest_close: number;
  latest_date: string;
  price_changes: { "1_week": number | null; "1_month": number | null; "1_year": number | null };
  fundamentals: Record<string, number | null>;
  signals: Record<string, Signal | { error: string }>;
  n_ohlcv_rows: number;
  n_factors: number;
  data_source?: string;
};

type ChartPayload = {
  symbol: string;
  count: number;
  bars: Bar[];
  micro: Micro[];
  signal: Sig;
  subscription?: { auth?: boolean; started?: boolean; subscribed?: string[]; note?: string };
  note?: string;
  latest_date?: string;
  days_stale?: number;
};

const TIMEFRAMES = [
  { key: "live", label: "Intraday (live)", api: null },
  { key: "1w",   label: "1W",  api: "historical" },
  { key: "1m",   label: "1M",  api: "historical" },
  { key: "6m",   label: "6M",  api: "historical" },
  { key: "1y",   label: "1Y",  api: "historical" },
  { key: "5y",   label: "5Y",  api: "historical" },
] as const;

type TfKey = typeof TIMEFRAMES[number]["key"];

// ── formatters ────────────────────────────────────────────────────

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}
function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}
function fmtPctRaw(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}%`;
}
function fmtLarge(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(2)}K`;
  return v.toFixed(2);
}

function actionTone(action: string): { color: string; label: string; tone: "strong" | "normal" | "watch" | "hold" } {
  switch (action) {
    case "STRONG_BUY":  return { color: "var(--accent-green)",  label: "STRONG BUY",   tone: "strong" };
    case "BUY":         return { color: "var(--accent-green)",  label: "BUY",          tone: "normal" };
    case "WATCH_LONG":  return { color: "var(--accent-cyan)",   label: "WATCH (long)", tone: "watch" };
    case "STRONG_SELL": return { color: "var(--accent-red)",    label: "STRONG SELL",  tone: "strong" };
    case "SELL":        return { color: "var(--accent-red)",    label: "SELL",         tone: "normal" };
    case "WATCH_SHORT": return { color: "var(--accent-red)",    label: "WATCH (short)", tone: "watch" };
    case "HOLD":
    default:            return { color: "var(--accent-amber)",  label: "HOLD",         tone: "hold"  };
  }
}

function horizonLabel(h: string): string {
  if (h === "intraday") return "INTRADAY (1d)";
  if (h === "swing")    return "SWING (5d)";
  return "LONG (20d)";
}

// ── main component ───────────────────────────────────────────────

export default function SymbolAnalysisPanel({
  symbol,
  onClose,
}: { symbol: string; onClose?: () => void }) {
  const sym = symbol.trim().toUpperCase();

  // /analyze payload (heavy, one-shot per symbol)
  const [analyze, setAnalyze] = useState<AnalyzeResponse | null>(null);
  const [analyzeErr, setAnalyzeErr] = useState<string | null>(null);
  const [analyzeLoading, setAnalyzeLoading] = useState(false);

  // Chart payloads — split so the Live tab can show historical context
  // until enough minute-bars accumulate to draw a meaningful intraday chart.
  const [tf, setTf] = useState<TfKey>("live");
  const [chartType, setChartType] = useState<ChartType>("candles");
  const [chart, setChart] = useState<ChartPayload | null>(null);          // currently-selected tf payload (live or historical)
  const [fallbackHist, setFallbackHist] = useState<ChartPayload | null>(null);  // 1M historical, loaded only while Live tab is sparse
  const [chartErr, setChartErr] = useState<string | null>(null);
  const [chartUpdated, setChartUpdated] = useState<string>("");

  // pin
  const [pinning, setPinning] = useState(false);
  const [pinMsg, setPinMsg] = useState<string | null>(null);

  // order ticket
  const [orderOpen, setOrderOpen] = useState(false);

  // Reset all per-symbol state when symbol prop changes.
  useEffect(() => {
    setAnalyze(null); setAnalyzeErr(null); setAnalyzeLoading(true);
    setChart(null);   setFallbackHist(null);
    setChartErr(null); setChartUpdated("");
    setPinMsg(null);  setOrderOpen(false);
    setTf("live");
  }, [sym]);

  // /analyze: fetch once per symbol (heavy, yfinance behind it).
  useEffect(() => {
    if (!sym) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API}/analyze/${encodeURIComponent(sym)}`);
        const d = await r.json();
        if (cancelled) return;
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        setAnalyze(d);
      } catch (e: unknown) {
        if (!cancelled) setAnalyzeErr(e instanceof Error ? e.message : "analyze failed");
      } finally {
        if (!cancelled) setAnalyzeLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sym]);

  // Chart polling. live tab -> /intraday @ 1s; others -> /historical @ 60s.
  const loadChart = useCallback(async (whichTf: TfKey) => {
    try {
      const url = whichTf === "live"
        ? `${API}/analysis/intraday/${encodeURIComponent(sym)}`
        : `${API}/analysis/historical/${encodeURIComponent(sym)}?range=${whichTf}`;
      const r = await fetch(url);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setChart(d);
      setChartErr(null);
      setChartUpdated(new Date().toLocaleTimeString());
    } catch (e: unknown) {
      setChartErr(e instanceof Error ? e.message : "chart load failed");
    }
  }, [sym]);

  // Standalone fetch for the 1M fallback used when Live is sparse. Doesn't
  // overwrite `chart` — lives in its own slot so the user can still see the
  // live count tick up underneath the banner.
  const loadFallbackHist = useCallback(async () => {
    try {
      const r = await fetch(`${API}/analysis/historical/${encodeURIComponent(sym)}?range=1m`);
      const d = await r.json();
      if (r.ok) setFallbackHist(d);
    } catch {/* silent — fallback is best-effort */}
  }, [sym]);

  useEffect(() => {
    if (!sym) return;
    loadChart(tf);
    const id = setInterval(() => loadChart(tf), tf === "live" ? LIVE_POLL_MS : HIST_POLL_MS);
    // While on the Live tab, always have a 1M historical context loaded so a
    // freshly-subscribed symbol (no intraday bars yet) still shows a chart.
    if (tf === "live") loadFallbackHist();
    return () => clearInterval(id);
  }, [sym, tf, loadChart, loadFallbackHist]);

  // Pin -> persists + re-fetches /analyze to pick up pinned signals.
  const handlePin = useCallback(async () => {
    if (!analyze) return;
    setPinning(true); setPinMsg(null);
    try {
      const r = await fetch(`${API}/analyze/${encodeURIComponent(analyze.input_symbol)}/pin`, { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || JSON.stringify(d));
      const p = d.persistence ?? {};
      setPinMsg(`Pinned: ${(p.ohlcv_rows_written ?? 0).toLocaleString()} OHLCV rows, ${(p.factor_rows_written ?? 0).toLocaleString()} factor rows, ${p.signals_written ?? 0} signal(s). Reload to see across the app.`);
    } catch (e: unknown) {
      setPinMsg(`Pin failed: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setPinning(false);
    }
  }, [analyze]);

  // Currently-displayed signal (drives entry/target/stop overlays on the chart)
  const liveSig: Sig = chart?.signal ?? null;

  // Live-tab fallback: if the live session has too few bars, render the 1M
  // historical context instead so the chart isn't a blank box. The live
  // poll keeps running underneath — once enough bars arrive, we swap back.
  const liveCount = tf === "live" ? (chart?.count ?? 0) : -1;
  const showFallback = tf === "live" && liveCount < SPARSE_INTRADAY_THRESHOLD && (fallbackHist?.bars?.length ?? 0) > 0;
  const displayedBars: Bar[] = showFallback ? (fallbackHist?.bars ?? []) : (chart?.bars ?? []);
  const displayedMicro: Micro[] = showFallback ? [] : (chart?.micro ?? []);
  const displayedSig: Sig = (showFallback || tf !== "live") ? null : liveSig;

  const subBadge = useMemo(() => {
    if (tf !== "live") return null;
    const s = chart?.subscription;
    if (!s) return null;
    if (!s.auth) return { color: "var(--accent-amber)", text: "Kite OFFLINE", login: true };
    if (s.started) return { color: "var(--accent-green)", text: `Live · ${(s.subscribed?.length ?? 0)} symbol(s) subscribed`, login: false };
    return { color: "var(--text-muted)", text: "Kite session ready but not yet streaming", login: false };
  }, [tf, chart]);

  return (
    <div className="fade-in">
      {/* 1. Identity header + Pin */}
      <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <div>
            <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap" }}>
              <span style={{ fontFamily: mono, fontSize: "1.7rem", fontWeight: 700, color: "var(--accent-cyan)" }}>
                {analyze?.internal_symbol ?? sym}
              </span>
              {analyze && (
                <span style={{ padding: "2px 8px", background: "var(--bg-surface)", border: "1px solid var(--border-color)", borderRadius: 3, fontFamily: mono, fontSize: "0.7rem", color: "var(--text-muted)" }}>
                  {analyze.exchange} · {analyze.yf_symbol}
                </span>
              )}
              {analyze?.data_source && (
                <span style={{ padding: "2px 8px", background: "var(--bg-surface)", border: "1px solid var(--border-color)", borderRadius: 3, fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>
                  src: {analyze.data_source}
                </span>
              )}
            </div>
            <div style={{ fontSize: "1rem", color: "var(--text-secondary)", marginTop: 4 }}>
              {analyze?.company_name || (analyzeLoading ? "Loading…" : "(no company name)")}
            </div>
            {analyze && (
              <div style={{ display: "flex", gap: 12, marginTop: 4, fontFamily: mono, fontSize: "0.75rem", color: "var(--text-muted)" }}>
                {analyze.sector && <span>{analyze.sector}</span>}
                {analyze.industry && <span>· {analyze.industry}</span>}
              </div>
            )}
          </div>

          <div style={{ display: "flex", gap: "var(--gap-sm)" }}>
            <button onClick={handlePin} disabled={!analyze || pinning} style={{
              padding: "8px 18px", background: "transparent", border: "1px solid var(--accent-amber)", borderRadius: "var(--radius-sm)",
              color: "var(--accent-amber)", fontFamily: mono, fontSize: "0.8rem", fontWeight: 600,
              cursor: !analyze || pinning ? "not-allowed" : "pointer", opacity: !analyze || pinning ? 0.5 : 1,
            }}>{pinning ? "Pinning..." : "★ Pin to track"}</button>
            {onClose && (
              <button onClick={onClose} style={{
                padding: "8px 14px", background: "transparent", border: "1px solid var(--border-color)", borderRadius: "var(--radius-sm)",
                color: "var(--text-muted)", fontFamily: mono, fontSize: "0.8rem", cursor: "pointer",
              }}>✕ Close</button>
            )}
          </div>
        </div>

        {pinMsg && (
          <div style={{
            marginTop: 10, padding: 8,
            background: pinMsg.startsWith("Pin failed") ? "rgba(255,56,96,0.1)" : "rgba(0,255,136,0.08)",
            border: `1px solid ${pinMsg.startsWith("Pin failed") ? "var(--accent-red)" : "var(--accent-green)"}`,
            borderRadius: "var(--radius-sm)", fontFamily: mono, fontSize: "0.75rem",
            color: pinMsg.startsWith("Pin failed") ? "var(--accent-red)" : "var(--accent-green)",
          }}>{pinMsg}</div>
        )}

        {analyzeErr && (
          <div style={{ marginTop: 10, padding: 8, background: "rgba(255,56,96,0.1)", border: "1px solid var(--accent-red)", borderRadius: "var(--radius-sm)", fontFamily: mono, fontSize: "0.75rem", color: "var(--accent-red)" }}>
            {analyzeErr}
          </div>
        )}

        {/* Price strip */}
        {analyze && (
          <div style={{ marginTop: 16, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
            <div className="metric-card">
              <div className="metric-label">Latest Close</div>
              <div className="metric-value">₹{analyze.latest_close.toLocaleString()}</div>
              <div className="metric-change" style={{ color: "var(--text-muted)" }}>{analyze.latest_date}</div>
            </div>
            {([["1W", "1_week"], ["1M", "1_month"], ["1Y", "1_year"]] as const).map(([lbl, key]) => {
              const v = analyze.price_changes[key];
              return (
                <div key={key} className="metric-card">
                  <div className="metric-label">{lbl} Change</div>
                  <div className="metric-value" style={{
                    color: (v ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)",
                  }}>{fmtPctRaw(v)}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 2. Chart card with timeframe tabs + chart-type toggle */}
      <div className="card" style={{ marginBottom: "var(--gap-md)", borderColor: "rgba(0,229,255,0.25)" }}>
        <div style={{ display: "flex", gap: "var(--gap-sm)", alignItems: "center", flexWrap: "wrap", marginBottom: "var(--gap-sm)" }}>
          {TIMEFRAMES.map((t) => (
            <button key={t.key} onClick={() => setTf(t.key)} style={{
              padding: "6px 12px",
              background: tf === t.key ? "var(--accent-green-glow)" : "var(--bg-card)",
              border: `1px solid ${tf === t.key ? "rgba(0,255,136,0.3)" : "var(--border-color)"}`,
              borderRadius: "var(--radius-sm)",
              color: tf === t.key ? "var(--accent-green)" : "var(--text-secondary)",
              fontFamily: mono, fontSize: "0.75rem", cursor: "pointer",
            }}>{t.label}</button>
          ))}

          {/* Chart-type toggle (Candles / Area / Line) */}
          <div style={{
            display: "flex",
            background: "var(--bg-card)",
            border: "1px solid var(--border-color)",
            borderRadius: "var(--radius-sm)",
            overflow: "hidden",
            marginLeft: "var(--gap-xs)",
          }}>
            {(["candles", "area", "line"] as const).map((t) => {
              const active = chartType === t;
              const icon = t === "candles" ? "▥" : t === "area" ? "◤" : "╱";
              return (
                <button key={t} onClick={() => setChartType(t)} style={{
                  padding: "6px 10px",
                  background: active ? "rgba(0,229,255,0.12)" : "transparent",
                  border: "none",
                  color: active ? "var(--accent-cyan)" : "var(--text-muted)",
                  fontFamily: mono, fontSize: "0.72rem", cursor: "pointer",
                  borderRight: t === "line" ? "none" : "1px solid var(--border-color)",
                }}>{icon} {t[0].toUpperCase() + t.slice(1)}</button>
              );
            })}
          </div>

          <div style={{ flex: 1 }} />
          {subBadge && (
            subBadge.login ? (
              <a
                href={`${API}/kite/login_url`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={async (e) => {
                  // Resolve the login_url JSON and bounce to it. Falls through
                  // to the default href if the fetch fails (offline, etc).
                  try {
                    e.preventDefault();
                    const r = await fetch(`${API}/kite/login_url`);
                    const d = await r.json();
                    if (d.login_url) window.open(d.login_url, "_blank", "noopener,noreferrer");
                  } catch { /* let default href take over */ }
                }}
                style={{
                  fontFamily: mono, fontSize: "0.65rem", color: subBadge.color,
                  textDecoration: "underline", cursor: "pointer",
                }}
              >● {subBadge.text} — click to log in</a>
            ) : (
              <span style={{ fontFamily: mono, fontSize: "0.65rem", color: subBadge.color }}>● {subBadge.text}</span>
            )
          )}
          <span style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>
            {chart ? `${chart.count} bars` : "loading…"}
            {chartUpdated ? ` · refreshed ${chartUpdated}` : ""}
            {tf === "live" ? " · 1s poll" : " · 60s poll"}
          </span>
          <button onClick={() => setOrderOpen(true)} disabled={!liveSig || liveSig.direction === 0} style={{
            padding: "6px 14px", background: "transparent", border: "1px solid var(--accent-cyan)", borderRadius: "var(--radius-sm)",
            color: "var(--accent-cyan)", fontFamily: mono, fontWeight: 700, fontSize: "0.7rem",
            cursor: (!liveSig || liveSig.direction === 0) ? "not-allowed" : "pointer",
            opacity: (!liveSig || liveSig.direction === 0) ? 0.5 : 1,
          }}>⊕ Trade</button>
        </div>

        {chartErr && (
          <div style={{ color: "var(--accent-red)", fontFamily: mono, fontSize: "0.75rem", marginBottom: "var(--gap-sm)" }}>{chartErr}</div>
        )}

        {/* Fallback banner: shown when Live is sparse and we're displaying the 1M context instead. */}
        {showFallback && (
          <div style={{
            marginBottom: 8, padding: "8px 12px",
            background: "rgba(255,184,0,0.08)", border: "1px solid rgba(255,184,0,0.3)",
            borderRadius: "var(--radius-sm)", fontFamily: mono, fontSize: "0.7rem",
            color: "var(--accent-amber)",
          }}>
            ⓘ Live session has only {liveCount} bar{liveCount === 1 ? "" : "s"} so far —
            showing 1M historical context. The live chart will take over once {SPARSE_INTRADAY_THRESHOLD} bars
            have streamed in (auto-swap, no action needed).
          </div>
        )}

        <LiveChart bars={displayedBars} micro={displayedMicro} signal={displayedSig} height={520} chartType={chartType} />
        <div style={{ marginTop: 6, fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>
          {showFallback
            ? `░ daily candles · 1M context (live session warming up · ${liveCount} bar${liveCount === 1 ? "" : "s"})`
            : tf === "live"
              ? "░ 1m candles · anchored VWAP + bands · POC/VAH/VAL · ▲▼ sweeps · entry/target/stop overlays"
              : `░ daily candles · ${tf.toUpperCase()} range${chart?.latest_date ? ` · through ${chart.latest_date}` : ""}`}
        </div>
        {chart?.note && (
          <div style={{ marginTop: 6, fontFamily: mono, fontSize: "0.7rem", color: "var(--accent-amber)" }}>
            {chart.note}
          </div>
        )}
      </div>

      {/* 3. Signal cards — three horizons */}
      {analyze && (
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))",
          gap: "var(--gap-md)",
          marginBottom: "var(--gap-md)",
        }}>
          {Object.entries(analyze.signals).map(([hzn, s]) => {
            if ("error" in s) {
              return (
                <div key={hzn} className="card">
                  <div className="card-header"><span className="card-title">{horizonLabel(hzn)}</span></div>
                  <div style={{ padding: 16, color: "var(--text-muted)", fontFamily: mono, fontSize: "0.75rem" }}>{s.error}</div>
                </div>
              );
            }
            const sig = s as Signal;
            const tone = actionTone(sig.action || "HOLD");
            return (
              <div key={hzn} className="card">
                <div className="card-header">
                  <span className="card-title">{horizonLabel(hzn)}</span>
                  <span style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>{sig.model_version}</span>
                </div>

                {/* Action chip + predicted return + confidence */}
                <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 12, flexWrap: "wrap" }}>
                  <div style={{
                    padding: "10px 18px",
                    background: `${tone.color}22`,
                    border: `${tone.tone === "strong" ? "2px" : "1px"} solid ${tone.color}`,
                    borderRadius: "var(--radius-md)",
                    color: tone.color,
                    fontFamily: mono,
                    fontSize: "1.05rem",
                    fontWeight: 700,
                    letterSpacing: 0.4,
                  }}>{tone.label}</div>
                  <div title="Cross-sectional model rank vs the universe today. The model emits a relative score (not a return); direction comes from this rank.">
                    <div style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>Model rank</div>
                    <div style={{ fontFamily: mono, fontSize: "1.1rem", fontWeight: 700, color: "var(--accent-cyan)" }}>
                      {sig.rank_pct == null
                        ? "—"
                        : sig.rank_pct >= 0.5
                          ? `Top ${Math.max(0.1, (1 - sig.rank_pct) * 100).toFixed(sig.rank_pct > 0.99 ? 1 : 0)}%`
                          : `Bottom ${Math.max(0.1, sig.rank_pct * 100).toFixed(sig.rank_pct < 0.01 ? 1 : 0)}%`}
                    </div>
                  </div>
                  <div style={{ flex: 1, minWidth: 140 }}
                       title={(sig.edge?.caveats || []).join("  •  ") || "Validated net-of-cost Sharpe for this horizon (liquid names, realistic friction)."}>
                    <div style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>Validated edge</div>
                    <div style={{ fontFamily: mono, fontSize: "0.95rem", fontWeight: 600, color: "var(--accent-amber)" }}>
                      {sig.edge?.net_sharpe != null
                        ? `≈ ${sig.edge.net_sharpe.toFixed(2)} net Sharpe${sig.edge.edge_band ? ` (${sig.edge.edge_band})` : ""}`
                        : "—"}
                    </div>
                    <div style={{ fontFamily: mono, fontSize: "0.58rem", color: "var(--text-muted)", marginTop: 2 }}>
                      liquid names · net of costs
                    </div>
                  </div>
                </div>

                {/* Precise timing strip */}
                {(sig.valid_until_label || sig.exit_by_date) && (
                  <div style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr 1fr",
                    gap: "var(--gap-sm)",
                    marginBottom: "var(--gap-sm)",
                    padding: "6px 10px",
                    background: "var(--bg-surface)",
                    borderRadius: "var(--radius-sm)",
                  }}>
                    <div>
                      <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>VALID UNTIL</div>
                      <div style={{ fontFamily: mono, fontSize: "0.78rem", color: "var(--accent-cyan)" }}>{sig.valid_until_label ?? "—"}</div>
                    </div>
                    <div>
                      <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>HOLD</div>
                      <div style={{ fontFamily: mono, fontSize: "0.78rem", color: "var(--text-primary)" }}>
                        ~{sig.hold_sessions ?? "—"} session{(sig.hold_sessions ?? 0) === 1 ? "" : "s"}
                      </div>
                    </div>
                    <div>
                      <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>EXIT BY</div>
                      <div style={{ fontFamily: mono, fontSize: "0.78rem", color: "var(--text-primary)" }}>{sig.exit_by_date ?? "—"}</div>
                    </div>
                  </div>
                )}

                {/* Trade plan: entry / target / stop / RR */}
                {sig.entry_price != null && (
                  <div style={{
                    marginBottom: "var(--gap-md)",
                    padding: "var(--gap-sm)",
                    background: "var(--bg-surface)",
                    borderRadius: "var(--radius-sm)",
                    border: sig.direction === 0
                      ? "1px dashed var(--border-color)"
                      : `1px solid ${sig.direction > 0 ? "rgba(0,255,136,0.25)" : "rgba(255,51,102,0.25)"}`,
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                      <div style={{ fontFamily: mono, fontSize: "0.7rem", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.5 }}>
                        {sig.direction === 0 ? "No trade — HOLD" : "Trade plan"}
                      </div>
                      {sig.risk_reward != null && (
                        <div style={{
                          fontFamily: mono, fontSize: "0.7rem", padding: "2px 8px", borderRadius: 10,
                          background: sig.trade_quality === "good" ? "rgba(0,255,136,0.10)" : "rgba(255,184,0,0.10)",
                          color: sig.trade_quality === "good" ? "var(--accent-green)" : "var(--accent-amber)",
                        }}>R:R {sig.risk_reward.toFixed(2)}{sig.trade_quality === "low" ? "  (low quality)" : ""}</div>
                      )}
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--gap-sm)" }}>
                      <div>
                        <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>ENTRY</div>
                        <div style={{ fontFamily: mono, fontSize: "0.9rem", color: "var(--text-primary)", fontWeight: 600 }}>
                          ₹{(sig.entry_price ?? 0).toFixed(2)}
                        </div>
                      </div>
                      <div>
                        <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>TARGET</div>
                        <div style={{ fontFamily: mono, fontSize: "0.9rem", color: sig.direction === 0 ? "var(--text-muted)" : "var(--accent-green)", fontWeight: 600 }}>
                          {sig.target_price != null ? `₹${sig.target_price.toFixed(2)}` : "—"}
                        </div>
                      </div>
                      <div>
                        <div style={{ fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>STOP</div>
                        <div style={{ fontFamily: mono, fontSize: "0.9rem", color: sig.direction === 0 ? "var(--text-muted)" : "var(--accent-red)", fontWeight: 600 }}>
                          {sig.stop_price != null ? `₹${sig.stop_price.toFixed(2)}` : "—"}
                        </div>
                      </div>
                    </div>
                    {sig.atr_pct != null && (
                      <div style={{ marginTop: 6, fontFamily: mono, fontSize: "0.6rem", color: "var(--text-muted)" }}>
                        ATR {(sig.atr_pct * 100).toFixed(2)}%{sig.trade_notes ? ` · ${sig.trade_notes}` : ""}
                      </div>
                    )}
                  </div>
                )}

                {/* Top SHAP factors */}
                <div>
                  <div style={{ fontFamily: mono, fontSize: "0.7rem", color: "var(--text-muted)", marginBottom: 6 }}>
                    Top contributing factors
                  </div>
                  {sig.top_factors.map((f, i) => {
                    const pos = f.contribution > 0;
                    const max = Math.max(...sig.top_factors.map((x) => Math.abs(x.contribution)));
                    const w = max > 0 ? (Math.abs(f.contribution) / max) * 50 : 0;
                    return (
                      <div key={i} style={{ display: "grid", gridTemplateColumns: "180px 1fr 70px", alignItems: "center", gap: 8, marginBottom: 3 }}>
                        <div style={{
                          fontFamily: mono, fontSize: "0.65rem",
                          color: f.name.startsWith("fund_") ? "var(--accent-amber)" :
                                 f.name.startsWith("flow_") ? "var(--accent-green)" :
                                 "var(--accent-cyan)",
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        }}>{f.name}</div>
                        <div style={{ position: "relative", height: 7, background: "var(--bg-surface)", borderRadius: 2 }}>
                          <div style={{ position: "absolute", left: "50%", top: 0, width: 1, height: "100%", background: "var(--border-color)" }} />
                          <div style={{
                            position: "absolute",
                            left: pos ? "50%" : `${50 - w}%`,
                            width: `${w}%`,
                            top: 0, height: "100%",
                            background: pos ? "var(--accent-green)" : "var(--accent-red)",
                            opacity: 0.7, borderRadius: 2,
                          }} />
                        </div>
                        <div style={{ textAlign: "right", fontFamily: mono, fontSize: "0.65rem", color: pos ? "var(--accent-green)" : "var(--accent-red)" }}>
                          {(f.contribution * 100).toFixed(2)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 4. Fundamentals */}
      {analyze && (
        <div className="card">
          <div className="card-header">
            <span className="card-title">▸ Fundamentals snapshot</span>
            <span style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)" }}>
              {analyze.n_ohlcv_rows} OHLCV rows · {analyze.n_factors} factors
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
            {([
              ["Market Cap",      fmtLarge(analyze.fundamentals.market_cap)],
              ["Enterprise Value", fmtLarge(analyze.fundamentals.enterprise_value)],
              ["Trailing P/E",     fmt(analyze.fundamentals.trailing_pe, 2)],
              ["P/B",              fmt(analyze.fundamentals.price_to_book, 2)],
              ["EV/EBITDA",        fmt(analyze.fundamentals.ev_to_ebitda, 2)],
              ["ROE",              fmtPct(analyze.fundamentals.roe)],
              ["Op Margin",        fmtPct(analyze.fundamentals.operating_margin)],
              ["Net Margin",       fmtPct(analyze.fundamentals.net_margin)],
              ["Debt/Equity",      fmt(analyze.fundamentals.debt_to_equity, 2)],
              ["Div Yield",        fmtPct(analyze.fundamentals.dividend_yield)],
              ["Revenue Growth",   fmtPct(analyze.fundamentals.revenue_growth_yoy)],
              ["Earnings Growth",  fmtPct(analyze.fundamentals.earnings_growth)],
              ["Revenue",          fmtLarge(analyze.fundamentals.total_revenue)],
              ["Free Cash Flow",   fmtLarge(analyze.fundamentals.free_cashflow)],
            ] as const).map(([label, val]) => (
              <div key={label} style={{ padding: 10, background: "var(--bg-surface)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-color)" }}>
                <div style={{ fontFamily: mono, fontSize: "0.65rem", color: "var(--text-muted)", textTransform: "uppercase" }}>{label}</div>
                <div style={{ fontFamily: mono, fontSize: "0.9rem", color: "var(--text-primary)", marginTop: 2 }}>{val}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {orderOpen && analyze && (
        <OrderTicketModal symbol={analyze.internal_symbol} onClose={() => setOrderOpen(false)} />
      )}
    </div>
  );
}

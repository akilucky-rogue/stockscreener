"use client";

/**
 * /paper/[id]/live — Live trade tracker.
 *
 * What it shows: one paper trade, stared at hard.
 *   - Candles since entry (daily for swing/long, intraday for intraday).
 *   - Horizontal lines at entry (gray), target (green), stop (red).
 *   - NIFTY 50 EQ benchmark as a line below the candles.
 *   - Side panel: what the model expected vs what's actually happening,
 *     plus MFE/MAE, time elapsed/remaining, delta vs benchmark.
 *
 * Why it matters: the model said this trade had a +X% expected return with
 * Y confidence. Reality is whatever the candles show. The job of this page
 * is to make that gap impossible to look away from.
 *
 * Requires: `npm install lightweight-charts` in the frontend folder.
 */

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";

const API = "http://127.0.0.1:8000/api";

// ──────────────────────────────────────────────────────────────────────
// API response shapes (mirror backend qsde/execution/paper_live.py)
// ──────────────────────────────────────────────────────────────────────

interface TradeRow {
  id: number;
  symbol: string;
  strategy: string;
  horizon: string;
  taken_at: string | null;
  entry_date: string;
  entry_price: number;
  direction: number;
  target_price: number | null;
  stop_price: number | null;
  horizon_sessions: number;
  cost_bps: number;
  status: "OPEN" | "WIN" | "LOSS" | "TIME";
  exit_date: string | null;
  exit_price: number | null;
  realized_ret: number | null;
  realized_ret_net: number | null;
  notes: string | null;
}

interface Candle {
  time: number | string;
  open: number; high: number; low: number; close: number;
  volume: number;
}

interface LinePoint { time: number | string; value: number; }

interface Stats {
  current_price: number | null;
  current_pnl_pct: number | null;
  current_pnl_bps: number | null;
  mfe: number | null;
  mae: number | null;
  benchmark_ret: number | null;
  delta_vs_benchmark: number | null;
  sessions_elapsed: number;
  sessions_remaining: number;
}

interface Expected {
  predicted_return: number | null;
  confidence: number | null;
  ranking_score: number | null;
  atr_pct: number | null;
  top_factors: any;
}

interface LivePayload {
  trade: TradeRow;
  stock_candles: Candle[];
  benchmark: { name: string; points: LinePoint[] };
  stats: Stats;
  expected: Expected;
}

// ──────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────

const fmtPct = (v: number | null, digits = 2) =>
  v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";

const fmtPrice = (v: number | null, digits = 2) =>
  v == null || !isFinite(v) ? "—" : v.toFixed(digits);

const statusColor = (s: string) => ({
  OPEN: "var(--accent-amber)",
  WIN:  "var(--accent-green)",
  LOSS: "var(--accent-red)",
  TIME: "var(--text-secondary)",
} as Record<string, string>)[s] || "var(--text-muted)";

// ──────────────────────────────────────────────────────────────────────
// Page
// ──────────────────────────────────────────────────────────────────────

export default function PaperLivePage() {
  const params = useParams();
  const router = useRouter();
  const tradeId = Number(params?.id);

  const [data, setData] = useState<LivePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chartContainerRef = useRef<HTMLDivElement | null>(null);
  const benchContainerRef = useRef<HTMLDivElement | null>(null);

  // Fetch live payload + auto-refresh every 30s for OPEN trades.
  useEffect(() => {
    if (!Number.isFinite(tradeId)) {
      setError("invalid trade id");
      return;
    }
    let cancelled = false;
    const fetchOnce = () => {
      fetch(`${API}/paper/${tradeId}/live`)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .then((d: LivePayload) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    };
    fetchOnce();
    const interval = setInterval(fetchOnce, 30_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [tradeId]);

  // Render the candle chart whenever data changes. lightweight-charts is
  // loaded lazily so the page can SSR; chart only mounts client-side.
  useEffect(() => {
    if (!data || !chartContainerRef.current) return;
    let chart: any = null;
    let benchChart: any = null;
    let resizeObserver: ResizeObserver | null = null;

    (async () => {
      const lwc = await import("lightweight-charts");
      const container = chartContainerRef.current;
      if (!container) return;

      chart = lwc.createChart(container, {
        width: container.clientWidth,
        height: 380,
        layout: {
          background: { color: "transparent" },
          textColor: "rgba(220,220,220,0.85)",
          fontFamily: "var(--font-mono)",
        },
        grid: {
          vertLines: { color: "rgba(255,255,255,0.04)" },
          horzLines: { color: "rgba(255,255,255,0.04)" },
        },
        rightPriceScale: { borderColor: "rgba(255,255,255,0.15)" },
        timeScale:       { borderColor: "rgba(255,255,255,0.15)" },
      });
      const candleSeries = chart.addCandlestickSeries({
        upColor: "#00ff88",
        downColor: "#ff3860",
        wickUpColor: "#00ff88",
        wickDownColor: "#ff3860",
        borderVisible: false,
      });
      candleSeries.setData(data.stock_candles as any);

      // Horizontal price lines for entry, target, stop.
      const lineCfgs: { price: number | null; color: string; title: string }[] = [
        { price: data.trade.entry_price,  color: "#9aa0a6", title: "Entry"  },
        { price: data.trade.target_price, color: "#00e676", title: "Target" },
        { price: data.trade.stop_price,   color: "#ff3860", title: "Stop"   },
      ];
      lineCfgs.forEach((cfg) => {
        if (cfg.price != null && isFinite(cfg.price)) {
          candleSeries.createPriceLine({
            price: cfg.price,
            color: cfg.color,
            lineWidth: 1,
            lineStyle: 0,         // Solid
            axisLabelVisible: true,
            title: cfg.title,
          });
        }
      });

      // Benchmark line below.
      if (benchContainerRef.current && data.benchmark.points.length > 0) {
        benchChart = lwc.createChart(benchContainerRef.current, {
          width: benchContainerRef.current.clientWidth,
          height: 140,
          layout: {
            background: { color: "transparent" },
            textColor: "rgba(180,180,180,0.75)",
            fontFamily: "var(--font-mono)",
          },
          grid: {
            vertLines: { color: "rgba(255,255,255,0.03)" },
            horzLines: { color: "rgba(255,255,255,0.03)" },
          },
          rightPriceScale: { borderColor: "rgba(255,255,255,0.12)" },
          timeScale:       { borderColor: "rgba(255,255,255,0.12)" },
        });
        const benchSeries = benchChart.addLineSeries({
          color: "#00e5ff",
          lineWidth: 2,
        });
        benchSeries.setData(data.benchmark.points as any);
        chart.timeScale().fitContent();
        benchChart.timeScale().fitContent();
      } else {
        chart.timeScale().fitContent();
      }

      // Resize on container changes (sidebar collapse, window resize).
      resizeObserver = new ResizeObserver(() => {
        if (chart && container) chart.applyOptions({ width: container.clientWidth });
        if (benchChart && benchContainerRef.current) {
          benchChart.applyOptions({ width: benchContainerRef.current.clientWidth });
        }
      });
      resizeObserver.observe(container);
      if (benchContainerRef.current) resizeObserver.observe(benchContainerRef.current);
    })();

    return () => {
      if (resizeObserver) resizeObserver.disconnect();
      if (chart) chart.remove();
      if (benchChart) benchChart.remove();
    };
  }, [data]);

  if (error) {
    return (
      <div className="fade-in">
        <div style={{ padding: 20, color: "var(--accent-red)" }}>
          Error loading trade {tradeId}: {error}
        </div>
        <button onClick={() => router.push("/paper")} style={btnStyle}>← Paper Journal</button>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="fade-in" style={{ padding: 20, color: "var(--text-muted)" }}>
        Loading trade {tradeId}…
      </div>
    );
  }

  const { trade, stats, expected, benchmark } = data;
  const dirLabel = trade.direction > 0 ? "LONG" : "SHORT";

  return (
    <div className="fade-in">
      {/* Header */}
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>
            ◆ {trade.symbol} <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>·</span>{" "}
            <span style={{ fontSize: "0.95rem", color: trade.direction > 0 ? "var(--accent-green)" : "var(--accent-red)" }}>
              {dirLabel}
            </span>{" "}
            <span style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>
              · {trade.strategy} · {trade.horizon}
            </span>
          </h1>
          <div style={{ fontSize: "0.78rem", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            Trade #{trade.id} · entered {trade.entry_date} @ ₹{fmtPrice(trade.entry_price)}
            {" · "}
            <span style={{ color: statusColor(trade.status), fontWeight: 600 }}>{trade.status}</span>
            {trade.exit_date && ` · exited ${trade.exit_date} @ ₹${fmtPrice(trade.exit_price)}`}
          </div>
        </div>
        <button onClick={() => router.push("/paper")} style={btnStyle}>← Paper Journal</button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: "var(--gap-lg)", alignItems: "start" }}>
        {/* Charts column */}
        <div>
          <div className="card" style={{ padding: 12 }}>
            <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)", marginBottom: 6,
                          fontFamily: "var(--font-mono)" }}>
              {trade.symbol} · entry/target/stop overlaid · {trade.horizon === "intraday" ? "5-min" : "daily"} bars
            </div>
            <div ref={chartContainerRef} style={{ width: "100%", height: 380 }} />
          </div>
          {benchmark.points.length > 0 && (
            <div className="card" style={{ padding: 12, marginTop: "var(--gap-md)" }}>
              <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)", marginBottom: 6,
                            fontFamily: "var(--font-mono)" }}>
                Benchmark · {benchmark.name} (equal-weighted NIFTY 50)
              </div>
              <div ref={benchContainerRef} style={{ width: "100%", height: 140 }} />
            </div>
          )}
        </div>

        {/* Side panel — Expected vs Actual */}
        <div className="card" style={{ padding: 16, fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
          <div style={{ fontSize: "0.85rem", color: "var(--accent-cyan)", marginBottom: 10,
                        letterSpacing: 0.5, textTransform: "uppercase" }}>
            Expected vs Actual
          </div>

          <SectionLabel>Trade Plan</SectionLabel>
          <KV k="Entry"  v={`₹${fmtPrice(trade.entry_price)}`} />
          <KV k="Target" v={`₹${fmtPrice(trade.target_price)}`} color="var(--accent-green)" />
          <KV k="Stop"   v={`₹${fmtPrice(trade.stop_price)}`} color="var(--accent-red)" />
          <KV k="Horizon" v={`${trade.horizon_sessions} sessions`} />
          <KV k="Cost" v={`${trade.cost_bps?.toFixed(0)} bps`} />

          <SectionLabel>Model Expected</SectionLabel>
          <KV k="Predicted ret"  v={fmtPct(expected.predicted_return)}
              color={(expected.predicted_return ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)"} />
          <KV k="Confidence"     v={expected.confidence == null ? "—" : (expected.confidence * 100).toFixed(0) + "%"} />
          <KV k="Ranking score"  v={expected.ranking_score == null ? "—" : expected.ranking_score.toFixed(4)} />
          <KV k="ATR%"           v={expected.atr_pct == null ? "—" : (expected.atr_pct).toFixed(3)} />

          <SectionLabel>Actual So Far</SectionLabel>
          <KV k="Current price" v={`₹${fmtPrice(stats.current_price)}`} />
          <KV k="P&L"     v={fmtPct(stats.current_pnl_pct)}
              color={(stats.current_pnl_pct ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)"} />
          <KV k="MFE (max favorable)" v={fmtPct(stats.mfe)} color="var(--accent-green)" />
          <KV k="MAE (max adverse)"   v={fmtPct(stats.mae)} color="var(--accent-red)" />

          <SectionLabel>Vs Benchmark</SectionLabel>
          <KV k="Benchmark ret"   v={fmtPct(stats.benchmark_ret)} />
          <KV k="Δ vs benchmark"  v={fmtPct(stats.delta_vs_benchmark)}
              color={(stats.delta_vs_benchmark ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)"} />

          <SectionLabel>Time</SectionLabel>
          <KV k="Elapsed"   v={`${stats.sessions_elapsed} sess.`} />
          <KV k="Remaining" v={`${stats.sessions_remaining} sess.`} />

          {trade.status !== "OPEN" && (
            <>
              <SectionLabel>Resolution</SectionLabel>
              <KV k="Exit"  v={`₹${fmtPrice(trade.exit_price)}`} />
              <KV k="Realized (net)" v={fmtPct(trade.realized_ret_net)}
                  color={(trade.realized_ret_net ?? 0) > 0 ? "var(--accent-green)" : "var(--accent-red)"} />
            </>
          )}

          <div style={{ marginTop: 14, fontSize: "0.7rem", color: "var(--text-muted)" }}>
            Auto-refreshes every 30s.
          </div>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Tiny presentational helpers
// ──────────────────────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      marginTop: 12, marginBottom: 4,
      fontSize: "0.65rem", color: "var(--text-muted)",
      textTransform: "uppercase", letterSpacing: 0.5,
      borderTop: "1px dashed var(--border-color)", paddingTop: 8,
    }}>{children}</div>
  );
}

function KV({ k, v, color }: { k: string; v: string; color?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "2px 0" }}>
      <span style={{ color: "var(--text-secondary)" }}>{k}</span>
      <span style={{ color: color || "var(--text-bright)", fontWeight: 600 }}>{v}</span>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  padding: "8px 14px",
  background: "transparent",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-sm)",
  color: "var(--accent-cyan)",
  fontFamily: "var(--font-mono)",
  fontSize: "0.78rem",
  cursor: "pointer",
};

"use client";

/**
 * LiveChart — dependency-free SVG candlestick chart.
 *
 * Pure presentational component (no chart lib, no d3 runtime) so it stays
 * stable across React 19 / Next 16. The parent owns fetching and passes
 * data in as props.
 *
 * Renders:
 *   1. Candlesticks (price panel, ~75% of height)
 *   2. Volume bars (volume panel below, ~20% of height)
 *   3. X-axis time labels (intraday: HH:MM IST, historical: DD MMM / MMM YY)
 *   4. Y-axis price grid + last-price tag on the right
 *   5. SMA(20) line on closes — turns off when n < 20
 *   6. Anchored-VWAP line + ±k·σ band (live mode)
 *   7. Session POC / VAH / VAL horizontal lines (live mode)
 *   8. Liquidity-sweep markers ▲▼ (live mode)
 *   9. Entry / Target / Stop dashed levels from the current signal
 *  10. Hover crosshair + tooltip card showing date · O/H/L/C · Δ% · V
 *
 * Bars and micro are 1:1 aligned by ts (same session frame).
 */

import { useMemo, useRef, useState } from "react";

export type Bar = {
  ts: string;
  open: number | null; high: number | null; low: number | null;
  close: number | null; volume: number | null;
};
export type Micro = {
  ts: string;
  avwap: number | null; avwap_upper: number | null; avwap_lower: number | null;
  ofi: number | null; vp_poc: number | null; vp_vah: number | null; vp_val: number | null;
  sweep_high: number; sweep_low: number;
};
export type Sig = {
  symbol: string; action: string; direction: number;
  entry: number | null; stop: number | null; target: number | null;
  risk_reward: number | null; bias: number; confidence: number; reasons: string[];
} | null;

const GREEN = "var(--accent-green)";
const RED = "var(--accent-red)";
const CYAN = "var(--accent-cyan)";
const AMBER = "var(--accent-amber)";
const MUTED = "var(--text-muted)";
const TEXT_PRIMARY = "var(--text-primary)";
const GRID = "var(--border-color)";

// ── time / number formatting ─────────────────────────────────

/**
 * The chart receives bar timestamps as ISO strings. Format depends on
 * granularity:
 *   * 1-min bars within today  ("2026-05-29T15:21:00+05:30")  -> "15:21"
 *   * Hourly bars across days  ("2026-05-26T10:00:00+05:30")  -> "26 May"
 *   * Daily bars               ("2026-05-20")                  -> "20 May 26"
 *
 * `multiDayIntraday` is set by the parent based on whether the first and
 * last bar share a calendar date.
 */
function isIntradayTs(ts: string): boolean {
  return ts.length > 10 && (ts.includes("T") || ts.includes(":"));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtTick(ts: string, intraday: boolean, multiDayIntraday: boolean): string {
  if (!ts) return "";
  try {
    if (intraday) {
      const d = new Date(ts);
      if (multiDayIntraday) {
        // Hourly across multiple days: "26 May" is enough on a tick label.
        return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
      }
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return `${hh}:${mm}`;
    }
    const [y, m, dd] = ts.split("T")[0].split("-");
    return `${parseInt(dd, 10)} ${MONTHS[parseInt(m, 10) - 1]} ${y.slice(2)}`;
  } catch {
    return ts.slice(0, 10);
  }
}

function fmtTooltipTs(ts: string, intraday: boolean): string {
  try {
    if (intraday) {
      const d = new Date(ts);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const date = d.toISOString().slice(0, 10);
      return `${date} ${hh}:${mm}`;
    }
    return ts.slice(0, 10);
  } catch {
    return ts;
  }
}

function fmtVol(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return `${v.toFixed(0)}`;
}

/** Pick ~targetCount evenly-spaced indices in [0, n-1] for x-axis ticks. */
function tickIndices(n: number, targetCount: number): number[] {
  if (n <= 1) return [0];
  const k = Math.min(n, Math.max(2, targetCount));
  const step = (n - 1) / (k - 1);
  const out: number[] = [];
  for (let i = 0; i < k; i++) out.push(Math.round(i * step));
  return Array.from(new Set(out));
}

/** Rolling SMA over `close`, NaN for the warmup window. */
function rollingSMA(closes: (number | null)[], window: number): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < window) return out;
  let sum = 0;
  let n = 0;
  for (let i = 0; i < closes.length; i++) {
    const c = closes[i];
    if (c != null) { sum += c; n++; }
    if (i >= window) {
      const drop = closes[i - window];
      if (drop != null) { sum -= drop; n--; }
    }
    if (i >= window - 1 && n === window) out[i] = sum / window;
  }
  return out;
}

// ── component ────────────────────────────────────────────────

export type ChartType = "candles" | "area" | "line";

export default function LiveChart({
  bars, micro, signal, height = 480, chartType = "candles",
}: {
  bars: Bar[];
  micro: Micro[];
  signal: Sig;
  height?: number;
  /** Visual style of the price series. Default candles. Area = Yahoo-style
   *  mountain with cyan gradient fill. Line = thin polyline only. */
  chartType?: ChartType;
}) {
  const W = 1000, H = height;
  // Right-side price axis (industry standard). mR widens to fit the
  // grid labels + the level labels (POC/Entry/Target/Stop) that already
  // hang off the right edge.
  const mL = 8, mR = 56, mT = 16, mB = 38;
  const plotW = W - mL - mR;
  const totalPanelH = H - mT - mB;
  const priceH = Math.round(totalPanelH * 0.74);
  const gapH = 6;
  const volTop = mT + priceH + gapH;
  const volH = totalPanelH - priceH - gapH;
  const xAxisY = mT + totalPanelH + 2;
  const priceBottomY = mT + priceH;   // bottom of price panel (for area fill)

  const [hover, setHover] = useState<{ i: number; mx: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const intraday = useMemo(() => {
    if (bars.length === 0) return true;
    return isIntradayTs(bars[0].ts);
  }, [bars]);

  // "Multi-day intraday" = hourly bars spanning more than one calendar day.
  // Used to switch x-axis labels from HH:MM to DD MMM so 1W/1M tabs (which
  // now use 1h yfinance bars) don't render the same time over and over.
  const multiDayIntraday = useMemo(() => {
    if (!intraday || bars.length < 2) return false;
    const first = bars[0].ts.slice(0, 10);
    const last  = bars[bars.length - 1].ts.slice(0, 10);
    return first !== last;
  }, [bars, intraday]);

  const v = useMemo(() => {
    const n = bars.length;
    if (n === 0) return null;

    // Price extent: candle range + any overlay levels.
    let lo = Infinity, hi = -Infinity;
    for (const b of bars) {
      if (b.low != null) lo = Math.min(lo, b.low);
      if (b.high != null) hi = Math.max(hi, b.high);
    }
    const last = micro.length ? micro[micro.length - 1] : null;
    const extras: number[] = [];
    if (last) [last.avwap_upper, last.avwap_lower, last.vp_vah, last.vp_val].forEach(x => x != null && extras.push(x));
    if (signal) [signal.entry, signal.stop, signal.target].forEach(x => x != null && extras.push(x));
    for (const x of extras) { lo = Math.min(lo, x); hi = Math.max(hi, x); }
    if (!isFinite(lo) || !isFinite(hi) || hi <= lo) { lo = (lo || 1) * 0.99; hi = (hi || 1) * 1.01 + 1; }
    const pad = (hi - lo) * 0.06;
    lo -= pad; hi += pad;

    // Volume extent
    let vmax = 0;
    for (const b of bars) {
      if (b.volume != null && b.volume > vmax) vmax = b.volume;
    }
    if (vmax === 0) vmax = 1;

    const xs = (i: number) => mL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const xFromMouse = (mx: number) => Math.max(0, Math.min(n - 1, Math.round((mx - mL) / plotW * (n - 1))));
    const ys = (p: number) => mT + (1 - (p - lo) / (hi - lo)) * priceH;
    const yv = (vol: number) => volTop + volH - (vol / vmax) * volH;
    const cw = Math.max(1.4, (plotW / n) * 0.65);

    const poly = (get: (m: Micro) => number | null): string =>
      micro
        .map((m, i) => ({ i, val: get(m) }))
        .filter(o => o.val != null)
        .map(o => `${xs(o.i).toFixed(1)},${ys(o.val as number).toFixed(1)}`)
        .join(" ");

    const closes = bars.map(b => b.close);
    const sma20 = rollingSMA(closes, 20);

    return { n, lo, hi, vmax, xs, xFromMouse, ys, yv, cw, poly, last, sma20 };
  }, [bars, micro, signal, plotW, priceH, volTop, volH]);

  if (!v) {
    return (
      <div style={{ height, display: "grid", placeItems: "center", color: MUTED, fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
        No bars to chart.
      </div>
    );
  }
  const { n, lo, hi, vmax, xs, xFromMouse, ys, yv, cw, poly, last, sma20 } = v;
  const priceGrid = Array.from({ length: 5 }, (_, k) => lo + ((hi - lo) * k) / 4);
  const volGrid = [0, vmax / 2, vmax];
  const xTicks = tickIndices(n, Math.min(8, Math.max(3, Math.floor(plotW / 110))));

  // Mouse → bar index (viewBox space).
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * W;
    if (mx < mL || mx > mL + plotW) { setHover(null); return; }
    setHover({ i: xFromMouse(mx), mx });
  };
  const onLeave = () => setHover(null);

  // Sweep markers (live only).
  const sweepTri = (cx: number, cy: number, r: number, up: boolean): string =>
    up
      ? `${cx},${cy + r} ${cx - r},${cy - r} ${cx + r},${cy - r}`
      : `${cx},${cy - r} ${cx - r},${cy + r} ${cx + r},${cy + r}`;

  // Horizontal level line + right-edge label.
  const levelLine = (price: number | null | undefined, color: string, label: string, dash = "4 3") => {
    if (price == null) return null;
    const y = ys(price);
    return (
      <g>
        <line x1={mL} y1={y} x2={mL + plotW} y2={y} stroke={color} strokeWidth={1} strokeDasharray={dash} opacity={0.9} />
        <text x={mL + plotW + 4} y={y + 3} fill={color} fontSize={10} fontFamily="var(--font-mono)">
          {label} {price.toFixed(1)}
        </text>
      </g>
    );
  };

  // VWAP band polygon for the price panel.
  const upPts = micro.map((m, i) => ({ i, val: m.avwap_upper })).filter(o => o.val != null);
  const loPts = micro.map((m, i) => ({ i, val: m.avwap_lower })).filter(o => o.val != null).reverse();
  const bandPolygon = upPts.length && loPts.length
    ? [...upPts.map(o => `${xs(o.i).toFixed(1)},${ys(o.val as number).toFixed(1)}`),
       ...loPts.map(o => `${xs(o.i).toFixed(1)},${ys(o.val as number).toFixed(1)}`)].join(" ")
    : "";

  // Hovered bar + tooltip card geometry.
  const hb = hover ? bars[hover.i] : null;
  const hbMicro = hover ? micro[hover.i] : null;
  const hoverPrev = hover && hover.i > 0 ? bars[hover.i - 1] : null;
  const hoverDeltaPct = (hb && hb.close != null && hoverPrev?.close != null)
    ? ((hb.close / hoverPrev.close - 1) * 100)
    : null;

  const lastBar = bars[n - 1];
  const lastClose = lastBar?.close;

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height={height}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
      style={{ display: "block", background: "var(--bg-surface)", borderRadius: "var(--radius-sm)", cursor: "crosshair" }}
    >
      {/* ── Reusable defs (gradient for area chart) ── */}
      <defs>
        <linearGradient id="priceAreaGradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent-cyan)" stopOpacity="0.55" />
          <stop offset="100%" stopColor="var(--accent-cyan)" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* ── Price grid + y labels (RIGHT side) ── */}
      {priceGrid.map((p, k) => (
        <g key={`pg${k}`}>
          <line x1={mL} y1={ys(p)} x2={mL + plotW} y2={ys(p)} stroke={GRID} strokeWidth={0.5} opacity={0.5} />
          <text
            x={mL + plotW + 32}
            y={ys(p) + 3}
            fill={MUTED}
            fontSize={9}
            fontFamily="var(--font-mono)"
            textAnchor="end"
          >{p.toFixed(0)}</text>
        </g>
      ))}

      {/* ── Volume grid (right side too) ── */}
      {volGrid.map((g, k) => (
        <g key={`vg${k}`}>
          <line x1={mL} y1={yv(g)} x2={mL + plotW} y2={yv(g)} stroke={GRID} strokeWidth={0.4} opacity={0.4} />
          {k === 2 && (
            <text x={mL + plotW + 32} y={yv(g) + 8} fill={MUTED} fontSize={8} fontFamily="var(--font-mono)" textAnchor="end">VOL</text>
          )}
          {k === 0 && (
            <text x={mL + plotW + 32} y={yv(g) + 4} fill={MUTED} fontSize={8} fontFamily="var(--font-mono)" textAnchor="end">{fmtVol(g)}</text>
          )}
        </g>
      ))}

      {/* ── X-axis ticks (date/time labels under volume panel) ── */}
      <line x1={mL} y1={xAxisY - 6} x2={mL + plotW} y2={xAxisY - 6} stroke={GRID} strokeWidth={0.5} opacity={0.6} />
      {xTicks.map((idx, k) => {
        const x = xs(idx);
        const b = bars[idx];
        if (!b) return null;
        return (
          <g key={`xt${k}`}>
            <line x1={x} y1={xAxisY - 6} x2={x} y2={xAxisY - 3} stroke={GRID} strokeWidth={0.6} />
            <text
              x={x} y={xAxisY + 8}
              fill={MUTED} fontSize={9} fontFamily="var(--font-mono)"
              textAnchor="middle"
            >{fmtTick(b.ts, intraday, multiDayIntraday)}</text>
          </g>
        );
      })}

      {/* ── VWAP band (live mode only) ── */}
      {bandPolygon && <polygon points={bandPolygon} fill={CYAN} opacity={0.06} />}

      {/* ── Volume bars ── */}
      {bars.map((b, i) => {
        if (b.volume == null || b.volume <= 0) return null;
        const up = b.close != null && b.open != null && b.close >= b.open;
        const x = xs(i);
        const top = yv(b.volume);
        const bot = volTop + volH;
        return (
          <rect key={`v${i}`}
            x={x - cw / 2} y={top} width={cw}
            height={Math.max(0.5, bot - top)}
            fill={up ? GREEN : RED}
            opacity={0.45}
          />
        );
      })}

      {/* ── Price series — switch on chartType ── */}
      {chartType === "candles" && bars.map((b, i) => {
        if (b.open == null || b.close == null || b.high == null || b.low == null) return null;
        const up = b.close >= b.open;
        const col = up ? GREEN : RED;
        const x = xs(i);
        const yO = ys(b.open), yC = ys(b.close);
        const top = Math.min(yO, yC), bh = Math.max(1, Math.abs(yC - yO));
        return (
          <g key={`c${i}`}>
            <line x1={x} y1={ys(b.high)} x2={x} y2={ys(b.low)} stroke={col} strokeWidth={0.9} />
            <rect x={x - cw / 2} y={top} width={cw} height={bh} fill={col} opacity={0.88} />
          </g>
        );
      })}

      {/* ── Area (Yahoo-style mountain): cyan gradient fill + thin line on top ── */}
      {chartType === "area" && (() => {
        const linePts: string[] = [];
        let firstX = NaN, lastX = NaN;
        bars.forEach((b, i) => {
          if (b.close == null) return;
          const x = xs(i), y = ys(b.close);
          linePts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
          if (Number.isNaN(firstX)) firstX = x;
          lastX = x;
        });
        if (linePts.length === 0 || Number.isNaN(firstX)) return null;
        // Wrap the close path into a closed polygon down to the panel bottom.
        const closedPoly = [
          `${firstX.toFixed(1)},${priceBottomY.toFixed(1)}`,
          ...linePts,
          `${lastX.toFixed(1)},${priceBottomY.toFixed(1)}`,
        ].join(" ");
        return (
          <g>
            <polygon points={closedPoly} fill="url(#priceAreaGradient)" />
            <polyline points={linePts.join(" ")} fill="none" stroke={CYAN} strokeWidth={1.6} />
          </g>
        );
      })()}

      {/* ── Line: thin polyline of closes, no fill ── */}
      {chartType === "line" && (() => {
        const pts = bars
          .map((b, i) => b.close != null ? `${xs(i).toFixed(1)},${ys(b.close).toFixed(1)}` : "")
          .filter(Boolean)
          .join(" ");
        return pts ? <polyline points={pts} fill="none" stroke={CYAN} strokeWidth={1.4} /> : null;
      })()}

      {/* ── SMA(20) overlay (whenever we have ≥20 bars) ── */}
      {sma20.some(x => x != null) && (
        <polyline
          points={sma20.map((m, i) => m != null ? `${xs(i).toFixed(1)},${ys(m).toFixed(1)}` : "").filter(Boolean).join(" ")}
          fill="none"
          stroke={AMBER}
          strokeWidth={1.2}
          opacity={0.85}
        />
      )}

      {/* ── Anchored VWAP + bands (live mode) ── */}
      <polyline points={poly(m => m.avwap)} fill="none" stroke={CYAN} strokeWidth={1.3} />
      <polyline points={poly(m => m.avwap_upper)} fill="none" stroke={CYAN} strokeWidth={0.6} opacity={0.5} strokeDasharray="3 3" />
      <polyline points={poly(m => m.avwap_lower)} fill="none" stroke={CYAN} strokeWidth={0.6} opacity={0.5} strokeDasharray="3 3" />

      {/* ── Liquidity-sweep markers (live mode) ── */}
      {micro.map((m, i) => {
        const b = bars[i];
        if (!b) return null;
        if (m.sweep_low && b.low != null) return <polygon key={`sl${i}`} points={sweepTri(xs(i), ys(b.low) + 6, 4, true)} fill={GREEN} />;
        if (m.sweep_high && b.high != null) return <polygon key={`sh${i}`} points={sweepTri(xs(i), ys(b.high) - 6, 4, false)} fill={RED} />;
        return null;
      })}

      {/* ── Session levels (live mode) ── */}
      {last && levelLine(last.vp_poc, AMBER, "POC", "1 0")}
      {last && levelLine(last.vp_vah, AMBER, "VAH")}
      {last && levelLine(last.vp_val, AMBER, "VAL")}

      {/* ── Trade levels from current signal ── */}
      {signal && levelLine(signal.entry, TEXT_PRIMARY, "Entry", "5 2")}
      {signal && levelLine(signal.target, GREEN, "Target")}
      {signal && levelLine(signal.stop, RED, "Stop")}

      {/* ── Last-price marker on right edge ── */}
      {lastClose != null && (
        <g>
          <line x1={mL} y1={ys(lastClose)} x2={mL + plotW} y2={ys(lastClose)} stroke={CYAN} strokeWidth={0.4} strokeDasharray="2 4" opacity={0.55} />
          <rect x={mL + plotW + 2} y={ys(lastClose) - 8} width={56} height={16} rx={2} fill="var(--bg-card)" stroke={CYAN} strokeWidth={0.7} />
          <text x={mL + plotW + 6} y={ys(lastClose) + 4} fill={CYAN} fontSize={11} fontFamily="var(--font-mono)" fontWeight={600}>
            {lastClose.toFixed(2)}
          </text>
        </g>
      )}

      {/* ── Hover crosshair + tooltip card ── */}
      {hb && hover && (
        <g>
          {/* Vertical crosshair */}
          <line x1={xs(hover.i)} y1={mT} x2={xs(hover.i)} y2={mT + totalPanelH} stroke={CYAN} strokeWidth={0.6} strokeDasharray="3 3" opacity={0.7} />
          {/* Tooltip card (right side if cursor on left half, left side otherwise) */}
          {(() => {
            const cw_tip = 200;
            const ch_tip = 108;
            const place = xs(hover.i) < W / 2 ? xs(hover.i) + 10 : xs(hover.i) - cw_tip - 10;
            const tx = Math.max(mL, Math.min(mL + plotW - cw_tip, place));
            const ty = mT + 6;
            const row = (label: string, val: string, color = TEXT_PRIMARY, dy = 0) => (
              <g transform={`translate(${tx + 8}, ${ty + 30 + dy})`}>
                <text x={0} y={0} fill={MUTED} fontSize={9} fontFamily="var(--font-mono)">{label}</text>
                <text x={cw_tip - 16} y={0} textAnchor="end" fill={color} fontSize={10} fontFamily="var(--font-mono)" fontWeight={600}>{val}</text>
              </g>
            );
            const upBar = hb.close != null && hb.open != null && hb.close >= hb.open;
            const candleCol = upBar ? GREEN : RED;
            return (
              <>
                <rect x={tx} y={ty} width={cw_tip} height={ch_tip} rx={4} fill="var(--bg-card)" stroke={GRID} strokeWidth={0.8} opacity={0.96} />
                <text x={tx + 8} y={ty + 14} fill={CYAN} fontSize={10} fontFamily="var(--font-mono)" fontWeight={700}>
                  {fmtTooltipTs(hb.ts, intraday)}
                </text>
                {row("O", hb.open != null ? hb.open.toFixed(2) : "—", TEXT_PRIMARY, 0)}
                {row("H", hb.high != null ? hb.high.toFixed(2) : "—", GREEN, 12)}
                {row("L", hb.low  != null ? hb.low.toFixed(2)  : "—", RED,   24)}
                {row("C", hb.close != null ? hb.close.toFixed(2) : "—", candleCol, 36)}
                {row("Vol", fmtVol(hb.volume), MUTED, 48)}
                {hoverDeltaPct != null && row("Δ", `${hoverDeltaPct >= 0 ? "+" : ""}${hoverDeltaPct.toFixed(2)}%`, hoverDeltaPct >= 0 ? GREEN : RED, 60)}
                {hbMicro?.avwap != null && row("AVWAP", hbMicro.avwap.toFixed(2), CYAN, 72)}
              </>
            );
          })()}
        </g>
      )}

      {/* ── Legend strip (bottom-left) ── */}
      <g transform={`translate(${mL + 4}, ${H - 6})`}>
        <text x={0} y={0} fill={MUTED} fontSize={8} fontFamily="var(--font-mono)">
          <tspan fill={AMBER}>━</tspan> SMA20  <tspan fill={CYAN}>━</tspan> AVWAP  <tspan fill={GREEN}>▲</tspan>/<tspan fill={RED}>▼</tspan> sweeps  <tspan fill={AMBER}>┄</tspan> POC/VAH/VAL
        </text>
      </g>
    </svg>
  );
}

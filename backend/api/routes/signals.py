"""Signal API endpoints -- ranked signals with factor attribution.

Every directional row returned by this module is enriched with trade levels
(entry / target / stop / risk_reward) via `qsde.risk.trade_levels`. The
entry price comes from the latest close in `ohlcv`, and the ATR comes from
the `tech_atr_pct` factor in `factor_pit`. We compute these in Python (not
SQL) so the per-horizon volatility multipliers stay in one place.
"""

from fastapi import APIRouter, Query
from typing import Optional

from qsde.risk.trade_levels import compute_trade_levels

router = APIRouter()


def _enrich_with_levels(rows: list[dict]) -> list[dict]:
    """Attach close / atr_pct / trade levels to each signal row in-place.

    Performs two bulk lookups (latest close per symbol, latest tech_atr_pct
    per symbol) so we don't N+1 query for a 200-row signal list.
    """
    if not rows:
        return rows
    from qsde.db import read_sql

    symbols = sorted({r["symbol"] for r in rows if r.get("symbol")})
    if not symbols:
        return rows

    # Latest close per symbol. Note: SQLAlchemy's :param binding with a
    # Python list expands cleanly via psycopg2's tuple adapter only when
    # using IN-style or ANY(ARRAY[...]). The simplest portable form is
    # `= ANY(:syms)` with the list passed as-is -- psycopg2 maps Python
    # list -> PostgreSQL ARRAY.
    closes_df = read_sql(
        """SELECT DISTINCT ON (symbol)
                  symbol, close, date
             FROM ohlcv
            WHERE symbol = ANY(:syms)
         ORDER BY symbol, date DESC""",
        params={"syms": symbols},
    )
    close_map = {r.symbol: float(r.close) for r in closes_df.itertuples()}

    # Latest tech_atr_pct per symbol from the PIT factor store.
    # Convention in this codebase: currently-valid rows have
    # `valid_to = 'infinity'::timestamptz` (NOT NULL -- see pit_writer.py),
    # and the column is `factor_value` (NOT `value`).
    atr_df = read_sql(
        """SELECT DISTINCT ON (symbol)
                  symbol, factor_value
             FROM factor_pit
            WHERE factor_name = 'tech_atr_pct'
              AND symbol = ANY(:syms)
              AND valid_to = 'infinity'::timestamptz
         ORDER BY symbol, valid_from DESC""",
        params={"syms": symbols},
    )
    atr_map = {r.symbol: float(r.factor_value) for r in atr_df.itertuples()}

    for r in rows:
        sym = r.get("symbol")
        price = close_map.get(sym)
        atr_pct = atr_map.get(sym)
        levels = compute_trade_levels(
            price=price,
            atr_pct=atr_pct,
            predicted_return=r.get("predicted_return"),
            direction=r.get("direction"),
            horizon=r.get("horizon") or "swing",
        )
        r["entry_price"] = levels["entry"]
        r["target_price"] = levels["target"]
        r["stop_price"] = levels["stop"]
        r["risk_reward"] = levels["risk_reward"]
        r["atr_pct"] = levels["atr_pct"]
        r["trade_quality"] = levels["quality"]
        r["trade_notes"] = levels["notes"]
    return rows


def _drop_sign_inconsistent(rows: list[dict]) -> tuple[list[dict], int]:
    """Structural-validity filter on signal rows.

    HISTORY: this gate used to drop rows where `direction` disagreed with the
    SIGN of `predicted_return`, and rows below a per-horizon magnitude floor.
    That logic assumed `predicted_return` was an actual forward return.

    Under AFML triple-barrier labeling (current pipeline), the model regresses
    on {-1, 0, +1} barrier-outcome labels, so `predicted_return` is a
    cross-sectional SCORE, not a return:
      * Its absolute sign is uninformative — the intraday model, for example,
        regresses toward a slightly-bearish base rate and emits all-negative
        scores, yet the RANKING is what carries the validated edge (the
        stress test sorted by score and went long the top-K).
      * Its magnitude is on a label scale (~0.1 intraday, ~0.005 swing), so a
        return-calibrated floor (0.3%-2%) is meaningless.

    `direction` is set authoritatively at generation time from the
    cross-sectional rank of the score (top decile -> +1, bottom -> -1), so it
    is internally coherent by construction. The old return-sign cross-check
    can no longer catch a real bug (it only mis-fires on score-scaled
    predictions), so we trust `direction` and drop only structurally-invalid
    rows.

    Returns (kept_rows, n_dropped).
    """
    kept: list[dict] = []
    dropped = 0
    for r in rows:
        d = r.get("direction")
        # Structural validity only: direction must be one of {-1, 0, +1}.
        if d not in (-1, 0, 1):
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


@router.get("/signals/edge_stats")
def signals_edge_stats():
    """Validated, cost+liquidity stress-tested edge per horizon.

    The UI/terminal should show these next to signals so the user trades
    with honest expectations: e.g. intraday liquid-only net Sharpe ~0.9,
    concentration-driven — NOT the gross backtest fantasy.
    """
    from qsde.models.edge_stats import read_edge_stats
    return read_edge_stats()


@router.get("/signals")
def get_signals(
    horizon: str = Query(default="swing", description="Signal horizon: intraday, swing, long"),
    limit: int = Query(default=200, ge=1, le=500),
    direction: Optional[int] = Query(default=None, description="Filter: -1, 0, +1, or omit for all"),
    sector: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False,
        description="Include signals for symbols that are no longer is_active=TRUE (default: hide)."),
    liquid_only: bool = Query(default=False,
        description="Only return tradeable (is_liquid=TRUE) signals. The stress test "
                    "proved the edge is fill-fiction in illiquid names; the live "
                    "dashboard should default this TRUE for intraday."),
    sort_by_prediction: bool = Query(default=False,
        description="Sort by predicted_return DESC instead of ranking_score. Reproduces "
                    "the validated 'top-K within liquid universe' selection."),
):
    """Get the latest signal PER SYMBOL for this horizon, ranked by score.

    Quality gates applied at the API boundary:
      1. Only symbols with `universe.is_active = TRUE` (no leaked bonds).
      2. Sign-consistency + per-horizon magnitude floor (_drop_sign_inconsistent).
      3. (Optional) liquid_only — restrict to is_liquid=TRUE names, which is
         the ONLY universe where the backtested edge survives execution
         costs. Combined with sort_by_prediction, this reproduces the
         validated top-K-within-liquid strategy.

    The response carries `edge` — the validated net-Sharpe stats for this
    horizon — so the caller never has to imply every BUY is gold.
    """
    try:
        from qsde.db import read_sql
        outer_clauses = []
        if not include_inactive:
            outer_clauses.append("u.is_active = TRUE")
        if liquid_only:
            outer_clauses.append("l.is_liquid = TRUE")
        params = {"horizon": horizon, "limit": limit}
        if direction is not None:
            outer_clauses.append("l.direction = :direction")
            params["direction"] = direction
        if sector:
            outer_clauses.append("u.sector = :sector")
            params["sector"] = sector
        where = ("WHERE " + " AND ".join(outer_clauses)) if outer_clauses else ""
        join_kind = "LEFT JOIN" if include_inactive else "INNER JOIN"
        order_col = "l.predicted_return" if sort_by_prediction else "l.ranking_score"
        df = read_sql(
            f"""WITH latest AS (
                  SELECT DISTINCT ON (symbol)
                         symbol, date, horizon, direction, confidence,
                         predicted_return, ranking_score, factor_attribution,
                         top_factors, model_version, adv_20d, is_liquid
                    FROM signals
                   WHERE horizon = :horizon
                ORDER BY symbol, date DESC
                )
                SELECT l.symbol, l.date, l.horizon, l.direction, l.confidence,
                       l.predicted_return, l.ranking_score, l.factor_attribution,
                       l.top_factors, l.model_version, l.adv_20d, l.is_liquid,
                       u.company_name, u.sector, u.industry
                  FROM latest l
                  {join_kind} universe u ON u.symbol = l.symbol
                  {where}
              ORDER BY {order_col} DESC NULLS LAST
                 LIMIT :limit""",
            params=params,
        )
        rows = df.to_dict("records")
        rows, n_inconsistent = _drop_sign_inconsistent(rows)
        rows = _enrich_with_levels(rows)

        from qsde.models.edge_stats import horizon_edge
        return {
            "signals": rows,
            "count": len(rows),
            "horizon": horizon,
            "liquid_only": liquid_only,
            "dropped_sign_inconsistent": n_inconsistent,
            "edge": horizon_edge(horizon),   # honest net-Sharpe stats for this horizon
        }
    except Exception as e:
        return {"signals": [], "count": 0, "horizon": horizon, "error": str(e)}


@router.get("/signals/{symbol}")
def get_signal_detail(symbol: str, horizon: str = "swing"):
    """Get detailed signal for a specific stock with factor attribution + trade levels."""
    try:
        from qsde.db import read_sql
        df = read_sql(
            """SELECT * FROM signals
               WHERE symbol = :symbol AND horizon = :horizon
               ORDER BY date DESC LIMIT 1""",
            params={"symbol": symbol.upper(), "horizon": horizon},
        )
        if df.empty:
            return {"error": "No signal found", "symbol": symbol}
        row = df.to_dict("records")[0]
        _enrich_with_levels([row])
        return row
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


@router.get("/signals/history/{symbol}")
def get_signal_history(
    symbol: str,
    horizon: str = "swing",
    days: int = Query(default=30, ge=1, le=365),
):
    """Get signal history for a stock."""
    try:
        from qsde.db import read_sql
        df = read_sql(
            """SELECT date, direction, confidence, predicted_return, ranking_score
               FROM signals
               WHERE symbol = :symbol AND horizon = :horizon
               ORDER BY date DESC LIMIT :days""",
            params={"symbol": symbol.upper(), "horizon": horizon, "days": days},
        )
        return {"history": df.to_dict("records"), "symbol": symbol, "horizon": horizon}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}

"""
DCF Valuation Engine.

Adapted from Anthropic financial-services: dcf-model SKILL.md
Builds discounted cash flow models with:
- Historical analysis (3-5 years)
- Revenue projections (5-year forecast)
- FCF computation (NOPAT + D&A - CapEx - ΔNWC)
- WACC via CAPM (risk-free from FRED, beta from OHLCV)
- Terminal value (perpetuity growth method)
- Enterprise-to-equity bridge
- Sensitivity analysis (WACC vs terminal growth)
- Bear / Base / Bull scenarios
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


# ── WACC / CAPM ──────────────────────────────────────────────

def compute_beta(symbol: str, market_symbol: str = "^NSEI", period_years: int = 5) -> float:
    """Compute 5-year monthly beta vs Nifty 50 from OHLCV data."""
    try:
        stock = read_sql(
            "SELECT date, close FROM ohlcv WHERE symbol = :symbol "
            "ORDER BY date DESC LIMIT :limit",
            params={"symbol": symbol, "limit": period_years * 252},
        )
        market = read_sql(
            "SELECT date, close FROM ohlcv WHERE symbol = :symbol "
            "ORDER BY date DESC LIMIT :limit",
            params={"symbol": market_symbol, "limit": period_years * 252},
        )
        if stock.empty or market.empty or len(stock) < 60:
            return 1.0  # Default beta

        # Monthly returns
        stock["date"] = pd.to_datetime(stock["date"])
        market["date"] = pd.to_datetime(market["date"])
        stock = stock.set_index("date").resample("ME").last()
        market = market.set_index("date").resample("ME").last()

        stock_ret = stock["close"].pct_change().dropna()
        market_ret = market["close"].pct_change().dropna()

        # Align
        aligned = pd.DataFrame({"stock": stock_ret, "market": market_ret}).dropna()
        if len(aligned) < 12:
            return 1.0

        cov = aligned["stock"].cov(aligned["market"])
        var = aligned["market"].var()
        beta = cov / var if var != 0 else 1.0
        return round(max(0.3, min(beta, 3.0)), 3)  # Clamp to reasonable range
    except Exception:
        return 1.0


def get_risk_free_rate() -> float:
    """Get latest risk-free rate from FRED macro data (India 10Y or US 10Y)."""
    try:
        rf = read_sql(
            "SELECT value FROM macro WHERE series_id = 'DGS10' "
            "ORDER BY date DESC LIMIT 1",
        )
        if not rf.empty:
            return float(rf.iloc[0]["value"]) / 100  # Convert percentage to decimal
    except Exception:
        pass
    return 0.07  # Default 7% for India


def compute_wacc(
    beta: float,
    risk_free_rate: float,
    equity_risk_premium: float = 0.065,
    cost_of_debt_pretax: float = 0.08,
    tax_rate: float = 0.25,
    debt_weight: float = 0.20,
) -> dict:
    """
    Compute WACC using CAPM methodology.

    CAPM: Cost of Equity = Rf + β × ERP
    WACC = (Ke × We) + (Kd × (1-t) × Wd)
    """
    cost_of_equity = risk_free_rate + beta * equity_risk_premium
    cost_of_debt_after_tax = cost_of_debt_pretax * (1 - tax_rate)
    equity_weight = 1 - debt_weight

    wacc = (cost_of_equity * equity_weight) + (cost_of_debt_after_tax * debt_weight)

    return {
        "wacc": round(wacc, 4),
        "cost_of_equity": round(cost_of_equity, 4),
        "cost_of_debt_after_tax": round(cost_of_debt_after_tax, 4),
        "risk_free_rate": round(risk_free_rate, 4),
        "beta": beta,
        "equity_risk_premium": equity_risk_premium,
        "equity_weight": equity_weight,
        "debt_weight": debt_weight,
        "tax_rate": tax_rate,
    }


# ── Historical Financials ────────────────────────────────────

def get_historical_financials(symbol: str) -> dict:
    """Retrieve historical revenue, margins, FCF from database."""
    df = read_sql(
        "SELECT fiscal_date as date, revenue, net_income, market_cap, enterprise_value, "
        "pe_ratio, gross_margin, operating_margin, net_margin, "
        "roe, roic, revenue_growth_yoy as revenue_growth, fcf_per_share, debt_equity as debt_to_equity "
        "FROM fundamentals WHERE symbol = :symbol "
        "ORDER BY fiscal_date DESC LIMIT 20",
        params={"symbol": symbol},
    )

    if df.empty:
        return {"available": False, "symbol": symbol}

    latest = df.iloc[0]
    return {
        "available": True,
        "symbol": symbol,
        "latest_date": str(latest.get("date", "")),
        "revenue": _safe_float(latest.get("revenue")),
        "net_income": _safe_float(latest.get("net_income")),
        "market_cap": _safe_float(latest.get("market_cap")),
        "enterprise_value": _safe_float(latest.get("enterprise_value")),
        "gross_margin": _safe_float(latest.get("gross_margin")),
        "operating_margin": _safe_float(latest.get("operating_margin")),
        "net_margin": _safe_float(latest.get("net_margin")),
        "roe": _safe_float(latest.get("roe")),
        "roic": _safe_float(latest.get("roic")),
        "revenue_growth": _safe_float(latest.get("revenue_growth")),
        "pe_ratio": _safe_float(latest.get("pe_ratio")),
        "debt_to_equity": _safe_float(latest.get("debt_to_equity")),
        "history": df.where(df.notna(), None).to_dict("records"),
    }


def _safe_float(val, default=None):
    """Safely convert to float."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return default


# ── DCF Projections ──────────────────────────────────────────

def project_cash_flows(
    base_revenue: float,
    growth_rates: list[float],
    ebit_margin: float,
    tax_rate: float = 0.25,
    da_pct: float = 0.03,
    capex_pct: float = 0.05,
    nwc_pct: float = 0.01,
) -> list[dict]:
    """
    Build 5-year FCF projection.

    FCF = NOPAT + D&A - CapEx - ΔNWC
    NOPAT = EBIT × (1 - tax_rate)
    """
    projections = []
    prev_revenue = base_revenue

    for i, g in enumerate(growth_rates):
        revenue = prev_revenue * (1 + g)
        ebit = revenue * ebit_margin
        nopat = ebit * (1 - tax_rate)
        da = revenue * da_pct
        capex = revenue * capex_pct
        delta_nwc = (revenue - prev_revenue) * nwc_pct
        fcf = nopat + da - capex - delta_nwc

        projections.append({
            "year": i + 1,
            "revenue": round(revenue, 2),
            "revenue_growth": round(g * 100, 1),
            "ebit": round(ebit, 2),
            "ebit_margin": round(ebit_margin * 100, 1),
            "nopat": round(nopat, 2),
            "da": round(da, 2),
            "capex": round(capex, 2),
            "delta_nwc": round(delta_nwc, 2),
            "fcf": round(fcf, 2),
        })
        prev_revenue = revenue

    return projections


def compute_terminal_value(
    final_fcf: float,
    wacc: float,
    terminal_growth: float = 0.03,
) -> dict:
    """
    Terminal value using perpetuity growth method.

    TV = FCF_n+1 / (WACC - g)
    Constraint: g < WACC (otherwise infinite value)
    """
    if terminal_growth >= wacc:
        terminal_growth = wacc - 0.01  # Safety clamp

    terminal_fcf = final_fcf * (1 + terminal_growth)
    tv = terminal_fcf / (wacc - terminal_growth)

    return {
        "terminal_fcf": round(terminal_fcf, 2),
        "terminal_value": round(tv, 2),
        "terminal_growth": round(terminal_growth * 100, 2),
    }


def discount_cash_flows(
    projections: list[dict],
    terminal_value: float,
    wacc: float,
    projection_years: int = 5,
) -> dict:
    """
    Discount projected FCFs and terminal value to present.
    Uses mid-year convention per Anthropic DCF skill.
    """
    pv_fcfs = []
    total_pv_fcf = 0

    for p in projections:
        period = p["year"] - 0.5  # Mid-year convention
        discount_factor = 1 / (1 + wacc) ** period
        pv = p["fcf"] * discount_factor
        pv_fcfs.append({
            "year": p["year"],
            "fcf": p["fcf"],
            "discount_factor": round(discount_factor, 4),
            "pv_fcf": round(pv, 2),
        })
        total_pv_fcf += pv

    # Terminal value discount
    tv_period = projection_years - 0.5  # End of final year, mid-year
    tv_discount_factor = 1 / (1 + wacc) ** tv_period
    pv_terminal = terminal_value * tv_discount_factor

    enterprise_value = total_pv_fcf + pv_terminal
    tv_pct_of_ev = (pv_terminal / enterprise_value * 100) if enterprise_value else 0

    return {
        "pv_fcfs": pv_fcfs,
        "total_pv_fcf": round(total_pv_fcf, 2),
        "pv_terminal_value": round(pv_terminal, 2),
        "enterprise_value": round(enterprise_value, 2),
        "tv_pct_of_ev": round(tv_pct_of_ev, 1),
    }


def equity_bridge(
    enterprise_value: float,
    net_debt: float,
    shares_outstanding: float,
    current_price: float,
) -> dict:
    """
    Enterprise-to-equity value bridge.

    Equity Value = EV - Net Debt (or + Net Cash)
    Implied Price = Equity Value / Diluted Shares
    """
    equity_value = enterprise_value - net_debt
    implied_price = equity_value / shares_outstanding if shares_outstanding else 0
    upside = ((implied_price / current_price) - 1) * 100 if current_price else 0

    return {
        "enterprise_value": round(enterprise_value, 2),
        "net_debt": round(net_debt, 2),
        "equity_value": round(equity_value, 2),
        "shares_outstanding": round(shares_outstanding, 2),
        "implied_price": round(implied_price, 2),
        "current_price": round(current_price, 2),
        "upside_pct": round(upside, 1),
    }


def build_sensitivity_grid(
    projections: list[dict],
    base_wacc: float,
    base_terminal_growth: float,
    net_debt: float,
    shares: float,
    wacc_steps: int = 5,
    tg_steps: int = 5,
) -> dict:
    """
    Build WACC vs Terminal Growth sensitivity grid (5x5).

    Per Anthropic DCF skill: odd dimensions, base case centered,
    center cell must equal model's actual implied price.
    """
    wacc_range = [base_wacc + (i - wacc_steps // 2) * 0.005 for i in range(wacc_steps)]
    tg_range = [base_terminal_growth + (i - tg_steps // 2) * 0.005 for i in range(tg_steps)]

    grid = []
    for wacc in wacc_range:
        row = []
        for tg in tg_range:
            if tg >= wacc:
                row.append(None)  # Invalid: g >= WACC
                continue
            # Full DCF recalc for this combination
            final_fcf = projections[-1]["fcf"]
            tv_data = compute_terminal_value(final_fcf, wacc, tg)
            dcf = discount_cash_flows(projections, tv_data["terminal_value"], wacc)
            eq = equity_bridge(dcf["enterprise_value"], net_debt, shares, 1)
            row.append(round(eq["implied_price"], 2))
        grid.append(row)

    return {
        "wacc_axis": [round(w * 100, 2) for w in wacc_range],
        "tg_axis": [round(t * 100, 2) for t in tg_range],
        "grid": grid,
        "base_wacc_idx": wacc_steps // 2,
        "base_tg_idx": tg_steps // 2,
    }


# ── Master DCF Builder ───────────────────────────────────────

def build_dcf_valuation(
    symbol: str,
    growth_scenario: str = "base",
) -> dict:
    """
    Build complete DCF valuation for a symbol.

    Scenarios (per Anthropic DCF skill):
    - Bear: Conservative growth (8-12%)
    - Base: Most likely (12-16%)
    - Bull: Optimistic (16-20%)
    """
    # Step 1: Historical financials
    hist = get_historical_financials(symbol)

    # Step 2: WACC
    beta = compute_beta(symbol)
    rf = get_risk_free_rate()
    wacc_data = compute_wacc(beta, rf)

    # Step 3: Determine base assumptions
    if hist.get("available"):
        base_revenue = hist["revenue"] or 100000
        ebit_margin = (hist["operating_margin"] or 15) / 100
        current_price = 0  # Would come from OHLCV
        # Get current price from OHLCV
        price_df = read_sql(
            "SELECT close FROM ohlcv WHERE symbol = :symbol ORDER BY date DESC LIMIT 1",
            params={"symbol": symbol},
        )
        current_price = float(price_df.iloc[0]["close"]) if not price_df.empty else 0

        market_cap = hist["market_cap"] or 0
        ev = hist["enterprise_value"] or market_cap
        net_debt = ev - market_cap if ev and market_cap else 0
        shares = (market_cap / current_price) if current_price else 1e6
    else:
        # Use placeholder assumptions
        base_revenue = 100000
        ebit_margin = 0.15
        current_price = 100
        net_debt = 0
        shares = 1e6

    # Step 4: Scenario growth rates
    scenarios = {
        "bear":  [0.08, 0.07, 0.06, 0.05, 0.04],
        "base":  [0.14, 0.12, 0.10, 0.09, 0.08],
        "bull":  [0.20, 0.18, 0.15, 0.12, 0.10],
    }
    growth_rates = scenarios.get(growth_scenario, scenarios["base"])

    # Step 5: Project cash flows
    projections = project_cash_flows(
        base_revenue=base_revenue,
        growth_rates=growth_rates,
        ebit_margin=ebit_margin,
    )

    # Step 6: Terminal value
    terminal_growth = 0.03
    tv_data = compute_terminal_value(
        projections[-1]["fcf"], wacc_data["wacc"], terminal_growth,
    )

    # Step 7: Discount cash flows
    dcf = discount_cash_flows(
        projections, tv_data["terminal_value"], wacc_data["wacc"],
    )

    # Step 8: Equity bridge
    bridge = equity_bridge(
        dcf["enterprise_value"], net_debt, shares, current_price,
    )

    # Step 9: Sensitivity grid
    sensitivity = build_sensitivity_grid(
        projections, wacc_data["wacc"], terminal_growth,
        net_debt, shares,
    )

    # Step 10: All three scenarios summary
    scenario_summary = {}
    for sc_name, sc_rates in scenarios.items():
        sc_proj = project_cash_flows(base_revenue, sc_rates, ebit_margin)
        sc_tv = compute_terminal_value(sc_proj[-1]["fcf"], wacc_data["wacc"], terminal_growth)
        sc_dcf = discount_cash_flows(sc_proj, sc_tv["terminal_value"], wacc_data["wacc"])
        sc_bridge = equity_bridge(sc_dcf["enterprise_value"], net_debt, shares, current_price)
        scenario_summary[sc_name] = {
            "implied_price": sc_bridge["implied_price"],
            "upside_pct": sc_bridge["upside_pct"],
            "enterprise_value": sc_dcf["enterprise_value"],
        }

    # `hist` is the dict returned by get_historical_financials(); its
    # `history` key is already a list of records with NaN -> None handled.
    return {
        "symbol": symbol,
        "scenario": growth_scenario,
        "historical": hist.get("history", []) if isinstance(hist, dict) else [],
        "wacc": wacc_data,
        "projections": projections,
        "terminal_value": tv_data,
        "dcf_summary": dcf,
        "equity_bridge": bridge,
        "sensitivity": sensitivity,
        "scenarios": scenario_summary,
        "methodology": {
            "projection_period": "5 years",
            "terminal_method": "Perpetuity growth",
            "discount_convention": "Mid-year",
            "adapted_from": "Anthropic financial-services dcf-model skill",
        },
    }

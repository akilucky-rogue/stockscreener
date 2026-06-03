"""
Research API endpoints — Comps, DCF, Earnings, Screener, Sector.

Adapted from Anthropic financial-services skill templates.
"""

from fastapi import APIRouter, Query
from typing import Optional

router = APIRouter()


@router.get("/research/comps/{symbol}")
def get_comps_analysis(symbol: str):
    """Comparable company analysis — peer valuation with quartile stats."""
    from qsde.research.comps_engine import build_comps_analysis
    return build_comps_analysis(symbol.upper())


@router.get("/research/dcf/{symbol}")
def get_dcf_valuation(
    symbol: str,
    scenario: str = Query(default="base", description="bear, base, or bull"),
):
    """DCF valuation model — 5-year projections, WACC, sensitivity grid."""
    from qsde.research.dcf_engine import build_dcf_valuation
    return build_dcf_valuation(symbol.upper(), scenario)


@router.get("/research/earnings/{symbol}")
def get_earnings_snapshot(symbol: str):
    """Earnings snapshot — beat/miss analysis, margin trends."""
    from qsde.research.earnings_engine import build_earnings_snapshot
    return build_earnings_snapshot(symbol.upper())


@router.get("/research/screen")
def run_stock_screen(
    preset: Optional[str] = Query(default=None, description="value, growth, quality, momentum, dividend"),
    sector: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
    sort_asc: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    include_momentum: bool = Query(default=False),
):
    """Multi-criteria stock screener with preset filters."""
    from qsde.research.screener_engine import run_screen
    return run_screen(
        preset=preset,
        sector=sector,
        sort_by=sort_by,
        sort_asc=sort_asc,
        limit=limit,
        include_momentum=include_momentum,
    )


@router.get("/research/sectors")
def get_sectors():
    """List all sectors with company counts."""
    from qsde.research.sector_engine import get_sector_list
    return {"sectors": get_sector_list()}


@router.get("/research/sector/{sector}")
def get_sector_overview(sector: str):
    """Sector overview — aggregate stats, leaders, competitive positioning."""
    from qsde.research.sector_engine import build_sector_overview
    return build_sector_overview(sector)

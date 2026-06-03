"""Universe API endpoints — Nifty 200 constituents and market data."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/universe")
def get_universe():
    """Get all active universe constituents with latest signals."""
    try:
        from qsde.db import read_sql
        df = read_sql(
            """SELECT u.symbol, u.company_name, u.sector, u.index_membership,
                      s.direction, s.confidence, s.ranking_score, s.date as signal_date
               FROM universe u
               LEFT JOIN LATERAL (
                   SELECT direction, confidence, ranking_score, date
                   FROM signals
                   WHERE symbol = u.symbol AND horizon = 'swing'
                   ORDER BY date DESC LIMIT 1
               ) s ON TRUE
               WHERE u.is_active = TRUE
               ORDER BY s.ranking_score DESC NULLS LAST""",
        )
        return {"universe": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"universe": [], "count": 0, "error": str(e)}


@router.get("/universe/top")
def get_top_picks(limit: int = 10):
    """Get auto-surfaced top picks from the model."""
    try:
        from qsde.db import read_sql
        df = read_sql(
            """SELECT u.symbol, u.company_name, u.sector,
                      s.direction, s.confidence, s.predicted_return,
                      s.ranking_score, s.top_factors
               FROM signals s
               JOIN universe u ON s.symbol = u.symbol
               WHERE s.horizon = 'swing'
                 AND s.date = (SELECT MAX(date) FROM signals WHERE horizon = 'swing')
                 AND s.direction = 1
               ORDER BY s.ranking_score DESC
               LIMIT :limit""",
            params={"limit": limit},
        )
        return {"top_picks": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"top_picks": [], "count": 0, "error": str(e)}


@router.get("/universe/sectors")
def get_sector_breakdown():
    """Get sector distribution of the universe."""
    try:
        from qsde.db import read_sql
        df = read_sql(
            """SELECT sector, COUNT(*) as count
               FROM universe WHERE is_active = TRUE AND sector IS NOT NULL
               GROUP BY sector ORDER BY count DESC""",
        )
        return {"sectors": df.to_dict("records")}
    except Exception as e:
        return {"sectors": [], "error": str(e)}

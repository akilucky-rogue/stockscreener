"""
QSDE FastAPI application entry point.

Run with: uvicorn api.main:app --reload --port 8000
"""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure backend directory is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.responses import CleanJSONResponse

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan: clean up the singleton Kite streamer on shutdown.

    Startup is intentionally empty -- the streamer is lazy-init'd on the
    first /api/analysis/... request so the API still boots even without an
    active Kite session.
    """
    yield
    try:
        from qsde.ingestion.live_subscriber import shutdown_manager
        shutdown_manager()
    except Exception as e:  # noqa: BLE001
        log.warning("Lifespan shutdown failed: %s", e)
from api.routes.signals import router as signals_router
from api.routes.universe import router as universe_router
from api.routes.health import router as health_router
from api.routes.research import router as research_router
from api.routes.factors import router as factors_router
from api.routes.backtest import router as backtest_router
from api.routes.watchlist import router as watchlist_router
from api.routes.analyze import router as analyze_router
from api.routes.kite import router as kite_router
from api.routes.intraday import router as intraday_router
from api.routes.live_signals import router as live_router
from api.routes.budget_screener import router as screener_router
from api.routes.orders import router as orders_router
from api.routes.analysis import router as analysis_router
from api.routes.paper import router as paper_router
from api.routes.risk import router as risk_router

app = FastAPI(
    title="QSDE — Quantitative Stock Decision Engine",
    description="Multi-factor ML signal generation for Indian equities (Nifty 200)",
    version="0.1.0",
    # Scrubs NaN/Inf -> null at the response boundary so individual
    # endpoints don't have to. See api/responses.py.
    default_response_class=CleanJSONResponse,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router,    prefix="/api", tags=["health"])
app.include_router(signals_router,   prefix="/api", tags=["signals"])
app.include_router(universe_router,  prefix="/api", tags=["universe"])
app.include_router(research_router,  prefix="/api", tags=["research"])
app.include_router(factors_router,   prefix="/api", tags=["factors"])
app.include_router(backtest_router,  prefix="/api", tags=["backtest"])
app.include_router(watchlist_router, prefix="/api", tags=["watchlist"])
app.include_router(analyze_router,   prefix="/api", tags=["analyze"])
app.include_router(kite_router,      prefix="/api", tags=["kite"])
app.include_router(intraday_router,  prefix="/api", tags=["intraday"])
app.include_router(live_router,      prefix="/api", tags=["live"])
app.include_router(screener_router,  prefix="/api", tags=["screener"])
app.include_router(orders_router,    prefix="/api", tags=["orders"])
app.include_router(analysis_router,  prefix="/api", tags=["analysis"])
app.include_router(paper_router,     prefix="/api", tags=["paper"])
app.include_router(risk_router,      prefix="/api", tags=["risk"])


@app.get("/")
def root():
    return {
        "name": "QSDE — Quantitative Stock Decision Engine",
        "version": "0.1.0",
        "docs": "/docs",
        "status": "Phase 0 — Layer 0 MVP",
    }

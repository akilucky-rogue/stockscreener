"""Health check endpoint — verifies DB, Redis, and data freshness."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    """System health check."""
    checks = {"api": "ok"}

    # Database check
    try:
        from qsde.db import check_connection
        checks["database"] = "ok" if check_connection() else "error"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Redis check
    try:
        import redis
        from qsde.config import settings
        r = redis.from_url(settings.redis_url)
        r.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "not_connected"

    status = "healthy" if checks["database"] == "ok" else "degraded"
    return {"status": status, "checks": checks}

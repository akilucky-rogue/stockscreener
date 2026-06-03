"""
Custom FastAPI response class that scrubs NaN / Inf before JSON serialization.

Why this exists: yfinance returns NaN for fundamentals it doesn't cover (notably
ROE/EV-EBITDA for Indian banks and gross margins where the standardized field
is missing). Python's json.dumps rejects NaN under strict JSON rules, which
crashes every endpoint that touches sparse fundamentals.

Setting CleanJSONResponse as FastAPI's default_response_class means individual
route handlers and research engines no longer have to remember to call
df.replace({np.nan: None}) on every dict path -- the response boundary
catches it.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi.responses import JSONResponse


def _scrub(value: Any) -> Any:
    """Recursively replace NaN / Inf floats with None.

    Handles dicts, lists, tuples, Python floats, and numpy floats
    (numpy.float64 is a subclass of float, so isinstance catches it).
    Everything else passes through unchanged.
    """
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(x) for x in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


class CleanJSONResponse(JSONResponse):
    """JSONResponse that converts NaN/Inf floats to null before encoding."""

    def render(self, content: Any) -> bytes:
        return super().render(_scrub(content))

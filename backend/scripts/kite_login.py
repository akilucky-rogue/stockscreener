"""
Manually exchange a Kite request_token for an access_token and store it.

Use this when the browser redirect after Zerodha login did NOT auto-store the
token (usually because the Kite app's Redirect URL isn't set to
http://127.0.0.1:8000/api/kite/callback). Works without the API running -- it
just needs the DB up and KITE_API_KEY/KITE_API_SECRET in qsde/.env.

Steps:
  1. Open http://localhost:8000/api/kite/login_url (or the login_url it returns)
     and log in at Zerodha.
  2. You'll land on some URL containing  ?request_token=XXXX&action=login&status=success
     Copy the XXXX value (the request_token).
  3. IMMEDIATELY run (it expires in ~2 min and is single-use):
         python scripts/kite_login.py XXXX
  4. Verify:  python scripts/kite_login.py --status
              or  Invoke-RestMethod http://localhost:8000/api/kite/status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _status() -> None:
    from qsde.db.connection import read_sql
    df = read_sql(
        "SELECT user_name, user_id, expires_at FROM kite_tokens "
        "WHERE is_active = TRUE AND expires_at > NOW() ORDER BY login_time DESC LIMIT 1"
    )
    if df.empty:
        print("Kite: NOT authenticated (no active token).")
    else:
        r = df.iloc[0]
        print(f"Kite: authenticated as {r['user_name']} ({r['user_id']}); token valid until {r['expires_at']}.")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/kite_login.py <request_token>   (or --status)")
        sys.exit(1)
    if sys.argv[1] in ("--status", "-s", "status"):
        _status()
        return

    request_token = sys.argv[1].strip()
    # Tolerate a pasted full URL: extract request_token=... if present.
    if "request_token=" in request_token:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(request_token).query)
        request_token = (q.get("request_token") or [request_token])[0]

    from qsde.ingestion.kite_client import get_kite_client
    client = get_kite_client()
    try:
        info = client.exchange_token(request_token)
        print(f"Kite connected: user={info.get('user_name')} valid_until={info.get('expires_at')}")
        print("Token stored. You can now run scripts/kite_ingest.py.")
    except Exception as e:  # noqa: BLE001
        log.error("Token exchange FAILED: %s", e)
        print(
            "\nCommon causes:\n"
            "  * request_token already used or expired (>2 min) -> log in again for a fresh one.\n"
            "  * KITE_API_SECRET in .env is wrong -> fix it, then retry.\n"
            "  * System clock skew -> sync time."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

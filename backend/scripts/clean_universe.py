"""
Deactivate bond/G-Sec/NCD rows that leaked into the universe via Kite's
instrument-master sync.

What this catches (and why):
  * `737NTPC35C-NA`, `753NTPC30E-NC`  — NTPC corporate bonds (NCD codes)
  * `1018GS2026-GS`                   — GOI loan / G-Sec
  * `738REC27TF-N2`, `735NHAI31-N9`   — REC/NHAI bond series
  * `1003ISFL28-N4`, `105IIFL29-N7`   — finance-co NCD tranches
  ...and ~30 more.

Filter is `(company_name IS NULL/blank) OR symbol ends with `-XX`
(2-char alphanumeric suffix)`. Legit equities (`360ONE`, `3MINDIA`,
`5PAISA`, `63MOONS`) all have non-empty `company_name` AND don't match
the bond suffix, so they survive.

Idempotent. Run after each `POST /api/kite/refresh_instruments`.

Usage:
  python scripts/clean_universe.py              # dry-run preview
  python scripts/clean_universe.py --apply      # actually deactivate
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import read_sql, execute_sql


# Rows matching ANY of these conditions are NOT equity instruments and
# should be deactivated. Deliberately narrow to avoid false positives:
#   - Kite's instrument-master leaves `company_name` blank for most Nifty
#     50 names, so we CANNOT use "empty company_name" as a filter.
#   - Legit equities starting with digits (3MINDIA, 360ONE, 5PAISA, 63MOONS,
#     20MICRONS, 21STCENMGM) exist, so "starts with digits" is also out.
#   - The reliable bond/NCD/G-Sec marker is the 2-char alphanumeric suffix
#     after a hyphen at end-of-symbol (-NA, -N4, -GS, -TF, -NC, -NX, etc.).
#     No NSE equity uses this pattern (BAJAJ-AUTO, LIC-HFL have ≥3-char
#     suffixes after the hyphen).
_BAD_FILTER = (
    "symbol ~ '-[A-Z0-9]{2}$' "                    # series suffix
    "OR company_name ILIKE '%GOI%LOAN%' "          # GOI loan rows
    "OR company_name ILIKE '%G-SEC%' "
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually run the UPDATE (default is dry-run preview).")
    args = ap.parse_args()

    preview = read_sql(
        f"SELECT symbol, company_name FROM universe "
        f"WHERE is_active = TRUE AND ({_BAD_FILTER}) "
        f"ORDER BY symbol"
    )
    print(f"Found {len(preview)} non-equity rows currently active:")
    if not preview.empty:
        with __import__("pandas").option_context("display.max_rows", 200, "display.width", 140):
            print(preview.to_string(index=False))

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to deactivate these rows.")
        return

    # Use psycopg2 %(name)s placeholder style since execute_sql goes through
    # raw cursor.execute (no SQLAlchemy bind translation).
    execute_sql(
        f"UPDATE universe SET is_active = FALSE "
        f"WHERE is_active = TRUE AND ({_BAD_FILTER})"
    )
    after = read_sql("SELECT COUNT(*) AS n_active FROM universe WHERE is_active = TRUE")
    print()
    print(f"Done. Active universe is now {int(after.iloc[0]['n_active'])} rows.")


if __name__ == "__main__":
    main()

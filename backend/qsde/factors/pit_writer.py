"""
Point-In-Time Factor Writer.

Handles writing computed wide-format factor DataFrames into the `factor_pit`
bitemporal hypertable. Expires old rows safely by setting `valid_to = NOW()`
ONLY when those rows actually exist -- for never-before-seen symbols we
skip the expire step entirely, saving a huge sequential scan.

Per-symbol commits give:
  * Progress visibility (you can see how far through 500 symbols you are)
  * Resumability (Ctrl-C / network blip doesn't lose all prior work)
  * Bounded transaction size (postgres doesn't try to plan a 419k-row UPDATE)
"""

import logging
from datetime import datetime

import pandas as pd
from psycopg2.extras import execute_values

from qsde.db.connection import get_sync_conn, read_sql

log = logging.getLogger(__name__)


def write_factors_to_pit(
    factors_wide: pd.DataFrame,
    data_source: str = "qsde_engine",
) -> int:
    """
    Write wide-format factors to the PIT table.

    Args:
        factors_wide: DataFrame indexed by date, with columns like
                      'symbol', 'tech_rsi_14', 'fund_pe_ratio', etc.
        data_source:  Free-text source tag.

    Returns:
        Number of (symbol, date, factor) rows inserted.
    """
    if factors_wide.empty:
        return 0

    # Normalize the date column.
    if "date" not in factors_wide.columns and factors_wide.index.name in ("date", "as_of_date"):
        df = factors_wide.reset_index().rename(columns={factors_wide.index.name: "date"})
    else:
        df = factors_wide.copy()

    if "symbol" not in df.columns or "date" not in df.columns:
        log.warning("Factors dataframe missing symbol or date column.")
        return 0

    # Wide -> long.
    id_vars = ["symbol", "date"]
    value_vars = [
        c for c in df.columns
        if c not in id_vars and c not in ("yf_symbol", "source")
    ]
    if not value_vars:
        return 0

    long_df = (
        df.melt(
            id_vars=id_vars,
            value_vars=value_vars,
            var_name="factor_name",
            value_name="factor_value",
        )
        .dropna(subset=["factor_value"])
    )
    if long_df.empty:
        return 0

    long_df = long_df.rename(columns={"date": "as_of_date"})
    long_df["data_source"] = data_source
    now = datetime.now()
    long_df["valid_from"] = now

    # Per-symbol processing for bounded transaction size + visible progress.
    symbols = sorted(long_df["symbol"].unique().tolist())
    log.info("Writing factor_pit for %d symbols (%d total rows)...",
             len(symbols), len(long_df))

    # Which symbols already have ANY rows in factor_pit? Those need the
    # expire pass; brand-new symbols can take the fast insert-only path.
    existing_df = read_sql(
        "SELECT DISTINCT symbol FROM factor_pit WHERE symbol = ANY(:syms)",
        params={"syms": symbols},
    )
    existing_set = set(existing_df["symbol"].tolist()) if not existing_df.empty else set()
    log.info("  %d symbols already in factor_pit (expire+insert), "
             "%d brand-new (insert-only fast path)",
             len(existing_set), len(symbols) - len(existing_set))

    insert_sql = """
        INSERT INTO factor_pit
            (symbol, as_of_date, valid_from, factor_name, factor_value, data_source)
        VALUES %s
        ON CONFLICT (symbol, as_of_date, factor_name, valid_from) DO NOTHING
    """

    # `data` is a (new_valid_to, symbol, as_of_date, factor_name) VALUES list.
    expire_sql = """
        UPDATE factor_pit
           SET valid_to = data.new_valid_to::timestamptz
          FROM (VALUES %s) AS data(new_valid_to, symbol, as_of_date, factor_name)
         WHERE factor_pit.symbol      = data.symbol::varchar
           AND factor_pit.as_of_date  = data.as_of_date::date
           AND factor_pit.factor_name = data.factor_name::varchar
           AND factor_pit.valid_to    = 'infinity'::timestamptz
    """

    total_written = 0
    PER_CALL = 5000   # rows per execute_values call -- keep SQL string < 1MB
    try:
        with get_sync_conn() as conn:
            cur = conn.cursor()
            for i, sym in enumerate(symbols, 1):
                sym_df = long_df[long_df["symbol"] == sym]
                # Long-form tuples to push.
                insert_records = list(
                    sym_df[["symbol", "as_of_date", "valid_from",
                            "factor_name", "factor_value", "data_source"]]
                    .itertuples(index=False, name=None)
                )

                # 1. Expire only if this symbol has prior rows.
                if sym in existing_set:
                    expire_records = [
                        (now, r[0], r[1], r[3]) for r in insert_records
                    ]
                    for j in range(0, len(expire_records), PER_CALL):
                        execute_values(
                            cur, expire_sql,
                            expire_records[j:j+PER_CALL],
                            template="(%s, %s, %s, %s)",
                            page_size=1000,
                        )

                # 2. Insert the new rows.
                for j in range(0, len(insert_records), PER_CALL):
                    execute_values(
                        cur, insert_sql,
                        insert_records[j:j+PER_CALL],
                        template="(%s, %s, %s, %s, %s, %s)",
                        page_size=1000,
                    )

                # Commit per symbol -- bounded TXN, visible progress, resumable.
                conn.commit()
                total_written += len(insert_records)

                if i % 25 == 0 or i == len(symbols):
                    log.info("  [%d/%d] %-12s -> %d rows (running total: %d)",
                             i, len(symbols), sym,
                             len(insert_records), total_written)

        log.info("Wrote %d factor rows to factor_pit (%d symbols).",
                 total_written, len(symbols))
        return total_written

    except Exception as e:
        log.error("Failed to write factors to PIT: %s", e, exc_info=True)
        return total_written

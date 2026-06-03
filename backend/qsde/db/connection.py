"""
Database connection helpers for QSDE.

Provides:
  - get_sync_conn()  → psycopg2 connection for ML training scripts
  - get_sync_engine() → SQLAlchemy engine for pandas operations
  - execute_sql()    → fire-and-forget SQL execution
  - pit_query()      → Point-in-Time correct factor retrieval
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import lru_cache
from typing import Generator, Optional

import pandas as pd
import psycopg2
import psycopg2.extras
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from qsde.config import settings

log = logging.getLogger(__name__)


@lru_cache()
def get_sync_engine() -> Engine:
    """Return a cached SQLAlchemy engine for pandas read_sql / to_sql."""
    return create_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )


@contextmanager
def get_sync_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Context manager for a psycopg2 connection.

    Usage:
        with get_sync_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
    """
    conn = psycopg2.connect(
        settings.database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_sql(sql: str, params: dict | tuple | None = None) -> None:
    """Execute a SQL statement (INSERT, UPDATE, CREATE, etc.)."""
    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)


def read_sql(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Execute a SELECT query and return a pandas DataFrame."""
    engine = get_sync_engine()
    return pd.read_sql(text(sql), engine, params=params)


def upsert_dataframe(
    df: pd.DataFrame,
    table: str,
    conflict_columns: list[str],
    update_columns: Optional[list[str]] = None,
) -> int:
    """
    Upsert a DataFrame into a PostgreSQL table using ON CONFLICT.

    Args:
        df: DataFrame with columns matching the target table.
        table: Target table name.
        conflict_columns: Columns forming the unique constraint.
        update_columns: Columns to update on conflict. If None, all non-conflict columns.

    Returns:
        Number of rows upserted.
    """
    if df.empty:
        return 0

    if update_columns is None:
        update_columns = [c for c in df.columns if c not in conflict_columns]

    columns = list(df.columns)
    placeholders = ", ".join([f"%({c})s" for c in columns])
    col_list = ", ".join(columns)
    conflict_list = ", ".join(conflict_columns)

    if update_columns:
        update_clause = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_columns])
        sql = f"""
            INSERT INTO {table} ({col_list})
            VALUES ({placeholders})
            ON CONFLICT ({conflict_list})
            DO UPDATE SET {update_clause}
        """
    else:
        sql = f"""
            INSERT INTO {table} ({col_list})
            VALUES ({placeholders})
            ON CONFLICT ({conflict_list}) DO NOTHING
        """

    records = df.to_dict("records")
    with get_sync_conn() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_batch(cur, sql, records, page_size=500)

    log.info("Upserted %d rows into %s", len(records), table)
    return len(records)


def pit_query(
    symbol: str,
    as_of_date: str,
    factor_names: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Point-in-Time correct factor retrieval.

    Returns factor values that were known on `as_of_date`, preventing
    lookahead bias. This is the ONLY sanctioned way to retrieve factors
    for backtest or live signal computation.

    Args:
        symbol: Stock ticker (e.g., 'RELIANCE').
        as_of_date: The date to query as of (YYYY-MM-DD).
        factor_names: Optional list of specific factors. If None, all factors.

    Returns:
        DataFrame with columns: factor_name, factor_value, data_source.
    """
    base_sql = """
        SELECT factor_name, factor_value, data_source
        FROM factor_pit
        WHERE symbol = :symbol
          AND as_of_date = :date
          AND valid_from <= :date::timestamp
          AND valid_to > :date::timestamp
    """
    params = {"symbol": symbol, "date": as_of_date}

    if factor_names:
        placeholders = ", ".join([f":f{i}" for i in range(len(factor_names))])
        base_sql += f" AND factor_name IN ({placeholders})"
        for i, fn in enumerate(factor_names):
            params[f"f{i}"] = fn

    return read_sql(base_sql, params)


def check_connection() -> bool:
    """Verify database connectivity. Returns True if connection succeeds."""
    try:
        with get_sync_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            return True
    except Exception as e:
        log.error("Database connection failed: %s", e)
        return False

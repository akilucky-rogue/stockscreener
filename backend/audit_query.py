import logging
import json
import pandas as pd
from qsde.db.connection import read_sql, check_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("audit_query")

def audit():
    log.info("Starting TimescaleDB audit...")
    
    # 1. Check connection
    if not check_connection():
        log.error("Could not connect to database.")
        return
    log.info("Connection successful!")
    
    # 2. Query Table list and Row counts
    tables = [
        "universe",
        "ohlcv",
        "fundamentals",
        "factor_pit",
        "signals",
        "model_runs",
        "bulk_deals",
        "watchlist"
    ]
    
    log.info("=== Table Statistics ===")
    for table in tables:
        try:
            count_df = read_sql(f"SELECT COUNT(*) as count FROM {table}")
            cnt = count_df.iloc[0]["count"]
            log.info(f"Table: {table:<15} | Row Count: {cnt:,}")
        except Exception as e:
            log.warning(f"Could not read table {table}: {e}")
            
    # 3. TimescaleDB hypertable health
    log.info("\n=== TimescaleDB Hypertable Details ===")
    try:
        hypertables = read_sql("SELECT hypertable_schema, hypertable_name, primary_dimension FROM timescaledb_information.hypertables")
        log.info(hypertables.to_string())
    except Exception as e:
        log.warning(f"Could not read timescaledb_information.hypertables: {e}")
        
    # 4. Target Stock details for HINDPETRO
    log.info("\n=== Target Stock Check (HINDPETRO) ===")
    try:
        uni = read_sql("SELECT * FROM universe WHERE symbol = 'HINDPETRO'")
        log.info(f"Universe record:\n{uni.to_string()}")
        
        ohlcv_cnt = read_sql("SELECT COUNT(*) as count, MIN(date) as min_date, MAX(date) as max_date FROM ohlcv WHERE symbol = 'HINDPETRO'")
        log.info(f"OHLCV stats:\n{ohlcv_cnt.to_string()}")
        
        fund = read_sql("SELECT fiscal_date, filing_date, gross_margin, operating_margin, pe_ratio FROM fundamentals WHERE symbol = 'HINDPETRO' ORDER BY fiscal_date DESC")
        log.info(f"Fundamentals:\n{fund.to_string()}")
        
        sig = read_sql("SELECT date, horizon, direction, predicted_return, ranking_score FROM signals WHERE symbol = 'HINDPETRO' ORDER BY date DESC, horizon LIMIT 10")
        log.info(f"Signals:\n{sig.to_string()}")
        
        peers = read_sql("""
            SELECT symbol, company_name, sector FROM universe 
            WHERE sector = (SELECT sector FROM universe WHERE symbol = 'HINDPETRO')
        """)
        log.info(f"Peers in same sector:\n{peers.to_string()}")
    except Exception as e:
        log.error(f"Error checking HINDPETRO: {e}")

if __name__ == "__main__":
    audit()

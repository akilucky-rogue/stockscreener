"""
Database Audit and Diagnostics Script for QSDE.
Runs queries on all tables and prints detailed counts and sample data.
"""
import os
import sys

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qsde.db import read_sql, check_connection

def audit():
    print("=" * 60)
    print("QSDE DATABASE AUDIT")
    print("=" * 60)
    
    connected = check_connection()
    print(f"Database Connected: {connected}")
    if not connected:
        print("Error: Cannot connect to the database. Make sure TimescaleDB is running.")
        return
        
    tables = read_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )
    
    print("\nTable Statistics:")
    print(f"{'Table Name':<30} | {'Row Count':<10}")
    print("-" * 45)
    
    all_tables = tables["table_name"].tolist()
    for table in all_tables:
        try:
            count_df = read_sql(f"SELECT COUNT(*) as cnt FROM {table}")
            cnt = count_df.iloc[0]["cnt"]
            print(f"{table:<30} | {cnt:<10}")
        except Exception as e:
            print(f"{table:<30} | ERROR: {e}")
            
    print("\n" + "=" * 60)
    print("UNIVERSE SAMPLE AND SYMBOL SEARCH")
    print("=" * 60)
    try:
        uni_count = read_sql("SELECT COUNT(*) as cnt FROM universe WHERE is_active = TRUE").iloc[0]["cnt"]
        print(f"Total Active Companies: {uni_count}")
        
        # Check HINDPETRO
        hp = read_sql("SELECT * FROM universe WHERE symbol = 'HINDPETRO'")
        if not hp.empty:
            print("\nHINDPETRO Universe Record:")
            for col in hp.columns:
                print(f"  {col}: {hp.iloc[0][col]}")
        else:
            print("\n[WARNING] HINDPETRO NOT found in universe table!")
            
            print("\nActive symbols in universe (first 10):")
            sample = read_sql("SELECT symbol, company_name, sector FROM universe WHERE is_active = TRUE LIMIT 10")
            print(sample)
    except Exception as e:
        print(f"Error reading universe: {e}")

    print("\n" + "=" * 60)
    print("OHLCV DATA CHECK")
    print("=" * 60)
    try:
        hp_ohlcv = read_sql("SELECT COUNT(*) as cnt FROM ohlcv WHERE symbol = 'HINDPETRO'")
        cnt = hp_ohlcv.iloc[0]["cnt"]
        print(f"HINDPETRO OHLCV Row Count: {cnt}")
        if cnt > 0:
            latest = read_sql("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = 'HINDPETRO' ORDER BY date DESC LIMIT 5")
            print("\nLatest OHLCV for HINDPETRO:")
            print(latest)
    except Exception as e:
        print(f"Error checking OHLCV: {e}")

    print("\n" + "=" * 60)
    print("FUNDAMENTALS DATA CHECK")
    print("=" * 60)
    try:
        hp_fund = read_sql("SELECT COUNT(*) as cnt FROM fundamentals WHERE symbol = 'HINDPETRO'")
        cnt = hp_fund.iloc[0]["cnt"]
        print(f"HINDPETRO Fundamentals Row Count: {cnt}")
        if cnt > 0:
            latest = read_sql("SELECT symbol, fiscal_date, revenue, net_income, pe_ratio, pb_ratio, roe, roic FROM fundamentals WHERE symbol = 'HINDPETRO' ORDER BY fiscal_date DESC LIMIT 5")
            print("\nLatest Fundamentals for HINDPETRO:")
            print(latest)
    except Exception as e:
        print(f"Error checking fundamentals: {e}")

    print("\n" + "=" * 60)
    print("SIGNALS DATA CHECK")
    print("=" * 60)
    try:
        sig_count = read_sql("SELECT COUNT(*) as cnt FROM signals").iloc[0]["cnt"]
        print(f"Total Signals: {sig_count}")
        if sig_count > 0:
            sample = read_sql("SELECT * FROM signals LIMIT 5")
            print(sample)
    except Exception as e:
        print(f"Error checking signals: {e}")

if __name__ == "__main__":
    audit()

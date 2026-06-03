"""
Test Research Engine Endpoints for HINDPETRO.
"""
import os
import sys
import json

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qsde.research.comps_engine import build_comps_analysis
from qsde.research.dcf_engine import build_dcf_valuation
from qsde.research.earnings_engine import build_earnings_snapshot

def test_symbol(symbol):
    print(f"Testing Research Engines for {symbol}...")
    
    print("\n--- Comparable Company Analysis ---")
    try:
        comps = build_comps_analysis(symbol)
        print("Keys returned:", list(comps.keys()))
        if "error" in comps:
            print("ERROR returned:", comps["error"])
        else:
            print(f"Peers found: {comps.get('peer_count')}")
            print(f"Positioning stats: {list(comps.get('positioning', {}).keys())}")
            print(f"Quartiles valuation stats: {list(comps.get('valuation_stats', {}).keys())}")
    except Exception as e:
        print("EXCEPTION in comps:", e)
        import traceback
        traceback.print_exc()

    print("\n--- DCF Valuation ---")
    try:
        dcf = build_dcf_valuation(symbol, "base")
        print("Keys returned:", list(dcf.keys()))
        if "error" in dcf:
            print("ERROR returned:", dcf["error"])
        else:
            print(f"Implied Price: {dcf.get('equity_bridge', {}).get('implied_price')}")
            print(f"Current Price: {dcf.get('equity_bridge', {}).get('current_price')}")
            print(f"WACC: {dcf.get('wacc', {}).get('wacc')}")
            print(f"Projections length: {len(dcf.get('projections', []))}")
    except Exception as e:
        print("EXCEPTION in DCF:", e)
        import traceback
        traceback.print_exc()

    print("\n--- Earnings Snapshot ---")
    try:
        earnings = build_earnings_snapshot(symbol)
        print("Keys returned:", list(earnings.keys()))
        if "error" in earnings:
            print("ERROR returned:", earnings["error"])
        else:
            print(f"Available: {earnings.get('available')}")
            print(f"Beat/Miss results keys: {list(earnings.get('beat_miss', {}).get('results', {}).keys()) if earnings.get('beat_miss') else 'N/A'}")
    except Exception as e:
        print("EXCEPTION in Earnings:", e)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_symbol("HINDPETRO")

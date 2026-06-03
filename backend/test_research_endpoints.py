import logging
import sys
import json
from qsde.research.comps_engine import build_comps_analysis
from qsde.research.dcf_engine import build_dcf_valuation
from qsde.research.earnings_engine import build_earnings_snapshot

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("test_research")

def test():
    symbol = "HINDPETRO"
    log.info(f"Testing research engines for {symbol}...")
    
    # 1. Comps Analysis
    try:
        log.info("--- Testing Comps Analysis ---")
        comps = build_comps_analysis(symbol)
        if "error" in comps:
            log.warning(f"Comps returned error: {comps['error']}")
        else:
            log.info(f"Comps output successfully! Keys: {list(comps.keys())}")
            log.info(f"Peers found: {len(comps.get('peers', []))}")
            log.info(f"Positioning metrics: {list(comps.get('positioning', {}).keys())}")
    except Exception as e:
        log.exception("Comps Engine crashed:")

    # 2. DCF Valuation
    try:
        log.info("--- Testing DCF Valuation ---")
        dcf = build_dcf_valuation(symbol, "base")
        if "error" in dcf:
            log.warning(f"DCF returned error: {dcf['error']}")
        else:
            log.info(f"DCF output successfully! Keys: {list(dcf.keys())}")
            log.info(f"WACC: {dcf.get('wacc', {}).get('wacc')}")
            log.info(f"Implied Price: {dcf.get('equity_bridge', {}).get('implied_price')}")
            log.info(f"Current Price: {dcf.get('equity_bridge', {}).get('current_price')}")
    except Exception as e:
        log.exception("DCF Engine crashed:")

    # 3. Earnings Snapshot
    try:
        log.info("--- Testing Earnings Snapshot ---")
        earnings = build_earnings_snapshot(symbol)
        if "error" in earnings:
            log.warning(f"Earnings returned error: {earnings['error']}")
        else:
            log.info(f"Earnings output successfully! Keys: {list(earnings.keys())}")
            log.info(f"Available: {earnings.get('available')}")
            if earnings.get('available'):
                log.info(f"Margins trends: {list(earnings.get('margin_trends', {}).keys())}")
    except Exception as e:
        log.exception("Earnings Engine crashed:")

if __name__ == "__main__":
    test()

"""
Print detailed JSON structures of HINDPETRO research API outputs
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qsde.research.comps_engine import build_comps_analysis
from qsde.research.dcf_engine import build_dcf_valuation
from qsde.research.earnings_engine import build_earnings_snapshot

def dump_data(name, data):
    print("=" * 60)
    print(f"JSON FOR: {name}")
    print("=" * 60)
    
    # We serialize and deserialize to simulate HTTP boundary
    serialized = json.dumps(data, default=str)
    loaded = json.loads(serialized)
    
    # Print target info and peers/projections sample
    if "target" in loaded:
        print("Target Company Data Types:")
        for k, v in loaded["target"].items():
            print(f"  {k}: {repr(v)} ({type(v).__name__})")
            
    if "peers" in loaded and loaded["peers"]:
        print("\nFirst Peer Data Types:")
        for k, v in loaded["peers"][0].items():
            print(f"  {k}: {repr(v)} ({type(v).__name__})")
            
    if "equity_bridge" in loaded:
        print("\nEquity Bridge:")
        for k, v in loaded["equity_bridge"].items():
            print(f"  {k}: {repr(v)} ({type(v).__name__})")
            
    if "wacc" in loaded:
        print("\nWACC:")
        for k, v in loaded["wacc"].items():
            print(f"  {k}: {repr(v)} ({type(v).__name__})")

    if "beat_miss" in loaded:
        print("\nBeat Miss:")
        print(json.dumps(loaded["beat_miss"], indent=2))

if __name__ == "__main__":
    dump_data("COMPS", build_comps_analysis("HINDPETRO"))
    dump_data("DCF", build_dcf_valuation("HINDPETRO", "base"))

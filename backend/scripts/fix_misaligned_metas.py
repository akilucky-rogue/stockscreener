"""One-shot: roll back swing & long meta-models that were overwritten
by the previous (now-fixed) ungated train_meta_model.

After the last retrain:
  weights/lgbm_swing.txt   = OLD primary (still serving, DSR<0.95 candidate rejected)
  weights/meta_swing.txt   = NEW meta trained on the REJECTED candidate's OOF preds

When /api/analyze runs swing it feeds the OLD primary's prediction into a meta
that was calibrated for a different primary → miscalibrated Score.

The right action: remove the misaligned active meta files so meta_predict
returns None and the API falls back to the magnitude-score confidence
(which is at least horizon-consistent with the active primary). Intraday
is untouched because its primary DID promote — the meta there is aligned.

Run this ONCE after pulling the meta-promotion-gate fix.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WEIGHTS = Path(__file__).resolve().parents[1] / "qsde" / "models" / "weights"

# Edit this list if a different horizon's primary was also rejected in the
# most recent retrain. The honest test is: did `weights/lgbm_{horizon}.txt`
# get re-saved in the last run? If no, the meta is misaligned and we drop it.
MISALIGNED = ("swing", "long")


def main() -> None:
    print(f"Weights dir: {WEIGHTS}")
    for h in MISALIGNED:
        active = WEIGHTS / f"meta_{h}.txt"
        backup = WEIGHTS / f"meta_{h}.txt.misaligned"
        if active.exists():
            active.rename(backup)
            print(f"  moved meta_{h}.txt -> meta_{h}.txt.misaligned "
                  f"(API will fall back to magnitude-score for {h})")
        else:
            print(f"  meta_{h}.txt not present — nothing to do")
    print()
    print("Active meta-models after rollback:")
    for f in sorted(WEIGHTS.glob("meta_*.txt")):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()

import gc
import logging
import sys

from qsde.models.dataset import build_training_dataset
from qsde.models.lgbm_model import train_lightgbm_model
from qsde.models.signal_generator import generate_signals

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _process_horizon(horizon: str) -> None:
    """Build → train → generate for one horizon, all locals scoped to this
    function so they're collected as soon as it returns. Without this the
    intraday dataset (~800k rows × 120 features ≈ 400 MB at float32) stays
    pinned in memory while swing rebuilds, blowing the heap on 16 GB boxes.
    """
    log.info("--- Processing %s Horizon ---", horizon.upper())

    log.info("1. Building Dataset")
    dataset = build_training_dataset(horizon=horizon)
    if dataset.empty:
        log.warning("Failed to build dataset for %s.", horizon)
        return

    log.info("2. Training Model")
    model = train_lightgbm_model(dataset, horizon=horizon)
    if not model:
        log.warning("Failed to train model for %s.", horizon)
        return

    # The trained model is now persisted to disk by lgbm_model.py.
    # Drop the in-memory copies before the costly signal-generation step.
    del dataset, model
    gc.collect()

    log.info("3. Generating Signals")
    signals = generate_signals(horizon=horizon)
    log.info("Generated %s signals for %s.", signals, horizon)


def main():
    log.info("--- Starting QSDE LightGBM Pipeline ---")
    for horizon in ("intraday", "swing", "long"):
        try:
            _process_horizon(horizon)
        except MemoryError:
            log.error("OOM on %s horizon — process aborted. Try reducing "
                      "QSDE_BACKFILL_YEARS or running horizons separately.",
                      horizon)
            sys.exit(1)
        finally:
            # Hard GC between horizons so each starts clean.
            gc.collect()
    log.info("--- QSDE LightGBM Pipeline Complete ---")


if __name__ == "__main__":
    main()

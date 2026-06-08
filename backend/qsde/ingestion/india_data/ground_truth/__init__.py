"""Price ground-truth checks against official exchange data.

Compare what's stored in our `ohlcv` table (Kite-sourced) vs what the
exchange publishes officially (NSE bhavcopy / BSE daily report). Any
divergence > tolerance is a signal that either Kite's adjustment is off,
or we've ingested a corporate-action-affected price without the corresponding
adjustment.
"""

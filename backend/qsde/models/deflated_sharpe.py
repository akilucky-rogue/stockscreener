"""
Deflated Sharpe Ratio & Probabilistic Sharpe Ratio.

Implements Lopez de Prado (2018) Ch. 14 formulas. DSR is the ONLY
promotion metric per Blueprint principle #4. Raw Sharpe is diagnostic.

Mathematical correction from Blueprint §9.2:
  A Sharpe of 1.8 after 100 trials does NOT deflate to ~1.1.
  It deflates to ~0.39 under proper application.
"""

from __future__ import annotations

import logging
import os

import numpy as np
from scipy.stats import norm

log = logging.getLogger(__name__)


def sharpe_ratio(
    returns: np.ndarray,
    risk_free_rate: float = 0.0,
    annualization: int = 252,
) -> float:
    """
    Compute annualized Sharpe Ratio.

    Args:
        returns: Array of periodic returns.
        risk_free_rate: Annual risk-free rate (default 0).
        annualization: Trading days per year (252 for daily).

    Returns:
        Annualized Sharpe Ratio.
    """
    excess = returns - risk_free_rate / annualization
    if np.std(excess) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(excess) * np.sqrt(annualization))


def sharpe_std_error(
    observed_sharpe: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """
    Standard error of the Sharpe Ratio estimate (Lo 2002).

    Accounts for non-normality through skewness and excess kurtosis.

    Args:
        observed_sharpe: The measured Sharpe ratio.
        n_obs: Number of observations.
        skew: Skewness of the return series.
        kurtosis: Kurtosis of the return series (3.0 = normal).

    Returns:
        Standard error of the Sharpe estimate.
    """
    if n_obs <= 1:
        return float("inf")

    sr = observed_sharpe
    excess_kurt = kurtosis - 3.0  # excess kurtosis

    var_sr = (
        1
        + 0.5 * sr**2
        - skew * sr
        + (excess_kurt / 4) * sr**2
    ) / (n_obs - 1)

    return float(np.sqrt(max(var_sr, 0)))


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    annualization: int = 252,
) -> float:
    """
    Probabilistic Sharpe Ratio — P(true Sharpe > benchmark | observed).

    A strategy with PSR < 0.95 against zero benchmark should NOT be
    deployed (Blueprint §9.2).

    Args:
        observed_sharpe: The measured Sharpe ratio.
        benchmark_sharpe: The benchmark Sharpe to beat (typically 0).
        n_obs: Number of return observations.
        skew: Skewness of returns.
        kurtosis: Kurtosis of returns.
        annualization: Trading days per year (default 252).

    Returns:
        Probability that true Sharpe exceeds benchmark (0 to 1).
    """
    # Convert annualized SR to per-period (daily) to compute correct standard error
    daily_sr = observed_sharpe / np.sqrt(annualization)
    daily_bench = benchmark_sharpe / np.sqrt(annualization)

    se = sharpe_std_error(daily_sr, n_obs, skew, kurtosis)
    if se == 0 or se == float("inf"):
        return 0.0

    z = (daily_sr - daily_bench) / se
    return float(norm.cdf(z))


def expected_max_sharpe_under_null(
    n_trials: int,
    se_sr: float,
) -> float:
    """
    Expected maximum Sharpe Ratio across N independent trials under the null
    hypothesis that all strategies have zero true Sharpe.

    Bailey & Lopez de Prado (2014) "The Deflated Sharpe Ratio". The Z-score
    of the maximum of N standard normal draws is approximated by

        E[max Z] ≈ sqrt(2 ln N) · (1 − γ / (2 ln N)) + γ / sqrt(2 ln N)

    where γ ≈ 0.5772 is the Euler–Mascheroni constant. The Z-score is then
    rescaled by SE(SR_estimator) so the result is in the same units as the
    observed Sharpe (both annualized in our pipeline).

    Args:
        n_trials: Number of independent trials.
        se_sr: Standard error of the per-trial Sharpe estimator.

    Returns:
        Expected maximum SR under the null, in the same units as the input SE.
    """
    if n_trials <= 1:
        return 0.0

    log_n = np.log(n_trials)
    if log_n <= 0:
        return 0.0

    gamma = 0.5772156649
    sqrt_2logn = np.sqrt(2 * log_n)
    expected_max_z = sqrt_2logn * (1 - gamma / (2 * log_n)) + gamma / sqrt_2logn
    return float(expected_max_z * se_sr)


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    annualization: int = 252,
) -> float:
    """
    Deflated Sharpe Ratio — probability that the true Sharpe exceeds the
    expected maximum Sharpe of N independent strategies under the null.

    Bailey & Lopez de Prado (2014). DSR is reported as a probability in
    [0, 1]; promotion threshold is conventionally 0.95.

    Implementation note (audit fix, 2026-05-10): The variance of the Sharpe
    ratio estimator requires the per-period (unannualized) Sharpe ratio. We
    convert the annualized observed_sharpe to per-period, compute the expected
    maximum per-period SR under the null, and compute the PSR.
    """
    if n_trials <= 1:
        return probabilistic_sharpe_ratio(observed_sharpe, 0.0, n_obs, skew, kurtosis, annualization)

    # Convert to per-period (daily) Sharpe ratio for variance calculations
    daily_sr = observed_sharpe / np.sqrt(annualization)

    # Standard error of the daily Sharpe ratio
    se_daily = sharpe_std_error(daily_sr, n_obs, skew, kurtosis)

    # Expected max daily Sharpe under null
    exp_max_daily = expected_max_sharpe_under_null(n_trials, se_daily)

    # Compute probabilistic sharpe ratio on the DAILY units
    # (Since we pass the daily SR and benchmark, annualization=1 inside PSR)
    dsr = probabilistic_sharpe_ratio(
        daily_sr, exp_max_daily, n_obs, skew, kurtosis, annualization=1
    )

    log.info(
        "DSR: observed_ann=%.3f, n_trials=%d, SE_daily=%.4f, "
        "exp_max_daily=%.3f, DSR=%.4f",
        observed_sharpe, n_trials, se_daily, exp_max_daily, dsr,
    )

    return dsr


def validate_strategy(
    returns: np.ndarray,
    n_trials: int = 1,
    risk_free_rate: float = 0.0,
) -> dict:
    """
    Full strategy validation suite.

    Returns dict with: sharpe, dsr, psr, skew, kurtosis, n_obs, pass/fail.
    A strategy passes if DSR > 0.95 and PSR > 0.95.
    """
    from scipy.stats import skew as calc_skew, kurtosis as calc_kurt

    n_obs = len(returns)
    sr_ann = sharpe_ratio(returns, risk_free_rate, annualization=252)
    sk = float(calc_skew(returns)) if n_obs > 0 else 0.0
    kt = float(calc_kurt(returns, fisher=False)) if n_obs > 0 else 3.0

    psr = probabilistic_sharpe_ratio(sr_ann, 0.0, n_obs, sk, kt, annualization=252)
    dsr = deflated_sharpe_ratio(sr_ann, n_trials, n_obs, sk, kt, annualization=252)

    return {
        "sharpe": round(sr_ann, 4),
        "deflated_sharpe": round(dsr, 4),
        "psr": round(psr, 4),
        "skewness": round(sk, 4),
        "kurtosis": round(kt, 4),
        "n_observations": n_obs,
        "n_trials": n_trials,
        "pass_dsr": dsr > 0.95,   # DSR is a probability, > 0.95 = passes
        "pass_psr": psr > 0.95,
    }


DSR_PROMOTION_THRESHOLD = 0.95


def should_promote(dsr, threshold=None, force=None) -> dict:
    """Model promotion decision — DSR is the ONLY promotion metric (Blueprint #2).

    Promote iff DSR >= threshold. `threshold` defaults to env
    QSDE_DSR_PROMOTION_THRESHOLD (else 0.95). A dev escape hatch,
    QSDE_FORCE_PROMOTE=true, promotes regardless (so a model can be produced on
    small / synthetic data where DSR can't realistically clear 0.95) — recorded
    as forced=True for the audit trail.

    HARD PAUSE: env QSDE_ML_PROMOTION_ENABLED=false short-circuits and refuses
    promotion regardless of DSR or force. Used during the Tier 1 rule-engine
    validation window (target: 30+ paper sessions) so ML and rule strategies
    can be compared on clean, non-overlapping promotion timelines. The current
    active model keeps producing signals; only PROMOTIONS are frozen.

    Returns {promote, passed, forced, threshold, reason}.
    """
    # ── Hard pause check (overrides everything, including FORCE_PROMOTE) ──
    ml_enabled_raw = os.getenv("QSDE_ML_PROMOTION_ENABLED", "true").strip().lower()
    ml_enabled = ml_enabled_raw in ("1", "true", "yes", "on", "y")
    if not ml_enabled:
        # Still compute DSR diagnostic for the audit log — caller may log it.
        d = float(dsr) if dsr is not None else 0.0
        return {
            "promote": False,
            "passed": False,
            "forced": False,
            "paused": True,
            "threshold": float(threshold) if threshold is not None else DSR_PROMOTION_THRESHOLD,
            "reason": (
                f"ML promotion PAUSED (QSDE_ML_PROMOTION_ENABLED={ml_enabled_raw!r}). "
                f"DSR diagnostic only: {d:.4f}. Active model unchanged. "
                "Unset or set =true to resume."
            ),
        }

    if threshold is None:
        try:
            threshold = float(os.getenv("QSDE_DSR_PROMOTION_THRESHOLD", DSR_PROMOTION_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DSR_PROMOTION_THRESHOLD
    if force is None:
        force = os.getenv("QSDE_FORCE_PROMOTE", "false").strip().lower() in ("1", "true", "yes", "on")

    d = float(dsr) if dsr is not None else 0.0
    passed = d >= threshold
    forced = bool(force and not passed)
    promote = bool(passed or force)
    if passed:
        reason = f"DSR {d:.4f} >= {threshold:.2f} -> PROMOTE"
    elif forced:
        reason = f"DSR {d:.4f} < {threshold:.2f} but FORCE-PROMOTED (QSDE_FORCE_PROMOTE)"
    else:
        reason = f"DSR {d:.4f} < {threshold:.2f} -> NOT promoted (active model unchanged)"
    return {"promote": promote, "passed": passed, "forced": forced,
            "paused": False,
            "threshold": float(threshold), "reason": reason}

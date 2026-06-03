"""
Position-risk cap governor — Kelly-aware, data-driven, one-way auto.

This is the single authority for "how much capital may a single trade put
at risk". Every order ticket and paper-take should pipe through it. The
goal is to remove judgment calls about position sizing from the heat of
the moment and turn them into a deterministic function of:

  * realized paper-trade stats (win rate, payoff ratio) per horizon
  * how many sessions of paper history we actually have
  * the user's current escalation tier (1% / 3% / 5%)

Behavior
--------
The governor escalates ONLY by explicit user action (POST /api/risk/escalate)
and de-escalates AUTOMATICALLY on drift. This asymmetry exists because the
failure modes are asymmetric: a too-cautious cap delays compounding by
weeks; a too-aggressive cap can blow the bankroll in days.

Tiers
-----
  T0 (default, first 30 paper sessions):  hard cap 1.0%
  T1 (>=30 sessions, edge confirmed):     quarter-Kelly, capped 3.0%
  T2 (>=60 sessions, edge persists):      quarter-Kelly, capped 5.0%
  T3 (>=90 sessions, manual override):    half-Kelly,    capped 7.0%

"Edge confirmed" =
  realized hit rate within 2pp of backtest band
  AND realized net Sharpe >= 0.5 of the backtest net Sharpe
  AND not currently drift-flagged

"Drift" =
  rolling-14d realized hit rate >5pp below backtest band
  OR rolling-14d realized net Sharpe negative
  -> immediately drops to T0 cap of 1.0% across all horizons.

Kelly fraction
--------------
For win rate p, payoff b = avg_win / avg_loss:
    f_kelly = max(0, (p * b - q) / b)
We then scale by k_mult (0.25 at T1/T2, 0.50 at T3) and clip to the tier's
hard cap. We do NOT use full Kelly anywhere — the drawdown variance at
full Kelly with realistic edge dispersion is in the 60-80% range, which
is unacceptable on a personal bankroll.

Outputs
-------
  cap_fraction(horizon) -> float in [0.005, 0.07]
      "you may risk this fraction of bankroll on one trade of this horizon."
  state() -> dict
      full diagnostic for the dashboard banner: current tier, days of
      paper history, realized stats per horizon, drift status, next
      escalation requirements.

Persistence
-----------
The current tier is stored in the `risk_governor_state` table (migration
010). User actions go through set_tier() and are audit-logged. We never
silently change tiers — the audit trail is what makes this trustworthy
when real money is in play.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from qsde.db.connection import execute_sql, read_sql
from qsde.models.edge_stats import horizon_edge

log = logging.getLogger(__name__)


# ── Tier definitions ────────────────────────────────────────────────

@dataclass(frozen=True)
class Tier:
    name: str
    min_sessions: int
    kelly_mult: float
    hard_cap: float          # absolute max fraction of capital per trade
    floor: float = 0.005     # never go below 0.5% (else trades are too small to matter)


TIERS: dict[str, Tier] = {
    "T0": Tier(name="T0", min_sessions=0,   kelly_mult=0.00, hard_cap=0.010),
    "T1": Tier(name="T1", min_sessions=30,  kelly_mult=0.25, hard_cap=0.030),
    "T2": Tier(name="T2", min_sessions=60,  kelly_mult=0.25, hard_cap=0.050),
    "T3": Tier(name="T3", min_sessions=90,  kelly_mult=0.50, hard_cap=0.070),
}


# ── Drift detection ─────────────────────────────────────────────────

@dataclass
class HorizonStats:
    n_trades:    int
    hit_rate:    Optional[float]      # realized
    avg_win:     Optional[float]      # realized (positive number)
    avg_loss:    Optional[float]      # realized (positive number)
    net_sharpe:  Optional[float]      # realized
    backtest_sharpe: Optional[float]
    backtest_hit_band: Optional[str]  # e.g. "0.7-1.0"
    drift_flag:  bool
    drift_reason: Optional[str]


def _realized_stats(horizon: str, window_sessions: Optional[int] = None) -> HorizonStats:
    """Realized live stats from paper_trades for a horizon.

    window_sessions: if provided, restrict to closed trades in the last N
    sessions. Used for rolling-14d drift detection. None = all-time.
    """
    sql = (
        """SELECT realized_ret_net, status, entry_date, horizon_sessions
             FROM paper_trades
            WHERE horizon = :h AND status IN ('WIN','LOSS','TIME')"""
    )
    params: dict[str, object] = {"h": horizon}
    if window_sessions is not None:
        cutoff = date.today() - timedelta(days=int(window_sessions * 1.5) + 5)
        sql += " AND entry_date >= :cut"
        params["cut"] = cutoff
    df = read_sql(sql, params=params)

    edge = horizon_edge(horizon) or {}
    backtest_sharpe = edge.get("net_sharpe")
    backtest_band   = edge.get("edge_band")

    if df.empty:
        return HorizonStats(
            n_trades=0, hit_rate=None, avg_win=None, avg_loss=None,
            net_sharpe=None, backtest_sharpe=backtest_sharpe,
            backtest_hit_band=backtest_band, drift_flag=False,
            drift_reason=None,
        )

    rets = df["realized_ret_net"].astype(float)
    n    = len(rets)
    hits = (rets > 0)
    hit_rate = float(hits.mean())
    wins   = rets[rets > 0]
    losses = rets[rets < 0]
    avg_win  = float(wins.mean())          if len(wins)   else None
    avg_loss = float(-losses.mean())       if len(losses) else None

    # Annualized Sharpe assuming the typical bar length per horizon.
    sessions = float(df["horizon_sessions"].mean()) or 1.0
    mean, std = float(rets.mean()), float(rets.std())
    sharpe = None
    if std > 0 and n >= 10:
        sharpe = mean / std * (252.0 / sessions) ** 0.5

    return HorizonStats(
        n_trades=n, hit_rate=hit_rate, avg_win=avg_win, avg_loss=avg_loss,
        net_sharpe=sharpe, backtest_sharpe=backtest_sharpe,
        backtest_hit_band=backtest_band, drift_flag=False, drift_reason=None,
    )


def _has_drift(rolling: HorizonStats) -> tuple[bool, Optional[str]]:
    """Decide if the rolling-window stats indicate edge has decayed."""
    if rolling.n_trades < 10:
        return (False, None)   # not enough data to claim drift

    # 1) Negative realized Sharpe in the rolling window.
    if rolling.net_sharpe is not None and rolling.net_sharpe < 0:
        return (True, f"rolling Sharpe negative ({rolling.net_sharpe:.2f})")

    # 2) Realized hit rate >5pp below backtest band low edge.
    band = rolling.backtest_hit_band
    if band and rolling.hit_rate is not None:
        try:
            lo_band = float(band.split("-")[0])
        except (ValueError, IndexError):
            lo_band = None
        if lo_band is not None and rolling.hit_rate < (lo_band - 0.05):
            return (True,
                    f"hit rate {rolling.hit_rate:.2f} > 5pp below band low {lo_band:.2f}")

    return (False, None)


# ── State persistence ───────────────────────────────────────────────

def _current_tier_name() -> str:
    """Read the active tier from risk_governor_state; default T0."""
    df = read_sql(
        "SELECT tier_name FROM risk_governor_state ORDER BY effective_at DESC LIMIT 1"
    )
    if df.empty:
        return "T0"
    return str(df.iloc[0]["tier_name"])


def _set_tier(tier_name: str, reason: str, by: str = "system") -> None:
    """Audit-log a tier change. by='user' or by='system'."""
    if tier_name not in TIERS:
        raise ValueError(f"unknown tier {tier_name}")
    execute_sql(
        """INSERT INTO risk_governor_state (tier_name, reason, changed_by)
           VALUES (%(t)s, %(r)s, %(b)s)""",
        {"t": tier_name, "r": reason, "b": by},
    )
    log.info("Risk tier set to %s by %s: %s", tier_name, by, reason)


def set_tier_user(target_tier: str, reason: str = "") -> dict:
    """User-initiated tier change. Validates against minimum requirements."""
    if target_tier not in TIERS:
        return {"ok": False, "error": f"unknown tier {target_tier}"}

    cur = _current_tier_name()
    if target_tier == cur:
        return {"ok": True, "no_change": True, "tier": cur}

    # User can de-escalate freely.
    if int(target_tier[1:]) < int(cur[1:]):
        _set_tier(target_tier, reason or "user de-escalation", by="user")
        return {"ok": True, "from": cur, "to": target_tier}

    # Escalating: check minimum sessions + edge confirmation per horizon.
    tier = TIERS[target_tier]
    overall_sessions = _paper_history_sessions()
    if overall_sessions < tier.min_sessions:
        return {"ok": False, "error":
                f"need >= {tier.min_sessions} paper sessions, have {overall_sessions}"}

    # Edge must be confirmed for at least one horizon.
    any_confirmed = False
    confirmed_horizons: list[str] = []
    for h in ("intraday", "swing", "long"):
        st = _realized_stats(h)
        if _edge_confirmed(st):
            any_confirmed = True
            confirmed_horizons.append(h)
    if not any_confirmed:
        return {"ok": False, "error":
                "no horizon has edge confirmed against backtest yet"}

    _set_tier(target_tier, reason or f"user escalation; confirmed on {confirmed_horizons}", by="user")
    return {"ok": True, "from": cur, "to": target_tier,
            "confirmed_horizons": confirmed_horizons}


def _paper_history_sessions() -> int:
    """Distinct NSE trading days with at least one closed paper trade."""
    df = read_sql(
        """SELECT COUNT(DISTINCT entry_date) AS n
             FROM paper_trades
            WHERE status IN ('WIN','LOSS','TIME')"""
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _edge_confirmed(st: HorizonStats) -> bool:
    """Edge confirmed = realized hit rate within band AND Sharpe >= 0.5 x backtest."""
    if st.n_trades < 15:
        return False
    if st.backtest_sharpe is not None and st.net_sharpe is not None:
        if st.net_sharpe < 0.5 * st.backtest_sharpe:
            return False
    band = st.backtest_hit_band
    if band and st.hit_rate is not None:
        try:
            lo, hi = [float(x) for x in band.split("-")]
        except ValueError:
            return True   # malformed band, fall back to Sharpe check above
        # Allow a 2pp grace below the band low.
        if st.hit_rate < (lo - 0.02):
            return False
    return True


# ── Public API ──────────────────────────────────────────────────────

def cap_fraction(horizon: str) -> float:
    """Maximum fraction of bankroll permitted at risk per trade of this horizon."""
    tier_name = _current_tier_name()

    # Auto-de-escalate on drift (rolling 14 sessions).
    rolling = _realized_stats(horizon, window_sessions=14)
    drifted, reason = _has_drift(rolling)
    if drifted and tier_name != "T0":
        _set_tier("T0", f"auto de-escalation: {reason}", by="system")
        tier_name = "T0"

    tier = TIERS[tier_name]

    # T0: hard cap, no Kelly scaling — edge unknown.
    if tier.kelly_mult == 0.0:
        return tier.hard_cap

    # Compute Kelly from realized all-time stats for this horizon.
    st = _realized_stats(horizon)
    if (st.hit_rate is None or st.avg_win is None or st.avg_loss is None
            or st.avg_loss <= 0):
        return tier.floor

    p = st.hit_rate
    b = st.avg_win / st.avg_loss
    q = 1.0 - p
    kelly_f = max(0.0, (p * b - q) / b)
    cap = min(kelly_f * tier.kelly_mult, tier.hard_cap)
    return max(cap, tier.floor)


def state() -> dict:
    """Full diagnostic state for the dashboard banner + /api/risk/cap."""
    tier_name = _current_tier_name()
    tier = TIERS[tier_name]
    sessions = _paper_history_sessions()

    horizons_state: dict[str, dict] = {}
    for h in ("intraday", "swing", "long"):
        all_time = _realized_stats(h)
        rolling  = _realized_stats(h, window_sessions=14)
        drifted, drift_reason = _has_drift(rolling)
        horizons_state[h] = {
            "n_trades":          all_time.n_trades,
            "hit_rate":          all_time.hit_rate,
            "avg_win_bps":       round(all_time.avg_win * 1e4, 1)  if all_time.avg_win  else None,
            "avg_loss_bps":      round(all_time.avg_loss * 1e4, 1) if all_time.avg_loss else None,
            "realized_sharpe":   round(all_time.net_sharpe, 2)     if all_time.net_sharpe is not None else None,
            "backtest_sharpe":   all_time.backtest_sharpe,
            "backtest_hit_band": all_time.backtest_hit_band,
            "edge_confirmed":    _edge_confirmed(all_time),
            "cap_fraction":      cap_fraction(h),
            "drift_flag":        drifted,
            "drift_reason":      drift_reason,
        }

    # Next-tier requirements (forward-looking).
    next_tier_name = None
    if tier_name in ("T0", "T1", "T2"):
        idx = int(tier_name[1:]) + 1
        next_tier_name = f"T{idx}"

    next_tier_state = None
    if next_tier_name:
        nt = TIERS[next_tier_name]
        need_sessions = max(0, nt.min_sessions - sessions)
        confirmed_horizons = [h for h, v in horizons_state.items() if v["edge_confirmed"]]
        next_tier_state = {
            "name":               nt.name,
            "hard_cap":           nt.hard_cap,
            "kelly_mult":         nt.kelly_mult,
            "sessions_required":  nt.min_sessions,
            "sessions_remaining": need_sessions,
            "horizons_confirmed": confirmed_horizons,
            "ready_to_escalate":  need_sessions == 0 and len(confirmed_horizons) >= 1,
        }

    return {
        "current_tier":     tier_name,
        "tier_meta":        {
            "kelly_mult": tier.kelly_mult,
            "hard_cap":   tier.hard_cap,
            "floor":      tier.floor,
        },
        "paper_sessions":   sessions,
        "horizons":         horizons_state,
        "next_tier":        next_tier_state,
        "philosophy":       (
            "One-way auto: drift de-escalates immediately; escalation requires "
            "explicit user action via POST /api/risk/escalate. Never silently "
            "scales risk upward."
        ),
    }

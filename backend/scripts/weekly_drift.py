"""
Weekly drift summary — Sunday 18:00 IST.

Reads /api/paper/drift, formats it as a single-page console report you
can eyeball in 30 seconds, writes a snapshot to weekly_reports/ for
audit, and (optionally) POSTs the same JSON to QSDE_DRIFT_WEBHOOK_URL
so you get a Slack/Discord/email alert without checking the dashboard.

The whole thing is read-only. The cap governor handles auto-de-escalation
independently — this script just summarises and notifies.

Usage:
    python backend/scripts/weekly_drift.py
    python backend/scripts/weekly_drift.py --webhook https://hooks.slack.com/...
    python backend/scripts/weekly_drift.py --no-write   # print only

Register as a weekly scheduled task to fire every Sunday at 18:00 IST.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [drift] %(message)s")
log = logging.getLogger("weekly_drift")


_ACTION_GLYPH_UNICODE = {
    "keep":   "✅",
    "shrink": "⚠️ ",
    "stop":   "🛑",
    "wait":   "⏳",
}

_ACTION_GLYPH_ASCII = {
    "keep":   "[OK]",
    "shrink": "[!] ",
    "stop":   "[X] ",
    "wait":   "[..]",
}

# Module-level default; main() may swap this to UNICODE based on --unicode
# or to ASCII based on --ascii (or autodetect).
_ACTION_GLYPH = _ACTION_GLYPH_ASCII

# Other Unicode glyphs used inside the report body — also need the swap so
# the table separators and em-dashes don't garble under cp1252.
_TXT_UNICODE = {"sep": "──", "dash": "—", "warn": "⚠"}
_TXT_ASCII   = {"sep": "--", "dash": "-", "warn": "!"}
_TXT = dict(_TXT_ASCII)

# Unicode characters that crop up from drift_report.py docstrings/summaries
# and the title bullet — replaced wholesale when ASCII mode is on so we
# don't have to chase every hardcoded glyph in upstream modules.
_ASCII_REPLACEMENTS = {
    "·":  ".",
    "—":  "-",
    "–":  "-",
    "…":  "...",
    "✓":  "[OK]",
    "✗":  "[X]",
    "✅": "[OK]",
    "⚠️": "[!]",
    "⚠":  "!",
    "🛑": "[X]",
    "⏳": "[..]",
    "└":  "+",
    "┌":  "+",
    "└─": "+-",
    "├":  "|",
    "│":  "|",
    "─":  "-",
    "↑":  "^",
    "↓":  "v",
}


def _to_ascii(text: str) -> str:
    """Best-effort drop of non-ASCII chars to ASCII equivalents."""
    for u, a in _ASCII_REPLACEMENTS.items():
        text = text.replace(u, a)
    # Last-resort: drop any remaining non-ASCII so cp1252 can't garble.
    return text.encode("ascii", "replace").decode("ascii")


def _f(value, spec: str = ".2f", default: str = "") -> str:
    # Default is set at call time from _TXT['dash'] so it matches the
    # active glyph mode (— in unicode, - in ascii).
    if default == "":
        default = _TXT["dash"]
    """Format a possibly-None numeric value. Stats dicts use None to mean
    'not enough data' (vs 0 which means 'zero'), so we have to dispatch
    on None explicitly — `dict.get(key, default)` returns None if the key
    exists with a None value."""
    if value is None:
        return default
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return default


def _format_report(rep: dict) -> str:
    """Return a single-page text summary suitable for terminal or chat."""
    lines: list[str] = []
    glyph = _ACTION_GLYPH.get((rep.get("action") or "").lower(), "  ")
    lines.append("=" * 70)
    lines.append(f"  QSDE WEEKLY DRIFT REPORT  ·  {rep.get('as_of', date.today())}")
    lines.append("=" * 70)
    lines.append(f"  {glyph} OVERALL: {(rep.get('action') or '').upper()}")
    lines.append(f"     {rep.get('summary', '')}")
    lines.append("")

    def _stats_line(label: str, s: dict) -> str:
        hit = s.get("hit_rate")
        avg = s.get("avg_net_ret_bps")
        shp = s.get("net_sharpe")
        return (
            f"     {label:<12} n={(s.get('n') or 0):>3}  "
            f"win={_f(hit*100 if hit is not None else None, '5.1f')}%  "
            f"avg={_f(avg, '+6.1f')}bps  "
            f"Sharpe={_f(shp, '.2f')}"
        )

    for h in ("intraday", "swing", "long"):
        block = (rep.get("horizons") or {}).get(h, {})
        rec   = block.get("recommendation") or {}
        bt    = block.get("vs_backtest") or {}
        bl    = block.get("vs_baselines") or {}
        m     = bl.get("model")     or {}
        bs    = bl.get("baselines") or {}

        g = _ACTION_GLYPH.get((rec.get("action") or "").lower(), "  ")
        lines.append(f"  {_TXT['sep']} {h.upper():<10} {g} {(rec.get('action') or '').upper():<6}  "
                     f"{rec.get('summary', '')}")
        lines.append(_stats_line("model:", m))
        for strat in ("baseline_top_momentum", "baseline_nifty", "baseline_random"):
            b = bs.get(strat) or {}
            tag = strat.replace("baseline_", "").replace("_", " ")[:11]
            lines.append(_stats_line(tag, b))
        rolling = bt.get("rolling_14d") or {}
        if rolling.get("n", 0) > 0:
            hit = rolling.get("hit_rate")
            lines.append(
                f"     rolling 14d: n={rolling.get('n')}  "
                f"win={_f(hit*100 if hit is not None else None, '.1f')}%  "
                f"vs backtest band {bt.get('backtest_hit_band')}"
            )
        if bt.get("drift_flag"):
            for r in (bt.get("drift_reasons") or []):
                lines.append(f"     {_TXT['warn']} DRIFT: {r}")
        lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def _post_webhook(url: str, rep: dict, text: str) -> None:
    """Best-effort POST. Slack-compatible payload {"text": "..."} —
    Discord webhooks accept the same shape, and most email-to-webhook
    bridges (Pipedream, Zapier) do too."""
    payload = {"text": f"```\n{text}\n```", "report": rep}
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=body,
                             headers={"Content-Type": "application/json"},
                             method="POST")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            log.info("Webhook POST -> %s (%d)", url, resp.status)
    except Exception as e:  # noqa: BLE001
        log.warning("Webhook POST failed: %s", e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.getenv("QSDE_DRIFT_WEBHOOK_URL"),
                    help="Slack/Discord/Pipedream webhook URL.")
    ap.add_argument("--no-write", action="store_true",
                    help="Don't persist a JSON snapshot to weekly_reports/.")
    ap.add_argument("--ascii", action="store_true",
                    help="Use ASCII-only glyphs. Use when host terminal is cp1252.")
    ap.add_argument("--unicode", action="store_true",
                    help="Force Unicode glyphs even if stdout encoding looks cp1252.")
    args = ap.parse_args()

    # Swap glyph tables AFTER argparse — wrapper scripts pass --ascii
    # because PowerShell's Tee-Object mangles UTF-8 output through cp1252
    # regardless of PYTHONUTF8. ASCII is the safe default for any
    # automated invocation.
    global _ACTION_GLYPH, _TXT
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if args.unicode:
        _ACTION_GLYPH = _ACTION_GLYPH_UNICODE
        _TXT          = dict(_TXT_UNICODE)
    elif args.ascii or "utf" not in enc:
        _ACTION_GLYPH = _ACTION_GLYPH_ASCII
        _TXT          = dict(_TXT_ASCII)
    else:
        _ACTION_GLYPH = _ACTION_GLYPH_UNICODE
        _TXT          = dict(_TXT_UNICODE)

    # Direct in-process call so the script works even if the API is down.
    from qsde.execution.drift_report import drift_report
    rep = drift_report()
    text = _format_report(rep)
    # Last-mile: when ASCII mode is on, sanitize ANY remaining unicode that
    # crept in from drift_report's summaries / docstrings so PowerShell's
    # Tee-Object doesn't mangle it through cp1252.
    if _ACTION_GLYPH is _ACTION_GLYPH_ASCII:
        text = _to_ascii(text)
    print(text)

    if not args.no_write:
        out_dir = Path(__file__).resolve().parents[1] / "weekly_reports"
        out_dir.mkdir(exist_ok=True)
        fn = out_dir / f"drift_{date.today().isoformat()}.json"
        fn.write_text(json.dumps(rep, indent=2))
        log.info("Snapshot written -> %s", fn)

    if args.webhook:
        _post_webhook(args.webhook, rep, text)

    # Exit code: 0 = keep/wait, 1 = shrink, 2 = stop.
    # Useful for Task Scheduler to surface drift as a "task failure".
    action = (rep.get("action") or "").lower()
    sys.exit({"stop": 2, "shrink": 1}.get(action, 0))


if __name__ == "__main__":
    main()

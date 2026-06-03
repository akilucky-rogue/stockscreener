"""
Telegram notification module for QSDE.

Bot: @Stoxybot
Sends signal alerts, kill condition warnings, and system status updates.
"""

from __future__ import annotations

import logging
from typing import Optional

from qsde.config import settings

log = logging.getLogger(__name__)


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message to the configured Telegram chat.

    Args:
        text: Message text (supports HTML formatting).
        parse_mode: 'HTML' or 'Markdown'.

    Returns:
        True if sent successfully.
    """
    if not settings.telegram_bot_token:
        log.warning("Telegram bot token not configured")
        return False
    if not settings.telegram_chat_id:
        log.warning("Telegram chat ID not configured")
        return False

    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=parse_mode,
        )
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def send_message_sync(text: str, parse_mode: str = "HTML") -> bool:
    """Synchronous wrapper for send_message."""
    import asyncio
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Create a new thread with its own event loop to run the coroutine
            import threading
            result = []
            
            def run_in_thread():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    res = new_loop.run_until_complete(send_message(text, parse_mode))
                    result.append(res)
                finally:
                    new_loop.close()
                    
            t = threading.Thread(target=run_in_thread)
            t.start()
            t.join(timeout=10)
            return result[0] if result else False
        return asyncio.run(send_message(text, parse_mode))
    except Exception as e:
        log.error("Sync telegram send failed: %s", e)
        return False


def format_signal_alert(
    symbol: str,
    direction: int,
    confidence: float,
    horizon: str,
    top_factors: list[dict],
) -> str:
    """Format a signal change into a Telegram message."""
    arrow = "🟢 BUY" if direction > 0 else "🔴 SELL" if direction < 0 else "⚪ HOLD"
    factors_str = "\n".join(
        f"  • {f['name']}: {f['contribution']:+.3f}" for f in top_factors[:5]
    )
    return (
        f"<b>📊 QSDE Signal Alert</b>\n\n"
        f"<b>{symbol}</b>  {arrow}\n"
        f"Confidence: {confidence:.0%}\n"
        f"Horizon: {horizon}\n\n"
        f"<b>Top Factors:</b>\n{factors_str}"
    )


def format_kill_condition_alert(
    condition_id: int,
    metric_name: str,
    metric_value: float,
    threshold: float,
    triggered: bool,
) -> str:
    """Format a kill condition check into a Telegram message."""
    status = "🚨 TRIGGERED" if triggered else "✅ PASSED"
    return (
        f"<b>⚠️ Kill Condition #{condition_id}</b>\n\n"
        f"Status: {status}\n"
        f"Metric: {metric_name}\n"
        f"Value: {metric_value:.4f}\n"
        f"Threshold: {threshold:.4f}"
    )

"""Finance polarity lexicon — small, deterministic, swap-out friendly.

Each entry is normalized lowercase. Polarity scoring picks +1 for every
positive hit, -1 for every negative hit in the headline, averaged per
headline. Result clipped to [-1, +1] per article, then averaged across
articles per (symbol, date) for the news_sentiment.avg_polarity column.

This is a coarse proxy — production-grade scoring would be FinBERT or
similar transformer model. The schema is unchanged when you upgrade the
scorer, so this is a drop-in replacement target.
"""
from __future__ import annotations

# Positive finance terms — earnings beat, rallies, growth, upgrades.
POSITIVE = frozenset({
    "beat", "beats", "exceeds", "surges", "surge", "rallies", "rally",
    "jumps", "jump", "rises", "rise", "rose", "gains", "gain", "gained",
    "soars", "soar", "climbs", "climb", "advances", "advance",
    "outperforms", "outperform", "outperformed",
    "upgrade", "upgrades", "upgraded",
    "growth", "grew", "growing", "profit", "profits", "profitable",
    "record", "milestone", "high", "highs", "all-time-high",
    "bullish", "bull", "bull-run",
    "buy", "strong-buy", "accumulate", "overweight",
    "expansion", "expands", "expanded",
    "approval", "approved", "wins", "win", "secured", "bagged",
    "dividend", "bonus", "buyback",
})

# Negative finance terms — misses, plunges, downgrades, losses.
NEGATIVE = frozenset({
    "miss", "missed", "misses", "falls", "fall", "fell", "drops", "drop",
    "dropped", "slumps", "slump", "plunges", "plunge", "plunged",
    "tumbles", "tumble", "crashes", "crash",
    "declines", "decline", "declined", "loss", "losses", "losing",
    "downgrade", "downgrades", "downgraded",
    "underperforms", "underperform", "underperformed",
    "bearish", "bear", "bear-market",
    "sell", "strong-sell", "reduce", "underweight",
    "probe", "investigation", "fraud", "scam", "raid",
    "penalty", "fine", "violation", "ban",
    "default", "bankruptcy", "insolvency", "delisting",
    "weak", "weakness", "concerns", "concern", "risk", "risks",
    "warning", "warned", "warns",
    "layoff", "layoffs", "cuts", "cut", "reducing", "shutdown",
})


# Modifiers that flip polarity. Headline scoring multiplies by -1 if
# any of these tokens appear in the same headline as a polarity word.
NEGATIONS = frozenset({"not", "no", "without", "fails", "failed", "failure"})


def score_headline(text: str) -> float:
    """Score a single headline polarity in [-1, +1].

    Tokenization is intentionally simple — split on whitespace + punctuation
    lowercased. False positives ("declined the offer" being negative is
    fine for our purposes; this is a coarse signal, not a parser).
    """
    if not isinstance(text, str) or not text.strip():
        return 0.0

    # Cheap tokenization — alphanum chunks, lowercased.
    tokens = []
    cur = []
    for ch in text.lower():
        if ch.isalnum() or ch == "-":
            cur.append(ch)
        else:
            if cur:
                tokens.append("".join(cur))
                cur = []
    if cur:
        tokens.append("".join(cur))

    pos_hits = sum(1 for t in tokens if t in POSITIVE)
    neg_hits = sum(1 for t in tokens if t in NEGATIVE)
    has_negation = any(t in NEGATIONS for t in tokens)

    if pos_hits == 0 and neg_hits == 0:
        return 0.0

    raw = (pos_hits - neg_hits) / max(pos_hits + neg_hits, 1)
    if has_negation:
        raw = -raw
    # Clip to [-1, +1] (already in range but explicit).
    return max(-1.0, min(1.0, raw))


__all__ = ["POSITIVE", "NEGATIVE", "NEGATIONS", "score_headline"]

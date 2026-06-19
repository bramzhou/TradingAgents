"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re

# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Chinese rating terms (the localized renders emit these) → canonical English.
_RATING_ZH_TO_EN = {
    "买入": "Buy", "增持": "Overweight", "持有": "Hold",
    "减持": "Underweight", "卖出": "Sell",
}

# Matches "Rating: X" / "评级: X" / "Rating: **X**" — tolerates markdown bold,
# a colon/hyphen (ASCII or full-width), and a Chinese or English rating term.
_RATING_LABEL_RE = re.compile(
    r"(?:rating|评级)\s*\**\s*[:：\-]\s*\**\s*([A-Za-z一-鿿]+)",
    re.IGNORECASE,
)


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text (English or Chinese).

    1. Look for an explicit "Rating: X" / "评级: X" label.
    2. Fall back to the first English 5-tier word found anywhere.
    3. Fall back to the first Chinese rating term found anywhere.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m:
            tok = m.group(1)
            if tok.lower() in _RATING_SET:
                return tok.capitalize()
            if tok in _RATING_ZH_TO_EN:
                return _RATING_ZH_TO_EN[tok]

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    for zh, en in _RATING_ZH_TO_EN.items():
        if zh in text:
            return en

    return default

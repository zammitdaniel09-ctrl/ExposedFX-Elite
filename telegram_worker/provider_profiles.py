import re

PROVIDER_PROFILES = {
    # Default gold-heavy topics.
    23: {
        "default_symbol": "XAUUSD",
        "gold_pip_mode": "10pips_1dollar",
        "allow_split_signals": True,
        "allow_breakout_words": True,
        "allow_auto_tp": True,
    },
    33: {
        "default_symbol": "XAUUSD",
        "gold_pip_mode": "10pips_1dollar",
        "allow_split_signals": True,
        "allow_breakout_words": True,
        "allow_auto_tp": True,
    },
    1927: {
        "default_symbol": "XAUUSD",
        "gold_pip_mode": "10pips_1dollar",
        "allow_split_signals": True,
        "allow_breakout_words": True,
        "allow_auto_tp": True,
        "strict_promo_filter": True,
    },
    8587: {
        "default_symbol": "XAUUSD",
        "gold_pip_mode": "10pips_1dollar",
        "allow_split_signals": True,
        "allow_breakout_words": True,
        "allow_auto_tp": True,
    },
}

DEFAULT_PROFILE = {
    "default_symbol": "XAUUSD",
    "gold_pip_mode": "10pips_1dollar",
    "allow_split_signals": True,
    "allow_breakout_words": True,
    "allow_auto_tp": True,
}


PROMO_PATTERNS = [
    r"\binstagram\b",
    r"\bcomment\b.*\brepost\b",
    r"\blike\b.*\bshare\b",
    r"\bfree\s+life[-\s]*time\b",
    r"\blifetime\s+vip\b",
    r"\bvip\s+access\b",
    r"\bjoin\s+now\b",
    r"\blimited\s+spots?\b",
    r"\bgiveaway\b",
    r"\bdm\s+me\b",
    r"\bwho\s+wants\b",
    r"\bnext\s+\d+\s+people\b",
    r"\bcheck\s+my\s+story\b",
    r"\bsubscribe\b",
]


def profile_for_topic(topic_id):
    try:
        return PROVIDER_PROFILES.get(int(topic_id), DEFAULT_PROFILE)
    except Exception:
        return DEFAULT_PROFILE


def is_promo_text(text: str, topic_id=None) -> bool:
    raw = text or ""
    low = raw.lower()

    has_trade_words = bool(
        re.search(r"\b(buy|sell|long|short|entry|sl|stop loss|tp|target|xau|gold|nasdaq|btc|eth)\b", low)
    )

    if has_trade_words:
        return False

    return any(re.search(pat, low) for pat in PROMO_PATTERNS)


def apply_provider_profile(text: str, topic_id=None) -> str:
    raw = text or ""
    profile = profile_for_topic(topic_id)
    default_symbol = profile.get("default_symbol", "")

    # If a provider sends "SELLS BELOW 4458" without symbol, make it explicit.
    if default_symbol:
        has_symbol = bool(
            re.search(r"\b(XAUUSD|XAU/USD|GOLD|NAS100|NASDAQ|US100|BTC|ETH|SOL|[A-Z]{6})\b", raw.upper())
        )
        has_trade = bool(
            re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|SHORT|BELOW|ABOVE)\b", raw.upper())
        )
        if has_trade and not has_symbol:
            return f"{default_symbol}\n{raw}"

    return raw

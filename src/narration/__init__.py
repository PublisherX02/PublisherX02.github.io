"""Narration and plain-English market commentary package for dashboard display."""
from .market_brief import (
    DEFAULT_FEATHERLESS_MODEL,
    MarketBriefResult,
    build_featherless_prompt,
    build_narration_context,
    fetch_sec_edgar_current_ratios,
    generate_market_brief,
    generate_market_brief_sync,
    get_latest_cached_brief,
)

__all__ = [
    "DEFAULT_FEATHERLESS_MODEL",
    "MarketBriefResult",
    "build_featherless_prompt",
    "build_narration_context",
    "fetch_sec_edgar_current_ratios",
    "generate_market_brief",
    "generate_market_brief_sync",
    "get_latest_cached_brief",
]

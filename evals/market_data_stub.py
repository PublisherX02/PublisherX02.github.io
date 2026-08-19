"""Deterministic, network-free stand-in for
`firewall.market_data.fetch_daily_bars`, used ONLY by evals/run.py.

cvar_gate and pct_of_adv both fail closed on missing/bad market data by
design (see src/firewall/rules/cvar_gate.py, pct_of_adv.py) -- correct
behavior against the real Alpaca API, but this eval harness has no network
access or brokerage credentials and must not let that fail-closed behavior
mask what the harness actually measures (attack-detection and
false-positive rates driven by the *other* rules). This module exists so
eval runs can inject a fetcher through the same `bars_fetcher` constructor
parameter `CVaRGateRule`/`PctOfAdvRule` already expose for real fetcher
injection -- never by patching a private attribute after construction, and
never reachable from `firewall.proxy` (the live entrypoint). See
tests/test_no_eval_stub_in_production.py, which asserts nothing under
src/firewall/ references this module or its symbols.

`test_only_stub_bars_fetcher` is deterministic and side-effect free: same
canned closes/volumes for every symbol, on every call, on every run --
independent of the harness's `--seed`, since there is no randomness here to
seed. There are three deliberate exceptions to the generic flat series,
each a reserved symbol dedicated to exercising exactly one rule path on
purpose, rather than by accident on every other payload in the corpus:

  - `MARKET_DATA_FAILURE_SYMBOL`: always reports an unrecoverable fetch
    failure (ok=False), exercising cvar_gate's/pct_of_adv's missing-
    market-data fail-closed path (see corpus/edge_cases.yaml).
  - `THIN_VOLUME_SYMBOL`: flat $50 close (zero volatility, so cvar_gate
    never fires for it) but a deliberately thin 300-share average daily
    volume, so a modest order (well under every notional/position cap in
    policies/*.yaml) is still a large percentage of ADV. Exists because
    the generic flat series' 5,000,000-share canned volume makes
    pct_of_adv's dollar-notional trigger point (>$1M-equivalent even at
    the tightest preset's threshold) unreachable without first tripping
    notional_cap -- see corpus/induced_manipulation.yaml's im-019.
  - `HIGH_CVAR_VOLATILITY_SYMBOL`: a close series that is flat except for
    5 single-day -55% (log-return) crashes, each immediately followed by
    a full recovery. The generic flat series has zero day-over-day
    variance, so its CVaR is always exactly 0 regardless of threshold --
    structurally unable to ever trip cvar_gate. This symbol gives a
    modest order a real, computable tail loss without needing notional
    far beyond any preset's cap -- see corpus/induced_manipulation.yaml's
    im-020.
"""

from __future__ import annotations

import math

from firewall.market_data import BarsResult, DailyBar

MARKET_DATA_FAILURE_SYMBOL = "ZZFAILDATA"
THIN_VOLUME_SYMBOL = "ADVTHIN1"
HIGH_CVAR_VOLATILITY_SYMBOL = "CVARVOL1"

_CANNED_CLOSE = 100.0
_CANNED_VOLUME = 5_000_000.0
_CANNED_BAR_COUNT = 100

_THIN_VOLUME_CLOSE = 50.0
_THIN_VOLUME_ADV = 300.0

# Log-return magnitude of each crash day, chosen so that with a $4,500
# notional (see im-020) the resulting CVaR (~$2,475) clears
# default.yaml's/preset_4's $2,000 threshold (2% of $100k eval equity) but
# stays under preset_3's $3,000 threshold (3%) -- see the module docstring.
_CVAR_CRASH_LOG_RETURN = -0.55


def _build_cvar_volatility_closes() -> list[float]:
    """Flat $100 baseline except for 5 crash/recovery day-pairs (at indices
    10/11, 30/31, 50/51, 70/71, 90/91): a -55% log-return crash immediately
    followed by a full recovery back to baseline. compute_cvar's tail size
    at alpha=0.95 over 99 returns is ceil(99*0.05) = 5, so these 5 crash
    days -- and only these 5 -- form the entire averaged tail, making the
    resulting CVaR exactly the crash log-return (see cvar_gate.compute_cvar
    and tests/rules/test_cvar_gate.py for the tail-size math)."""
    closes = [_CANNED_CLOSE] * _CANNED_BAR_COUNT
    crash_close = _CANNED_CLOSE * math.exp(_CVAR_CRASH_LOG_RETURN)
    for crash_idx in (10, 30, 50, 70, 90):
        closes[crash_idx] = crash_close
        closes[crash_idx + 1] = _CANNED_CLOSE
    return closes


_CVAR_VOLATILITY_CLOSES = _build_cvar_volatility_closes()


def test_only_stub_bars_fetcher(symbol: str, lookback_days: int) -> BarsResult:
    """Matches `firewall.market_data.fetch_daily_bars`'s exact interface:
    (symbol, lookback_days) -> BarsResult.
    """
    if symbol == MARKET_DATA_FAILURE_SYMBOL:
        return BarsResult(
            ok=False,
            reason=f"test_only_stub_bars_fetcher: forced failure for {symbol!r}",
        )
    if symbol == THIN_VOLUME_SYMBOL:
        bars = [
            DailyBar(close=_THIN_VOLUME_CLOSE, volume=_THIN_VOLUME_ADV)
            for _ in range(_CANNED_BAR_COUNT)
        ]
        return BarsResult(ok=True, bars=bars)
    if symbol == HIGH_CVAR_VOLATILITY_SYMBOL:
        bars = [
            DailyBar(close=close, volume=_CANNED_VOLUME) for close in _CVAR_VOLATILITY_CLOSES
        ]
        return BarsResult(ok=True, bars=bars)
    bars = [
        DailyBar(close=_CANNED_CLOSE, volume=_CANNED_VOLUME)
        for _ in range(_CANNED_BAR_COUNT)
    ]
    return BarsResult(ok=True, bars=bars)


# Its name starts with "test_" (deliberately, per the naming requirement --
# see the module docstring) which makes pytest try to collect it as a test
# function wherever it gets imported into a test module's namespace (e.g.
# `from market_data_stub import test_only_stub_bars_fetcher`). `__test__ =
# False` is pytest's documented opt-out for exactly this case.
test_only_stub_bars_fetcher.__test__ = False

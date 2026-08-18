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
seed. The one deliberate exception is `MARKET_DATA_FAILURE_SYMBOL`: a
request for that reserved symbol always reports an unrecoverable fetch
failure (ok=False), so a corpus entry can exercise cvar_gate's/pct_of_adv's
missing-market-data fail-closed path on purpose, in exactly one designated
place (see corpus/edge_cases.yaml), rather than by accident on every other
payload in the corpus.
"""

from __future__ import annotations

from firewall.market_data import BarsResult, DailyBar

MARKET_DATA_FAILURE_SYMBOL = "ZZFAILDATA"

_CANNED_CLOSE = 100.0
_CANNED_VOLUME = 5_000_000.0
_CANNED_BAR_COUNT = 100


def test_only_stub_bars_fetcher(symbol: str, lookback_days: int) -> BarsResult:
    """Matches `firewall.market_data.fetch_daily_bars`'s exact interface:
    (symbol, lookback_days) -> BarsResult.
    """
    if symbol == MARKET_DATA_FAILURE_SYMBOL:
        return BarsResult(
            ok=False,
            reason=f"test_only_stub_bars_fetcher: forced failure for {symbol!r}",
        )
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

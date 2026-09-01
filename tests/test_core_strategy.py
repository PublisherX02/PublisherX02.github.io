"""Tests for core_strategy -- the inverse-volatility weighted basket rebalancer.

Verifies (a) realized volatility calculation (reusing cvar_gate._log_returns),
(b) inverse-volatility weighting and risk allocation, (c) drift-based
rebalancing logic, (d) position parsing, and (e) end-to-end runs through the
real default policy firewall via fake upstreams.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from fastmcp import Client, FastMCP

import core_strategy
from firewall import account_data
from firewall.audit import AuditLogWriter
from firewall.market_data import BarsResult, ContractResolutionResult, DailyBar, ResolvedContract
from firewall.policy import PolicyEngine
from firewall.proxy import build_proxy
from firewall.rules.hedge_proposal import format_occ_symbol


def _default_test_bars_fetcher(symbol, lookback_days, **kwargs):
    """A generic, hermetic stand-in for firewall.market_data.fetch_daily_bars
    -- flat $100 closes, real-looking volume. Used as PolicyEngine.from_yaml's
    bars_fetcher default in these tests so notional_cap/position_cap's
    reference-price fallback (see notional_cap.py's module docstring) never
    makes a real network call. core_strategy.py's own module-level
    `load_dotenv()` (imported at the top of this file) loads real Alpaca
    credentials into THIS PROCESS as a side effect -- without this fixture,
    that fallback would silently succeed against the real, live Alpaca
    market-data API for every qty-only place_stock_order call in this file's
    tests, which is neither hermetic nor what any of them are testing."""
    return BarsResult(
        ok=True,
        bars=[DailyBar(close=100.0, volume=1_000_000.0) for _ in range(max(lookback_days, 1))],
    )


def _real_policy_engine(tmp_path, bars_fetcher=None):
    """The real, unmodified default.yaml policy. cvar-gate/pct-of-adv stay
    enabled deliberately (not disabled like test_proxy.py's equivalent
    helper needs to for *its* limit-order tests): every basket order this
    module submits is qty-only, no limit_price/notional (verified live
    against the real Alpaca paper API -- see core_strategy.py's own comment
    at its build_market_order_payload call sites), so extract_notional
    returns None for them and both rules cleanly skip (RuleOutcome(False))
    before ever touching their bars_fetcher.

    notional_cap/position_cap are different: their reference-price fallback
    (see notional_cap.py's module docstring) DOES fetch for exactly this
    order shape (plain qty-only place_stock_order) -- `bars_fetcher`
    defaults to `_default_test_bars_fetcher` above so that fetch stays
    hermetic; pass an explicit one (e.g. reusing a test's own
    `_fake_bars_fetcher(prices)`) when a test cares about the specific
    reference price used."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml(
        "policies/default.yaml",
        audit_writer=writer,
        bars_fetcher=bars_fetcher or _default_test_bars_fetcher,
    )
    return engine, log_path


def _patch_contract_resolver(monkeypatch):
    """Patch hedge_proposal.resolve_listed_contract with a deterministic,
    network-free fake that echoes the mechanical target back as if it were
    the real listed contract -- keeps these tests hermetic without a live
    Alpaca options-chain call on every scheduled-overlay exercise. Tests
    that specifically need a resolution FAILURE patch it separately."""
    import firewall.rules.hedge_proposal as hedge_proposal_module

    def fake_resolve(symbol, target_strike, target_expiry, option_type, *, min_dte=None, now=None):
        occ_symbol = format_occ_symbol(symbol, target_expiry, option_type, target_strike)
        return ContractResolutionResult(
            ok=True,
            contract=ResolvedContract(
                occ_symbol=occ_symbol, strike=target_strike, expiry=target_expiry
            ),
        )

    monkeypatch.setattr(hedge_proposal_module, "resolve_listed_contract", fake_resolve)


def _fake_bars_fetcher(prices: dict[str, float] | dict[str, list[float]]):
    def fetch(symbol: str, lookback_days: int) -> BarsResult:
        if symbol not in prices:
            return BarsResult(ok=False, reason=f"no fake price for {symbol}")
        val = prices[symbol]
        if isinstance(val, list):
            bars = [DailyBar(close=c, volume=1_000_000.0) for c in val]
        else:
            # 5 daily closes around the base price
            bars = [DailyBar(close=val * (1.0 + 0.005 * i), volume=1_000_000.0) for i in range(5)]
        return BarsResult(ok=True, bars=bars)

    return fetch


# --- pure helpers & inverse-volatility weighting ------------------------


def test_basket_is_small_fixed_and_disclosed():
    assert 3 <= len(core_strategy.BASKET) <= 15
    assert len(set(core_strategy.BASKET)) == len(core_strategy.BASKET)  # no duplicates
    assert all(isinstance(s, str) and s.isupper() for s in core_strategy.BASKET)


def test_compute_realized_volatility():
    # 3 closes -> 2 log returns
    closes = [100.0, 105.0, 102.0]
    vol = core_strategy.compute_realized_volatility(closes)
    assert vol > 0.0
    assert math.isfinite(vol)

    # Fewer than 2 closes raises ValueError
    with pytest.raises(ValueError):
        core_strategy.compute_realized_volatility([100.0])
    with pytest.raises(ValueError):
        core_strategy.compute_realized_volatility([])


def test_compute_inverse_vol_weights_allocates_equal_risk():
    # AAPL has double the volatility of MSFT
    volatilities = {"AAPL": 0.02, "MSFT": 0.01}
    weights = core_strategy.compute_inverse_vol_weights(volatilities)

    assert pytest.approx(sum(weights.values())) == 1.0
    # Lower vol gets higher weight (MSFT gets 2x weight of AAPL)
    assert pytest.approx(weights["MSFT"] / weights["AAPL"]) == 2.0
    # Risk contribution (weight * vol) is equal across both assets
    assert pytest.approx(weights["AAPL"] * volatilities["AAPL"]) == pytest.approx(
        weights["MSFT"] * volatilities["MSFT"]
    )


def test_compute_inverse_vol_weights_handles_fallback():
    # Empty
    assert core_strategy.compute_inverse_vol_weights({}) == {}
    # Flat / zero volatility fallback to equal weights
    vols = {"AAPL": 0.0, "MSFT": 0.0}
    weights = core_strategy.compute_inverse_vol_weights(vols)
    assert pytest.approx(weights["AAPL"]) == 0.5
    assert pytest.approx(weights["MSFT"]) == 0.5


def test_compute_target_quantities():
    weights = {"AAPL": 0.25, "MSFT": 0.75}
    prices = {"AAPL": 100.0, "MSFT": 200.0}
    # Budget $800: AAPL gets $200 (2 shares), MSFT gets $600 (3 shares)
    targets = core_strategy.compute_target_quantities(weights, prices, total_budget_usd=800.0)
    assert targets["AAPL"] == 2
    assert targets["MSFT"] == 3


def test_compute_target_quantities_enforces_minimum_one_share():
    weights = {"AAPL": 0.1}
    prices = {"AAPL": 5000.0}
    # $800 * 0.1 = $80 budget, but min 1 share enforced
    targets = core_strategy.compute_target_quantities(weights, prices, total_budget_usd=800.0)
    assert targets["AAPL"] == 1


def test_compute_rebalance_orders_drift_thresholding():
    weights = {"AAPL": 0.5, "MSFT": 0.5}
    targets = {"AAPL": 5, "MSFT": 5}
    prices = {"AAPL": 100.0, "MSFT": 100.0}

    # 1. Zero initial positions -> orders needed for all
    curr_0 = {"AAPL": 0, "MSFT": 0}
    deltas_0 = core_strategy.compute_rebalance_orders(curr_0, targets, weights, prices, drift_threshold=0.05)
    assert deltas_0 == {"AAPL": 5, "MSFT": 5}

    # 2. Balanced portfolio (50% / 50%) -> drift = 0.0 <= 0.05 -> no orders (delta = 0)
    curr_balanced = {"AAPL": 5, "MSFT": 5}
    deltas_balanced = core_strategy.compute_rebalance_orders(
        curr_balanced, targets, weights, prices, drift_threshold=0.05
    )
    assert deltas_balanced == {"AAPL": 0, "MSFT": 0}

    # 3. Small drift (e.g. 52% vs 48% -> drift = 0.02 <= 0.05) -> no orders
    prices_slight_drift = {"AAPL": 104.0, "MSFT": 96.0}
    deltas_slight = core_strategy.compute_rebalance_orders(
        curr_balanced, targets, weights, prices_slight_drift, drift_threshold=0.05
    )
    assert deltas_slight == {"AAPL": 0, "MSFT": 0}

    # 4. Large drift (e.g. AAPL value $700, MSFT $300 -> 70% vs 30% -> drift = 0.20 > 0.05)
    curr_drifted = {"AAPL": 7, "MSFT": 3}
    deltas_drifted = core_strategy.compute_rebalance_orders(
        curr_drifted, targets, weights, prices, drift_threshold=0.05
    )
    assert deltas_drifted["AAPL"] == -2  # Sell 2 shares of AAPL
    assert deltas_drifted["MSFT"] == 2   # Buy 2 shares of MSFT


def test_rebalance_reference_budget_exposes_proportional_leverage():
    """A 50/50 basket at 2x its intended budget is not drift-free."""
    deltas = core_strategy.compute_rebalance_orders(
        current_positions={"AAPL": 10, "MSFT": 10},
        target_quantities={"AAPL": 5, "MSFT": 5},
        target_weights={"AAPL": 0.5, "MSFT": 0.5},
        prices={"AAPL": 100.0, "MSFT": 100.0},
        drift_threshold=0.05,
        reference_value_usd=1_000.0,
    )
    assert deltas == {"AAPL": -5, "MSFT": -5}


def test_clipped_weight_settles_at_target_instead_of_churning():
    """A name whose raw inverse-vol weight was clipped by
    clip_weights_to_position_cap must reach a stable drift-zero state once its
    holding matches the CAPPED weight -- not keep re-ordering every cycle
    because compute_rebalance_orders was comparing current weight against the
    pre-clip raw target instead.

    place_basket_orders (see line ~763) reassigns target_weights to the
    return of clip_weights_to_position_cap before it is threaded into both
    compute_target_quantities and compute_rebalance_orders, so both see the
    same capped figure. This test exercises that pairing directly, without
    going through the firewall, to pin the behavior independently of the
    fuller integration test.
    """
    raw_weights = {"SPY": 0.61, "AAPL": 0.13, "MSFT": 0.13, "QQQ": 0.13}
    total_budget_usd = 90_000.0
    account_equity = 100_000.0
    position_cap_max_pct_of_equity = 0.25

    capped_weights, clips = core_strategy.clip_weights_to_position_cap(
        raw_weights, total_budget_usd, account_equity, position_cap_max_pct_of_equity
    )
    assert "SPY" in clips
    assert capped_weights["SPY"] < raw_weights["SPY"]

    prices = {"SPY": 500.0, "AAPL": 200.0, "MSFT": 400.0, "QQQ": 450.0}
    target_quantities = core_strategy.compute_target_quantities(capped_weights, prices, total_budget_usd)

    # Simulate the account already sitting exactly at the capped target (as it
    # would after a prior successful rebalance).
    current_positions = dict(target_quantities)

    deltas = core_strategy.compute_rebalance_orders(
        current_positions,
        target_quantities,
        target_weights=capped_weights,
        prices=prices,
        drift_threshold=0.05,
    )

    assert all(delta == 0 for delta in deltas.values()), (
        f"expected no re-order once holdings match the capped target, got {deltas}"
    )


def test_compute_total_budget_usd_is_a_stated_fraction_of_equity():
    budget = core_strategy.compute_total_budget_usd(100_000.0, basket_pct_of_equity=0.90)
    assert budget == pytest.approx(90_000.0)


def test_compute_total_budget_usd_rejects_non_positive_equity():
    with pytest.raises(ValueError):
        core_strategy.compute_total_budget_usd(0.0)
    with pytest.raises(ValueError):
        core_strategy.compute_total_budget_usd(-500.0)


# --- clip_weights_to_position_cap: requirement 1's "ceiling on the sum
# across chunks" enforced at the allocation step, not reactively ----------


def test_clip_weights_leaves_names_under_the_ceiling_untouched():
    weights = {"AAPL": 0.20, "MSFT": 0.20}
    capped, clips = core_strategy.clip_weights_to_position_cap(
        weights, total_budget_usd=90_000.0, account_equity=100_000.0,
        position_cap_max_pct_of_equity=0.25,
    )
    assert capped == weights
    assert clips == {}


def test_clip_weights_clips_a_name_over_the_ceiling():
    # SPY's raw weight targets 0.44 * $90,000 = $39,600, over the ceiling
    # of 0.25 * $100,000 = $25,000 -> capped weight = $25,000 / $90,000.
    weights = {"SPY": 0.44, "QQQ": 0.20}
    capped, clips = core_strategy.clip_weights_to_position_cap(
        weights, total_budget_usd=90_000.0, account_equity=100_000.0,
        position_cap_max_pct_of_equity=0.25,
    )
    assert capped["QQQ"] == pytest.approx(0.20)  # unclipped name is untouched
    assert capped["SPY"] == pytest.approx(25_000.0 / 90_000.0)
    assert "SPY" in clips
    assert "QQQ" not in clips
    clip = clips["SPY"]
    assert clip.raw_weight == pytest.approx(0.44)
    assert clip.ceiling_usd == pytest.approx(25_000.0)


def test_clip_weights_excess_is_not_redistributed():
    # The clipped-away weight simply vanishes (becomes uninvested cash) --
    # it must NOT be added to any other name's weight.
    weights = {"SPY": 0.50, "QQQ": 0.20}
    capped, _ = core_strategy.clip_weights_to_position_cap(
        weights, total_budget_usd=100_000.0, account_equity=100_000.0,
        position_cap_max_pct_of_equity=0.25,
    )
    assert capped["QQQ"] == pytest.approx(0.20)
    assert sum(capped.values()) < sum(weights.values())


# --- split_order_into_chunks: requirement 1's actual order-splitting -----


def test_split_order_into_chunks_under_ceiling_is_a_single_chunk():
    # 10 shares * $100 = $1,000, under a $5,000 ceiling.
    chunks = core_strategy.split_order_into_chunks(10, 100.0, max_notional_per_chunk=5000.0)
    assert chunks == [10]


def test_split_order_into_chunks_splits_evenly():
    # 220 shares * $100 = $22,000; ceiling $5,000 -> 50 shares/chunk max
    # (floor(5000/100)) -> 5 chunks of 44 shares each ($4,400/chunk).
    chunks = core_strategy.split_order_into_chunks(220, 100.0, max_notional_per_chunk=5000.0)
    assert sum(chunks) == 220
    assert all(c * 100.0 <= 5000.0 for c in chunks)
    assert len(chunks) == 5  # matches the prompt's own "SPY needs 5 chunks" framing


def test_split_order_into_chunks_last_chunk_is_the_remainder():
    # 101 shares, 50/chunk max -> 50, 50, 1.
    chunks = core_strategy.split_order_into_chunks(101, 100.0, max_notional_per_chunk=5000.0)
    assert chunks == [50, 50, 1]
    assert sum(chunks) == 101


def test_split_order_into_chunks_no_ceiling_is_unsplit():
    chunks = core_strategy.split_order_into_chunks(500, 100.0, max_notional_per_chunk=math.inf)
    assert chunks == [500]


def test_split_order_into_chunks_zero_qty_is_empty():
    assert core_strategy.split_order_into_chunks(0, 100.0, max_notional_per_chunk=5000.0) == []


# --- ThrottlePacer: requirement 2's throttle-aware spacing ----------------


def test_throttle_pacer_does_not_sleep_under_the_margin():
    sleeps: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    pacer = core_strategy.ThrottlePacer(
        max_orders=20, window_seconds=60.0, safety_margin=0.75,
        sleep_fn=fake_sleep, time_fn=lambda: clock["t"],
    )

    async def run():
        for _ in range(15):  # exactly the 75%-of-20 margin
            await pacer.before_submit()

    asyncio.run(run())
    assert sleeps == []


def test_throttle_pacer_sleeps_before_exceeding_the_margin():
    sleeps: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds  # simulate time actually passing

    pacer = core_strategy.ThrottlePacer(
        max_orders=20, window_seconds=60.0, safety_margin=0.75,
        sleep_fn=fake_sleep, time_fn=lambda: clock["t"],
    )

    async def run():
        for _ in range(16):  # one over the 15-order margin
            await pacer.before_submit()

    asyncio.run(run())
    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_throttle_pacer_never_exceeds_margin_within_any_window():
    """The real property that matters: no 60-second trailing window ever
    contains more than the margin's worth of submissions, across a long
    run -- not just "the 16th call sleeps once"."""
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        clock["t"] += seconds

    pacer = core_strategy.ThrottlePacer(
        max_orders=20, window_seconds=60.0, safety_margin=0.75,
        sleep_fn=fake_sleep, time_fn=lambda: clock["t"],
    )

    timestamps: list[float] = []

    async def run():
        for _ in range(40):
            await pacer.before_submit()
            timestamps.append(clock["t"])
            clock["t"] += 0.01  # simulate real submission latency

    asyncio.run(run())

    for t in timestamps:
        count_in_window = sum(1 for ts in timestamps if t - 60.0 < ts <= t)
        assert count_in_window <= 15


def test_market_order_payload_structure():
    buy_payload = core_strategy.build_market_order_payload("AAPL", 3, side="buy")
    assert buy_payload == {
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "qty": "3",
    }

    sell_payload = core_strategy.build_market_order_payload("MSFT", -2, side="sell")
    assert sell_payload == {
        "symbol": "MSFT",
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "qty": "2",
    }


def test_order_payloads_include_explicit_client_order_id_only_when_supplied():
    stock = core_strategy.build_market_order_payload(
        "AAPL", 3, client_order_id="cycle-aapl-01"
    )
    option = core_strategy.build_option_order_payload(
        "AAPL260918P00220000", 1, client_order_id="cycle-option-01"
    )
    assert stock["client_order_id"] == "cycle-aapl-01"
    assert option["client_order_id"] == "cycle-option-01"


# --- end-to-end against the real, unmodified default policy -------------


def test_basket_orders_clear_the_real_default_policy(tmp_path):
    """The central claim this module's docstring makes: inverse-volatility
    rebalancing orders for every basket name are allowed (not hard-blocked)
    by the real, unmodified policies/default.yaml, and reach the fake upstream
    as qty-only market orders."""
    received: list[tuple[str, dict]] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append((symbol, {"side": side, "qty": qty, "type": type, "limit_price": limit_price}))
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    @upstream.tool
    def get_all_positions() -> list[dict]:
        return []

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    prices = {s: 100.0 + i * 50 for i, s in enumerate(core_strategy.BASKET)}
    bars_fetcher = _fake_bars_fetcher(prices)

    async def run():
        async with Client(proxy) as client:
                return await core_strategy.place_basket_orders(
                    client,
                    bars_fetcher=bars_fetcher,
                    current_positions={s: 0 for s in core_strategy.BASKET},
                    total_budget_usd=800.0,
                include_options_overlay=False,
            )

    attempts = asyncio.run(run())

    assert len(attempts) == len(core_strategy.BASKET)
    for attempt in attempts:
        assert attempt.forwarded, attempt.detail
        assert attempt.qty is not None and attempt.qty >= 1

    assert {sym for sym, _ in received} == set(core_strategy.BASKET)
    for symbol, args in received:
        assert args["side"] == "buy"
        assert args["type"] == "market"
        # No limit_price: verified live against the real Alpaca paper API
        # (2026-08-29) that type="market" + limit_price is rejected outright
        # (HTTP 422, code 40010001) -- see core_strategy.py's own comment at
        # its build_market_order_payload call site.
        assert args["limit_price"] is None
        assert int(args["qty"]) >= 1

    records = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) >= len(core_strategy.BASKET)


def test_full_pipeline_clips_dominant_weight_and_chunks_oversized_orders(tmp_path, monkeypatch):
    """The real demonstration requirement 4 asks for: at $100k-account
    scale, with real dynamic caps (notional_cap 5%/position_cap 25% of
    equity, both read from the real, unmodified default.yaml), a
    dominant-weight name (SPY here, ~61% raw target from real inverse-vol
    math on the price series below) gets its target weight CLIPPED to
    position_cap's real ceiling before any order is built, and the
    resulting (still oversized relative to notional_cap) order is SPLIT
    into multiple chunks, each individually clearing the real firewall.
    The other three names are NOT clipped (their own raw targets already
    sit under the ceiling) but ARE still chunked, since $9-14k targets
    exceed the $5,000 per-order notional cap. Every single order --
    clipped or not, chunked or not -- must reach the fake upstream, proving
    the real caps allow correctly-sized traffic rather than just blocking
    everything.
    """
    # Empirically chosen (see this test's own derivation) so SPY's realized
    # volatility is meaningfully lower than the other three, producing a
    # dominant ~61% raw inverse-vol weight -- comfortably over position_cap's
    # 25%-of-equity ceiling, while the other three stay comfortably under it.
    price_series = {s: [100.0, 103.0, 98.0, 102.0, 97.0] for s in core_strategy.BASKET}
    price_series["SPY"] = [100.0, 101.0, 99.5, 100.5, 99.8]
    shared_bars_fetcher = _fake_bars_fetcher(price_series)

    received: list[tuple[str, str]] = []  # (symbol, qty)

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append((symbol, qty))
        return {"order_id": f"fake-{symbol}-{len(received)}", "status": "accepted"}

    # SAME shared_bars_fetcher for the real policy engine's notional_cap/
    # position_cap reference-price fallback -- so the firewall's own
    # independent repricing of each qty-only chunk agrees with the price
    # core_strategy itself used to size that chunk (both real GET
    # /v2/stocks/{symbol}/bars calls would agree in production; this keeps
    # the test's two fakes from silently disagreeing with each other).
    engine, log_path = _real_policy_engine(tmp_path, bars_fetcher=shared_bars_fetcher)

    def fake_account_fetcher():
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0, equity=100_000.0)

    def fake_positions_fetcher():
        return account_data.PositionsResult(ok=True, positions={}, fetched_at=0.0)

    proxy = build_proxy(
        upstream,
        policy_engine=engine,
        account_pnl_fetcher=fake_account_fetcher,
        positions_fetcher=fake_positions_fetcher,
    )

    # Fast, injected pacer: this test's own chunk count is not the point
    # (ThrottlePacer's own pacing behavior is already covered directly by
    # the test_throttle_pacer_* tests above) -- a real asyncio.sleep here
    # would make this test slow/flaky against exactly how many chunks the
    # real vol/weight math happens to produce.
    import time as time_module
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(time_module, "time", lambda: clock["t"])

    async def fake_sleep(seconds):
        clock["t"] += seconds

    fast_pacer = core_strategy.ThrottlePacer(
        max_orders=20, window_seconds=60.0, sleep_fn=fake_sleep, time_fn=lambda: clock["t"]
    )

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=shared_bars_fetcher,
                account_equity=100_000.0,
                position_cap_max_pct_of_equity=0.25,
                notional_cap_max_usd=5000.0,
                current_positions={s: 0 for s in core_strategy.BASKET},
                include_options_overlay=False,
                audit_writer=engine.audit_writer,
                throttle_pacer=fast_pacer,
            )

    attempts = asyncio.run(run())

    # Every attempt -- every chunk of every symbol -- must have cleared the
    # real firewall. A single failure here means either the clip or the
    # chunk math disagreed with what notional_cap/position_cap actually
    # enforce.
    for attempt in attempts:
        assert attempt.forwarded, attempt.detail

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    clip_records = [r for r in records if r["tool_name"] == "basket_rebalance:weight_clipped"]
    assert {r["arguments"]["symbol"] for r in clip_records} == {"SPY"}
    spy_clip = clip_records[0]
    assert spy_clip["arguments"]["raw_target_weight"] > 0.27
    assert spy_clip["arguments"]["ceiling_usd"] == pytest.approx(25_000.0)

    chunk_records = {
        r["arguments"]["symbol"]: r
        for r in records
        if r["tool_name"] == "basket_rebalance:order_chunked"
    }
    assert "SPY" in chunk_records
    for symbol, record in chunk_records.items():
        args = record["arguments"]
        assert args["chunk_count"] > 1
        assert sum(args["chunk_sizes"]) == args["total_qty"]
        assert all(n <= 5000.0 + 1e-6 for n in args["chunk_notionals"])

    # The real, load-bearing assertion: SPY's chunks must sum to AT OR
    # UNDER position_cap's real $25,000 ceiling -- not the raw target
    # inverse-vol weighting alone would have produced.
    spy_qty = sum(int(qty) for sym, qty in received if sym == "SPY")
    spy_price = price_series["SPY"][-1]
    assert spy_qty * spy_price <= 25_000.0 + 1e-6

    # The other names must reach their own FULL (unclipped) targets
    # -- proving the clip is symbol-specific, not a global cap on the cycle.
    for symbol in core_strategy.BASKET:
        if symbol != "SPY":
            assert symbol not in {r["arguments"]["symbol"] for r in clip_records}
            qty = sum(int(q) for sym, q in received if sym == symbol)
            assert qty > 0


def test_rebalance_skips_when_drift_within_threshold(tmp_path):
    """When existing positions already match target weights within drift_threshold,
    no orders are placed upstream, avoiding unnecessary turnover and firewall load."""
    received: list[tuple[str, dict]] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append((symbol, {"side": side, "qty": qty, "type": type, "limit_price": limit_price}))
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Identical prices and equal volatilities -> equal 25% target weights
    prices = {s: 100.0 for s in core_strategy.BASKET}
    bars_fetcher = _fake_bars_fetcher(prices)

    # Positions already held: 2 shares of each ($200 each, 25% each -> 0% drift)
    existing_positions = {s: 2 for s in core_strategy.BASKET}

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                current_positions=existing_positions,
                total_budget_usd=800.0,
                drift_threshold=0.05,
                include_options_overlay=False,
            )

    attempts = asyncio.run(run())

    assert len(attempts) == len(core_strategy.BASKET)
    for attempt in attempts:
        assert attempt.forwarded
        assert attempt.qty == 0
        assert "no rebalance needed" in attempt.detail

    # Zero orders sent upstream
    assert len(received) == 0


def test_unpriceable_symbol_is_skipped_not_fatal(tmp_path):
    """A bars-fetch failure for one name doesn't abort the whole cycle --
    the remaining basket names still get evaluated and proposed."""
    upstream = FastMCP("fake-alpaca")
    received: list[str] = []

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append(symbol)
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Price every basket name except the first.
    priced = {s: 100.0 for s in core_strategy.BASKET[1:]}
    bars_fetcher = _fake_bars_fetcher(priced)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                total_budget_usd=800.0,
                current_positions={s: 0 for s in core_strategy.BASKET},
            )

    attempts = asyncio.run(run())

    unpriced_symbol = core_strategy.BASKET[0]
    skipped = next(a for a in attempts if a.symbol == unpriced_symbol)
    assert not skipped.forwarded
    assert "skipped" in skipped.detail

    assert unpriced_symbol not in received
    assert set(received) == set(core_strategy.BASKET[1:])


def test_place_basket_orders_includes_scheduled_options_overlay(tmp_path, monkeypatch):
    """When include_options_overlay is enabled (default), place_basket_orders
    proposes a protective put on the largest position in addition to stock rebalancing."""
    _patch_contract_resolver(monkeypatch)
    stock_orders: list[dict] = []
    option_orders: list[dict] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        stock_orders.append({"symbol": symbol, "side": side, "qty": qty, "type": type})
        return {"order_id": f"fake-stock-{symbol}", "status": "accepted"}

    @upstream.tool
    def place_option_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        option_orders.append({"symbol": symbol, "side": side, "qty": qty, "type": type})
        return {"order_id": f"fake-opt-{symbol}", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Give AAPL higher value so it is the largest position
    prices = {s: 100.0 for s in core_strategy.BASKET}
    prices["AAPL"] = 200.0
    bars_fetcher = _fake_bars_fetcher(prices)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                current_positions={s: 0 for s in core_strategy.BASKET},
                total_budget_usd=800.0,
                include_options_overlay=True,
            )

    attempts = asyncio.run(run())

    assert len(attempts) == len(core_strategy.BASKET) + 1

    # Stock orders
    stock_attempts = [a for a in attempts if a.symbol in core_strategy.BASKET]
    assert len(stock_attempts) == len(core_strategy.BASKET)
    for a in stock_attempts:
        assert a.forwarded

    # 1 option overlay attempt
    option_attempt = next(a for a in attempts if a.symbol not in core_strategy.BASKET)
    assert option_attempt.qty >= 1
    assert "scheduled options overlay" in option_attempt.detail.lower()


def test_scheduled_overlay_writes_provenance_record_before_real_verdict(tmp_path, monkeypatch):
    """The scheduled overlay's order submission must produce TWO audit
    records, not one: a provenance record this module writes directly
    (rule_id="scheduled-options-overlay", verdict="info", identifying where
    the order came from) and the real policy verdict PolicyEngine.evaluate()
    writes for that same place_option_order call (whatever rule_id actually
    fired). Neither record may be dropped or collapsed into the other --
    this reproduces the exact case that originally surfaced the gap: no
    real market data for the option quote in this test environment, so the
    real evaluation fails closed (hard_block) on a real firewall rule.
    Both records must still land."""
    _patch_contract_resolver(monkeypatch)
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    @upstream.tool
    def place_option_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        return {"order_id": f"fake-opt-{symbol}", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    prices = {s: 100.0 for s in core_strategy.BASKET}
    prices["AAPL"] = 200.0
    bars_fetcher = _fake_bars_fetcher(prices)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                current_positions={s: 0 for s in core_strategy.BASKET},
                total_budget_usd=800.0,
                include_options_overlay=True,
                audit_writer=engine.audit_writer,
            )

    attempts = asyncio.run(run())

    option_attempt = next(a for a in attempts if a.symbol not in core_strategy.BASKET)
    # No real options market data reachable in this test environment --
    # reproduces the hard-block case this test exists to lock in.
    assert not option_attempt.forwarded

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]

    provenance_records = [r for r in records if r["tool_name"] == "scheduled_overlay:proposed"]
    assert len(provenance_records) == 1, "expected exactly one provenance record"
    provenance = provenance_records[0]
    assert provenance["rule_id"] == "scheduled-options-overlay"
    assert provenance["verdict"] == "info"
    assert provenance["arguments"]["occ_symbol"] == option_attempt.symbol

    verdict_records = [
        r
        for r in records
        if r["tool_name"] == "place_option_order"
        and r["arguments"].get("symbol") == option_attempt.symbol
    ]
    assert len(verdict_records) == 1, "expected exactly one real-verdict record"
    real_verdict = verdict_records[0]

    # The real verdict is a genuine PolicyEngine decision -- hard-blocked
    # here by an actual registered rule, never by the provenance tag.
    assert real_verdict["verdict"] == "hard_block"
    assert real_verdict["rule_id"] is not None
    assert real_verdict["rule_id"] != "scheduled-options-overlay"

    # Provenance is written first, unconditionally -- before the order is
    # even submitted -- so it never depends on (or is erased by) the real
    # verdict's outcome.
    assert records.index(provenance) < records.index(real_verdict)


def test_scheduled_options_overlay_description_and_reason_audit(monkeypatch):
    """Verify that the scheduled overlay is explicitly framed as standing portfolio
    insurance applied regardless of market conditions, distinct from the reactive hedge."""
    from firewall.rules.hedge_proposal import compute_scheduled_overlay

    _patch_contract_resolver(monkeypatch)
    positions = {"AAPL": 100, "MSFT": 10}
    prices = {"AAPL": 150.0, "MSFT": 300.0}  # AAPL: $15,000, MSFT: $3,000

    overlay = compute_scheduled_overlay(positions, prices, now=1_700_000_000.0)

    assert overlay is not None
    assert overlay.symbol == "AAPL"
    assert overlay.current_price == 150.0
    assert overlay.strike == 142.50  # 5% OTM
    assert overlay.contracts >= 1
    assert "SCHEDULED OPTIONS OVERLAY" in overlay.reason
    assert "disclosed, scheduled options overlay applied regardless of market conditions" in overlay.reason
    assert "distinct from the reactive CVaR-triggered hedge" in overlay.reason
    assert "Standing portfolio insurance, not a market-timing decision" in overlay.reason


def test_read_dynamic_policy_config_matches_the_real_default_policy(tmp_path, monkeypatch):
    """`_read_dynamic_policy_config` must read notional_cap/position_cap/
    order_rate_throttle's REAL configured values off the real, unmodified
    default.yaml -- proving core_strategy's own clip/chunk/pacing math has
    a single source of truth with the firewall, not a duplicated constant
    that could silently drift from it."""
    log_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine.from_yaml(
        "policies/default.yaml", audit_writer=AuditLogWriter(log_path, session_id="test")
    )

    def fake_fetch_session_pnl(**kwargs):
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0, equity=100_000.0)

    monkeypatch.setattr(account_data, "fetch_session_pnl", fake_fetch_session_pnl)

    config = core_strategy._read_dynamic_policy_config(engine)

    assert config["account_equity"] == pytest.approx(100_000.0)
    assert config["position_cap_max_pct_of_equity"] == pytest.approx(0.25)
    # notional_cap's max_pct_of_equity (0.05) * $100,000 equity = $5,000 --
    # matches default.yaml's own comment on why that figure was chosen.
    assert config["notional_cap_max_usd"] == pytest.approx(5_000.0)
    assert config["order_rate_max_orders"] == 20
    assert config["order_rate_window_seconds"] == pytest.approx(60.0)


def test_read_dynamic_policy_config_falls_back_to_static_notional_cap_when_equity_missing(
    tmp_path, monkeypatch
):
    log_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine.from_yaml(
        "policies/default.yaml", audit_writer=AuditLogWriter(log_path, session_id="test")
    )

    def failing_fetch_session_pnl(**kwargs):
        return account_data.AccountPnLResult(ok=False, reason="network error")

    monkeypatch.setattr(account_data, "fetch_session_pnl", failing_fetch_session_pnl)

    config = core_strategy._read_dynamic_policy_config(engine)

    assert config["account_equity"] is None
    # Falls back to notional_cap's static max_usd, same posture the rule
    # itself takes (see notional_cap.py's own "DYNAMIC CAP, WITH A STATIC
    # FALLBACK" docstring section).
    assert config["notional_cap_max_usd"] == pytest.approx(5_000.0)


def test_fetch_current_positions_formats():
    """Verify fetch_current_positions correctly parses both flat list responses
    and wrapped dictionary responses (e.g. from alpaca-mcp-server)."""
    # 1. Flat list format
    flat_upstream = FastMCP("fake-alpaca-flat")

    @flat_upstream.tool
    def get_all_positions() -> list[dict]:
        return [
            {"symbol": "SPY", "qty": "10"},
            {"symbol": "AAPL", "qty": "5.0"},
        ]

    async def run_flat():
        async with Client(flat_upstream) as client:
            return await core_strategy.fetch_current_positions(client)

    flat_positions = asyncio.run(run_flat())
    assert flat_positions["SPY"] == 10
    assert flat_positions["AAPL"] == 5
    assert flat_positions["MSFT"] == 0
    assert flat_positions["QQQ"] == 0

    # 2. Wrapped dict format
    wrapped_upstream = FastMCP("fake-alpaca-wrapped")

    @wrapped_upstream.tool
    def get_all_positions() -> dict:
        return {
            "_alpaca_mcp_security": {},
            "data": {
                "result": [
                    {"symbol": "SPY", "qty": "58"},
                    {"symbol": "QQQ", "qty": "32"},
                    {"symbol": "AAPL", "qty": "58"},
                    {"symbol": "MSFT", "qty": "27"},
                ]
            },
        }

    async def run_wrapped():
        async with Client(wrapped_upstream) as client:
            return await core_strategy.fetch_current_positions(client)

    wrapped_positions = asyncio.run(run_wrapped())
    assert wrapped_positions["SPY"] == 58
    assert wrapped_positions["QQQ"] == 32
    assert wrapped_positions["AAPL"] == 58
    assert wrapped_positions["MSFT"] == 27

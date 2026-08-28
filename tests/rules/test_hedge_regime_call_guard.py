"""Tests for hedge_regime_call_guard.

Reuses hedge_proposal.compute_proposal()'s own trigger detection verbatim
(no re-derivation) -- see the fixtures below, which mirror
tests/rules/test_hedge_proposal.py's own _hedge_rule/_cvar_rule/
_drawdown_rule helpers.
"""

from __future__ import annotations

from firewall.market_data import BarsResult, DailyBar
from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
from firewall.rules.hedge_proposal import HedgeProposalRule
from firewall.rules.hedge_regime_call_guard import HedgeRegimeCallGuardRule


def _guard_rule(**params) -> HedgeRegimeCallGuardRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-hedge-regime-call-guard",
            "type": "hedge_regime_call_guard",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return HedgeRegimeCallGuardRule(config)


def _hedge_rule(**params) -> HedgeProposalRule:
    config = RuleConfig.model_validate(
        {
            "id": "hedge-proposal",
            "type": "hedge_proposal",
            "severity": "soft",
            "regulation_ref": None,
            **params,
        }
    )
    return HedgeProposalRule(config)


def _bars_result(closes: list[float]) -> BarsResult:
    return BarsResult(ok=True, bars=[DailyBar(close=c, volume=1000.0) for c in closes])


def _bars_fetcher_for_flagged_symbol_only(expected_symbol: str, closes: list[float]):
    """A fake fetch_daily_bars that only serves real bars for
    `expected_symbol` (the flagged position's own ticker, e.g. "AAPL" from
    order_history) and fails for anything else -- most importantly the
    option's own OCC symbol ("AAPL260918C00150000"). This is what actually
    proves compute_proposal()'s drawdown path resolves bars for the FLAGGED
    stock symbol, not whatever the incoming place_option_order call's own
    `symbol` argument happens to be -- a fetcher that ignores its `symbol`
    argument entirely would pass this test's assertions for the wrong
    reason."""

    def fetcher(symbol: str, lookback: int) -> BarsResult:
        if symbol != expected_symbol:
            return BarsResult(ok=False, reason=f"no such symbol: {symbol!r}")
        return _bars_result(closes)

    return fetcher


def _cvar_rule(result: BarsResult, **params) -> CVaRGateRule:
    config = RuleConfig.model_validate(
        {
            "id": "cvar-gate",
            "type": "cvar_gate",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return CVaRGateRule(config, bars_fetcher=lambda symbol, lookback: result)


def _drawdown_rule(**params) -> DrawdownKillswitchRule:
    config = RuleConfig.model_validate(
        {
            "id": "drawdown-killswitch",
            "type": "drawdown_killswitch",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return DrawdownKillswitchRule(config)


_CALL_ARGS = {"symbol": "AAPL260918C00150000", "side": "buy", "qty": "1"}
_PUT_ARGS = {"symbol": "AAPL260918P00150000", "side": "buy", "qty": "1"}


def _drawdown_active_state(**overrides) -> dict:
    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )
    state = {
        "session_pnl_usd": -5000.0,  # well past 50% of -1000 threshold
        "order_history": history,
        "now": 1_700_000_000.0,
    }
    state.update(overrides)
    return state


# --- regime active (drawdown trigger) -> call BUY hard-blocks --------------


def test_call_buy_hard_blocks_when_drawdown_regime_active(monkeypatch):
    # compute_proposal()'s drawdown path fetches bars for the FLAGGED
    # symbol (AAPL, from order_history), NOT the incoming call's own OCC
    # symbol -- the fake fetcher below only serves bars for "AAPL" and
    # fails for anything else, so this proves that resolution actually
    # happens rather than passing for a fetcher that ignores its argument.
    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(
        hedge_proposal_module,
        "fetch_daily_bars",
        _bars_fetcher_for_flagged_symbol_only("AAPL", [150.0, 151.0]),
    )

    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check("place_option_order", _CALL_ARGS, state)

    assert outcome.triggered
    assert "authorized hedging structures are limited to protective puts or collars" in outcome.reason


def test_reason_documents_the_long_stock_assumption(monkeypatch):
    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(
        hedge_proposal_module,
        "fetch_daily_bars",
        _bars_fetcher_for_flagged_symbol_only("AAPL", [150.0, 151.0]),
    )

    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check("place_option_order", _CALL_ARGS, state)

    assert outcome.triggered
    assert "long" in outcome.reason.lower()


def test_cvar_path_is_wired_correctly_but_unreachable_with_a_real_bars_fetcher():
    # This proves the WIRING is correct (compute_proposal's cvar path fires
    # and this rule reports it) -- it does NOT prove the cvar path is
    # reachable in production, and must not be read as such: the fake
    # CVaRGateRule below (via _cvar_rule) injects a bars_fetcher that
    # ignores its own `symbol` argument entirely, same fixture pattern
    # test_hedge_proposal.py's own cvar-trigger tests already use. A REAL
    # cvar_gate_rule._bars_fetcher would try to resolve this call's own OCC
    # `symbol` ("AAPL260918C00150000") as a stock ticker and fail -- the
    # identical "structurally unreachable for options" gap README already
    # documents for cvar_gate generally (see this rule's own module
    # docstring's "WHAT'S ACTUALLY LIVE TODAY" section). Contrast
    # test_call_buy_hard_blocks_when_drawdown_regime_active above, whose
    # fake fetcher DOES discriminate on symbol and therefore proves real
    # reachability for the drawdown path.
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(
        bars, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9, cvar_lookback_days=45
    )
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.5)
    rule = _guard_rule()

    state = {
        "account_equity": 100.0,
        "now": 1_700_000_000.0,
        "hedge_proposal_rule": hedge_rule,
        "cvar_gate_rule": cvar_rule,
    }
    outcome = rule.check(
        "place_option_order",
        # limit_price is required here only because cvar_gate's own
        # extract_notional() needs a price to compute a notional at all --
        # not because this rule cares about the option's own premium.
        {"symbol": "AAPL260918C00150000", "side": "buy", "qty": "1", "limit_price": "100"},
        state,
    )

    assert outcome.triggered


# --- regime NOT active -> call BUY passes -----------------------------------


def test_call_buy_passes_when_no_regime_active():
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        session_pnl_usd=-100.0,  # well above (less negative than) threshold
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check("place_option_order", _CALL_ARGS, state)

    assert not outcome.triggered


def test_missing_hedge_proposal_rule_in_state_does_not_block():
    # Best-effort detection reuse, not a fail-closed market-data check: if
    # the live HedgeProposalRule instance isn't wired into state (the same
    # disclosed, pre-existing gap iv_hv_ratio's cvar_gate_rule state key
    # has), this rule cannot determine whether the regime is active and
    # does not block -- matching compute_proposal()'s own "no proposal"
    # semantics for missing inputs, not net_delta_floor's fail-closed
    # market-data convention.
    rule = _guard_rule()

    outcome = rule.check("place_option_order", _CALL_ARGS, _drawdown_active_state())

    assert not outcome.triggered


# --- out of scope: puts, sells, multi-leg, other tools ----------------------


def test_put_buy_is_never_blocked_by_this_rule():
    # A put has negative delta -- exactly the authorized structure.
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check("place_option_order", _PUT_ARGS, state)

    assert not outcome.triggered


def test_call_sell_is_unchecked():
    # Selling a call is separately, unconditionally hard-blocked by
    # option_sell_guard regardless of regime -- not this rule's job.
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918C00150000", "side": "sell", "qty": "1"},
        state,
    )

    assert not outcome.triggered


def test_multi_leg_order_is_unchecked():
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918P00150000", "ratio_qty": "1", "side": "buy"},
            ],
        },
        state,
    )

    assert not outcome.triggered


def test_stock_order_is_unchecked():
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    rule = _guard_rule()

    state = _drawdown_active_state(
        hedge_proposal_rule=hedge_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, state
    )

    assert not outcome.triggered

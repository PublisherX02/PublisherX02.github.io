"""Tests for hedge_proposal: a single new triggered action (not a new
pillar) that detects cvar_gate's tail-loss estimate or
drawdown_killswitch's session-PnL proximity crossing a configured
early-warning threshold, and computes ONE defined protective options
structure (a protective put) via a disclosed, mechanical formula.

Detection + audit only: compute_proposal() never submits anything and
HedgeProposalRule.check() always returns RuleOutcome(False) -- see the
module docstring in firewall/rules/hedge_proposal.py for why.
"""

from __future__ import annotations

import firewall.rules.hedge_proposal as hedge_proposal_module
from firewall.market_data import BarsResult, ContractResolutionResult, DailyBar, ResolvedContract
from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
from firewall.rules.hedge_proposal import (
    HedgeProposalRule,
    ScheduledOverlayProposal,
    _largest_open_position_from_history,
    _mechanical_contracts,
    _mechanical_expiry,
    _mechanical_strike,
    compute_proposal,
    compute_scheduled_overlay,
    format_hedge_release_note,
    format_occ_symbol,
    is_cvar_trigger_normalized,
    is_drawdown_trigger_normalized,
    is_trigger_normalized,
)


def _bars_result(closes: list[float]) -> BarsResult:
    return BarsResult(ok=True, bars=[DailyBar(close=c, volume=1000.0) for c in closes])


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


def _cvar_rule(result: BarsResult, **params):
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


# --- HedgeProposalRule.check() is always a no-op -----------------------


def test_check_never_triggers_regardless_of_input():
    rule = _hedge_rule()

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "qty": "1000", "limit_price": "500"}, {}
    )

    assert not outcome.triggered
    assert outcome.state_events == []


# --- mechanical formula helpers -----------------------------------------


def test_mechanical_strike_is_otm_percent_below_current_price():
    assert _mechanical_strike(100.0, 0.05) == 95.0


def test_mechanical_expiry_is_midpoint_of_configured_window():
    import time
    from datetime import datetime, timedelta, timezone

    now = time.time()
    expiry = _mechanical_expiry(now, expiry_min_days=10, expiry_max_days=20)

    expected = (
        datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=15)
    ).date().isoformat()
    assert expiry == expected


def test_mechanical_contracts_covers_configured_percent_of_notional():
    # $100,000 flagged notional, 50% coverage -> $50,000 to cover.
    # strike $95 -> 526.3 shares -> ceil(526.3 / 100) = 6 contracts.
    contracts = _mechanical_contracts(flagged_notional=100_000.0, strike=95.0, coverage_pct=0.5)
    assert contracts == 6


def test_mechanical_contracts_is_at_least_one():
    contracts = _mechanical_contracts(flagged_notional=10.0, strike=1000.0, coverage_pct=0.5)
    assert contracts == 1


def test_mechanical_contracts_zero_on_nonpositive_strike():
    assert _mechanical_contracts(flagged_notional=1000.0, strike=0.0, coverage_pct=0.5) == 0


# --- _largest_open_position_from_history --------------------------------


def test_largest_open_position_picks_biggest_absolute_net_quantity():
    # Picked by qty, not notional -- MSFT's $300 price would win on
    # notional (10 * 300 = 3,000 > nothing to compare here), but this
    # test's point is AAPL's 100-share qty beats MSFT's 10-share qty
    # regardless of price.
    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )
    history.record(
        timestamp=2.0, tool="place_stock_order", symbol="MSFT", side="buy",
        qty=10, price=300.0, order_id="2", outcome="open",
    )

    result = _largest_open_position_from_history(history)

    assert result == ("AAPL", 100.0)


def test_largest_open_position_nets_buys_against_sells():
    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )
    history.record(
        timestamp=2.0, tool="place_stock_order", symbol="AAPL", side="sell",
        qty=100, price=150.0, order_id="2", outcome="filled",
    )

    result = _largest_open_position_from_history(history)

    assert result is None  # fully netted out -- no live exposure


def test_largest_open_position_excludes_cancelled_blocked_rejected():
    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=1000, price=500.0, order_id="1", outcome="cancelled",
    )
    history.record(
        timestamp=2.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=1000, price=500.0, order_id="2", outcome="blocked",
    )
    history.record(
        timestamp=3.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=1000, price=500.0, order_id="3", outcome="rejected",
    )
    history.record(
        timestamp=4.0, tool="place_stock_order", symbol="MSFT", side="buy",
        qty=10, price=300.0, order_id="4", outcome="open",
    )

    result = _largest_open_position_from_history(history)

    assert result == ("MSFT", 10.0)


def test_largest_open_position_returns_none_for_empty_history():
    assert _largest_open_position_from_history(OrderHistory()) is None


def test_largest_open_position_includes_events_with_no_price():
    # The bug this proves fixed: a plain market order (symbol/side/qty
    # only, no limit_price -> OrderEvent.price is None) must still be
    # trackable -- it's the most common real order shape. See
    # AUDIT.md's "silently blind to plain market orders" finding.
    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=None, order_id="1", outcome="open",
    )

    assert _largest_open_position_from_history(history) == ("AAPL", 100.0)


# --- compute_proposal: cvar_gate trigger --------------------------------


def test_cvar_trigger_fires_when_tail_loss_crosses_hedge_threshold():
    # Same shape as cvar_gate.py's own test_exceeding_max_loss_triggers:
    # a single sharp drop dominates the tail.
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(
        bars, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9, cvar_lookback_days=45
    )
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.5)

    state = {"account_equity": 100.0, "now": 1_700_000_000.0}
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )

    assert proposal is not None
    assert proposal.trigger == "cvar_gate"
    assert proposal.symbol == "AAPL"
    assert proposal.contracts >= 1
    assert "DEFENSIVE HEDGE PROPOSAL" in proposal.reason
    assert "PROPOSAL ONLY" in proposal.reason
    assert "not a market view" in proposal.reason


def test_cvar_trigger_does_not_fire_under_threshold():
    # Flat prices -> zero CVaR -> never crosses any positive hedge threshold.
    bars = _bars_result([100.0] * 30)
    cvar_rule = _cvar_rule(bars, cvar_max_loss_pct_of_equity=0.01)
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.5)

    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"account_equity": 10_000.0, "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )

    assert proposal is None


def test_cvar_trigger_does_not_fire_without_account_equity():
    # Same gap cvar_gate itself has: nothing in src/ populates
    # state["account_equity"] in the live proxy today, so this path is
    # currently dormant in production -- proven here at the unit level
    # since it can't be proven through build_proxy() (see test_proxy.py).
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(bars, cvar_max_loss_pct_of_equity=0.01)
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.01)

    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"now": 1_700_000_000.0},  # no account_equity
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )

    assert proposal is None


def test_cvar_trigger_skips_when_notional_uncomputable():
    bars = _bars_result([100.0] * 30)
    cvar_rule = _cvar_rule(bars, cvar_max_loss_pct_of_equity=0.01)
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.01)

    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL"},  # plain market order, no computable notional
        {"account_equity": 10_000.0, "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )

    assert proposal is None


def test_no_tool_match_skips_entirely():
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(bars, cvar_max_loss_pct_of_equity=0.01)
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.01)

    proposal = compute_proposal(
        "get_account_info",
        {},
        {"account_equity": 100.0, "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )

    assert proposal is None


def test_no_cvar_gate_rule_configured_skips_that_trigger():
    hedge_rule = _hedge_rule()

    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"account_equity": 100.0, "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=None,
    )

    assert proposal is None


# --- compute_proposal: drawdown_killswitch trigger ----------------------


def test_drawdown_trigger_fires_for_position_seeded_by_plain_market_order(monkeypatch):
    # The bug this proves fixed: a position seeded entirely by plain
    # market orders (symbol/side/qty only, no limit_price on any event --
    # OrderEvent.price is None throughout) must still be flagged. Before
    # the fix, _largest_open_position_from_history excluded every
    # price=None event, so this history produced zero hedge proposals.
    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(
        hedge_proposal_module,
        "fetch_daily_bars",
        lambda symbol, lookback: _bars_result([150.0, 151.0]),
    )

    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)

    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=None, order_id="1", outcome="filled",
    )

    state = {
        "session_pnl_usd": -5000.0,
        "order_history": history,
        "now": 1_700_000_000.0,
    }
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": "100"},  # the triggering call is also a market order
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is not None
    assert proposal.trigger == "drawdown_killswitch"
    assert proposal.symbol == "AAPL"
    assert proposal.flagged_notional == 15_100.0  # 100 qty * $151.0 current price


def test_drawdown_trigger_fires_and_flags_largest_position(monkeypatch):
    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(
        hedge_proposal_module,
        "fetch_daily_bars",
        lambda symbol, lookback: _bars_result([150.0, 151.0]),
    )

    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)

    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )
    history.record(
        timestamp=2.0, tool="place_stock_order", symbol="MSFT", side="buy",
        qty=1, price=300.0, order_id="2", outcome="open",
    )

    state = {
        "session_pnl_usd": -5000.0,  # well past 50% of -1000 threshold
        "order_history": history,
        "now": 1_700_000_000.0,
    }
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "MSFT", "qty": "1", "limit_price": "300"},
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is not None
    assert proposal.trigger == "drawdown_killswitch"
    assert proposal.symbol == "AAPL"  # largest recorded exposure, not the call's own symbol
    # net qty (100) * CURRENT price (151.0, the mocked bars fetcher's
    # latest close) -- not AAPL's $150.0 order-time price.
    assert proposal.flagged_notional == 15_100.0


def test_drawdown_trigger_does_not_fire_above_threshold():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)

    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )

    state = {"session_pnl_usd": -100.0, "order_history": history, "now": 1_700_000_000.0}
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": "1", "limit_price": "150"},
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is None


def test_drawdown_trigger_skips_without_session_pnl():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)

    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": "1", "limit_price": "150"},
        {"order_history": OrderHistory(), "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is None


def test_drawdown_trigger_skips_when_no_open_position_in_history():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.5)

    state = {
        "session_pnl_usd": -5000.0,
        "order_history": OrderHistory(),
        "now": 1_700_000_000.0,
    }
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": "1", "limit_price": "150"},
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is None


def test_cvar_trigger_checked_before_drawdown_when_both_configured():
    # cvar_gate is checked first; if it fires, drawdown isn't consulted.
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(
        bars, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9, cvar_lookback_days=45
    )
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(
        cvar_trigger_pct_of_max_loss=0.5, drawdown_trigger_pct_of_threshold=0.5
    )

    state = {
        "account_equity": 100.0,
        "session_pnl_usd": -5000.0,
        "order_history": OrderHistory(),
        "now": 1_700_000_000.0,
    }
    proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=drawdown_rule,
    )

    assert proposal is not None
    assert proposal.trigger == "cvar_gate"


# --- hedge normalization / release tests --------------------------------


def test_format_hedge_release_note_adds_cashtag():
    assert format_hedge_release_note("AAPL") == "hedge on $AAPL: trigger condition resolved, review for release"
    assert format_hedge_release_note("$AAPL") == "hedge on $AAPL: trigger condition resolved, review for release"
    assert format_hedge_release_note("X") == "hedge on $X: trigger condition resolved, review for release"


def test_drawdown_normalization_when_pnl_recovers():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.8)  # trigger is -800

    # Still breached (-900 <= -800)
    state_breached = {"session_pnl_usd": -900.0}
    assert not is_drawdown_trigger_normalized(state_breached, hedge_rule.cfg, drawdown_rule)

    # Recovered (-500 > -800)
    state_recovered = {"session_pnl_usd": -500.0}
    assert is_drawdown_trigger_normalized(state_recovered, hedge_rule.cfg, drawdown_rule)


def test_drawdown_normalization_when_position_closed():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.8)

    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )
    history.record(
        timestamp=2.0, tool="place_stock_order", symbol="AAPL", side="sell",
        qty=100, price=150.0, order_id="2", outcome="filled",
    )

    # Even if session PnL is still low, the position itself was fully closed
    state = {"session_pnl_usd": -900.0, "order_history": history}
    assert is_drawdown_trigger_normalized(state, hedge_rule.cfg, drawdown_rule, symbol="AAPL")


def test_cvar_normalization_when_risk_drops():
    bars_risky = _bars_result([100.0] * 29 + [50.0])
    bars_calm = _bars_result([100.0] * 30)

    cvar_rule_calm = _cvar_rule(bars_calm, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9)
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.5)

    state = {"account_equity": 10_000.0}
    # Calm bars -> CVaR is 0.0 < threshold -> normalized
    assert is_cvar_trigger_normalized("AAPL", state, hedge_rule.cfg, cvar_rule_calm, notional=1000.0)

    # Risky bars with large notional -> still risky
    cvar_rule_risky = _cvar_rule(bars_risky, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9, cvar_lookback_days=30)
    state_risky = {"account_equity": 100.0}
    assert not is_cvar_trigger_normalized("AAPL", state_risky, hedge_rule.cfg, cvar_rule_risky, notional=10_000.0)


def test_is_trigger_normalized_dispatches_properly():
    drawdown_rule = _drawdown_rule(session_pnl_threshold_usd=-1000.0)
    hedge_rule = _hedge_rule(drawdown_trigger_pct_of_threshold=0.8)

    state = {"session_pnl_usd": -200.0}
    assert is_trigger_normalized(
        "drawdown_killswitch",
        "AAPL",
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )
    assert not is_trigger_normalized(
        "cvar_gate",
        "AAPL",
        state,
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=None,
        drawdown_killswitch_rule=drawdown_rule,
    )


# --- scheduled options overlay tests ------------------------------------


def _patch_contract_resolver(monkeypatch):
    """Patch resolve_listed_contract with a deterministic, network-free
    fake that echoes the mechanical target back as if it were the real
    listed contract -- keeps these unit tests hermetic and their
    pre-existing exact-value assertions valid without a live Alpaca
    options-chain call. Tests of resolution FAILURE patch it separately."""

    def fake_resolve(symbol, target_strike, target_expiry, option_type, *, min_dte=None, now=None):
        occ_symbol = format_occ_symbol(symbol, target_expiry, option_type, target_strike)
        return ContractResolutionResult(
            ok=True,
            contract=ResolvedContract(
                occ_symbol=occ_symbol, strike=target_strike, expiry=target_expiry
            ),
        )

    monkeypatch.setattr(hedge_proposal_module, "resolve_listed_contract", fake_resolve)


def test_format_occ_symbol():
    occ = format_occ_symbol("AAPL", "2026-09-18", "P", 220.0)
    assert occ == "AAPL260918P00220000"

    occ_call = format_occ_symbol("SPY", "2026-10-30", "C", 550.5)
    assert occ_call == "SPY261030C00550500"


def test_compute_scheduled_overlay_on_largest_position(monkeypatch):
    _patch_contract_resolver(monkeypatch)
    positions = {"AAPL": 100, "MSFT": 50}
    prices = {"AAPL": 150.0, "MSFT": 400.0}  # AAPL: $15,000; MSFT: $20,000 (largest)

    overlay = compute_scheduled_overlay(
        positions,
        prices,
        otm_pct=0.05,
        expiry_min_days=14,
        expiry_max_days=45,
        coverage_pct=0.5,
        now=1_700_000_000.0,
    )

    assert overlay is not None
    assert overlay.symbol == "MSFT"
    assert overlay.current_price == 400.0
    assert overlay.strike == 380.0  # 400 * 0.95
    assert overlay.flagged_notional == 20_000.0
    assert overlay.contracts == 1  # (0.5 * 20000) / 380 = 26.3 shares -> ceil(26.3/100) = 1
    assert overlay.occ_symbol.startswith("MSFT")
    assert "P00380000" in overlay.occ_symbol
    assert "SCHEDULED OPTIONS OVERLAY" in overlay.reason
    assert "disclosed, scheduled options overlay applied regardless of market conditions" in overlay.reason
    assert "distinct from the reactive CVaR-triggered hedge" in overlay.reason
    assert "Standing portfolio insurance, not a market-timing decision" in overlay.reason


def test_compute_scheduled_overlay_returns_none_on_empty_or_zero():
    assert compute_scheduled_overlay({}, {"AAPL": 150.0}) is None
    assert compute_scheduled_overlay({"AAPL": 100}, {}) is None
    assert compute_scheduled_overlay({"AAPL": 0}, {"AAPL": 150.0}) is None
    assert compute_scheduled_overlay({"AAPL": 100}, {"AAPL": 0.0}) is None


def test_compute_scheduled_overlay_returns_none_when_no_contract_resolves(monkeypatch):
    """A resolution failure (no real listed contract found) must never fall
    back to asserting a symbol from arithmetic -- it returns None, the same
    outcome as the pre-existing "nothing to hedge" cases above."""

    def failing_resolve(symbol, target_strike, target_expiry, option_type, *, min_dte=None, now=None):
        return ContractResolutionResult(ok=False, reason="no tradable contracts listed")

    monkeypatch.setattr(hedge_proposal_module, "resolve_listed_contract", failing_resolve)

    overlay = compute_scheduled_overlay(
        {"AAPL": 100}, {"AAPL": 150.0}, now=1_700_000_000.0
    )
    assert overlay is None


def test_compute_scheduled_overlay_passes_expiry_floor_as_min_dte(monkeypatch):
    """The 7-day option-expiry-floor DTE minimum (policies/default.yaml's
    option-expiry-floor rule) must reach resolve_listed_contract as
    min_dte, and the overlay's final strike/expiry must be the RESOLVED
    contract's real values, not the mechanical target -- proving the
    interaction is wired, not just the resolver's own internals (which
    resolve_listed_contract's own unit tests in test_market_data.py
    already cover in isolation)."""
    seen_kwargs: dict = {}

    def fake_resolve(symbol, target_strike, target_expiry, option_type, *, min_dte=None, now=None):
        seen_kwargs["min_dte"] = min_dte
        seen_kwargs["target_strike"] = target_strike
        seen_kwargs["target_expiry"] = target_expiry
        seen_kwargs["option_type"] = option_type
        # Deliberately different from the target, so the assertions below
        # can only pass if compute_scheduled_overlay actually uses the
        # RESOLVED values, not the mechanical target it started from.
        return ContractResolutionResult(
            ok=True,
            contract=ResolvedContract(
                occ_symbol="AAPL261204P00141000", strike=141.0, expiry="2026-12-04"
            ),
        )

    monkeypatch.setattr(hedge_proposal_module, "resolve_listed_contract", fake_resolve)

    overlay = compute_scheduled_overlay(
        {"AAPL": 100}, {"AAPL": 150.0}, now=1_700_000_000.0
    )

    assert seen_kwargs["min_dte"] == 7  # _EXPIRY_FLOOR_DAYS, matching option_expiry_floor's default
    assert seen_kwargs["option_type"] == "P"
    assert overlay is not None
    assert overlay.strike == 141.0  # RESOLVED strike, not the mechanical target (142.50)
    assert overlay.target_expiry == "2026-12-04"  # RESOLVED expiry, not the mechanical target
    assert overlay.occ_symbol == "AAPL261204P00141000"
    assert "target was strike" in overlay.reason  # original target still disclosed for transparency
    assert "selected by DELTA, not price" in overlay.reason
    assert "resolved delta unavailable" in overlay.reason  # fake resolver left delta=None


def test_scheduled_overlay_and_reactive_hedge_have_distinguishable_audit_records(monkeypatch):
    """Verify that both hedge sources produce clearly distinguishable audit records
    with distinct reason prefixes and descriptions so dashboards and write-ups can
    show them as two separate, honest mechanisms."""
    _patch_contract_resolver(monkeypatch)
    # 1. Reactive trigger proposal
    bars = _bars_result([100.0] * 29 + [50.0])
    cvar_rule = _cvar_rule(
        bars, cvar_max_loss_pct_of_equity=0.01, cvar_alpha=0.9, cvar_lookback_days=45
    )
    hedge_rule = _hedge_rule(cvar_trigger_pct_of_max_loss=0.5)
    reactive_proposal = compute_proposal(
        "place_stock_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"account_equity": 100.0, "now": 1_700_000_000.0},
        hedge_cfg=hedge_rule.cfg,
        cvar_gate_rule=cvar_rule,
        drawdown_killswitch_rule=None,
    )
    assert reactive_proposal is not None
    assert reactive_proposal.reason.startswith("DEFENSIVE HEDGE PROPOSAL -- not a market view, not a forecast")
    assert "Trigger: cvar_gate" in reactive_proposal.reason

    # 2. Scheduled overlay proposal
    scheduled_proposal = compute_scheduled_overlay(
        {"AAPL": 100}, {"AAPL": 100.0}, now=1_700_000_000.0
    )
    assert scheduled_proposal is not None
    assert scheduled_proposal.reason.startswith(
        "SCHEDULED OPTIONS OVERLAY -- a disclosed, scheduled options overlay applied regardless of market conditions"
    )
    assert "Standing portfolio insurance, not a market-timing decision" in scheduled_proposal.reason

    # Confirm they do not conflate
    assert "SCHEDULED OPTIONS OVERLAY" not in reactive_proposal.reason
    assert "DEFENSIVE HEDGE PROPOSAL" not in scheduled_proposal.reason


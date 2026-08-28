"""Tests for the net_delta_floor rule."""

from firewall.market_data import OptionQuote, OptionQuoteResult
from firewall.rules.base import RuleConfig
from firewall.rules.net_delta_floor import NetDeltaFloorRule, compute_net_delta


def _rule(quote_result: OptionQuoteResult | None, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-net-delta-floor",
            "type": "net_delta_floor",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(ii)",
            **params,
        }
    )
    calls: list[str] = []

    def fake_fetcher(symbol: str) -> OptionQuoteResult:
        calls.append(symbol)
        return quote_result

    return NetDeltaFloorRule(config, quote_fetcher=fake_fetcher), calls


def _quote(delta: float) -> OptionQuoteResult:
    return OptionQuoteResult(ok=True, quote=OptionQuote(bid=4.15, ask=4.30, delta=delta))


# --- compute_net_delta (pure function) ------------------------------------


def test_compute_net_delta_applies_the_x100_contract_multiplier():
    # The exact worked example from the task: a single put contract with
    # delta -0.50 against 100 existing shares must compute to net +50,
    # NOT +99.50 (which is what you get if you forget the x100 multiplier
    # -- comparing a raw option delta directly against share count is off
    # by two orders of magnitude).
    net = compute_net_delta(
        existing_shares=100.0, option_qty=1.0, option_delta=-0.50, side="buy"
    )

    assert net == 50.0
    assert net != 99.50  # explicit regression guard against the missing-x100 bug


def test_compute_net_delta_buy_call_adds_positive_delta():
    net = compute_net_delta(
        existing_shares=0.0, option_qty=2.0, option_delta=0.60, side="buy"
    )

    assert net == 120.0  # 0 + 2 * 0.60 * 100


def test_compute_net_delta_sell_flips_the_sign():
    # Selling (writing) an option is the opposite delta exposure of
    # buying it -- selling a put (delta -0.50) ADDS positive delta,
    # the mirror image of buying one.
    net = compute_net_delta(
        existing_shares=0.0, option_qty=1.0, option_delta=-0.50, side="sell"
    )

    assert net == 50.0


def test_compute_net_delta_sell_call_subtracts_delta():
    net = compute_net_delta(
        existing_shares=100.0, option_qty=1.0, option_delta=0.60, side="sell"
    )

    assert net == 40.0  # 100 - 1 * 0.60 * 100


def test_compute_net_delta_custom_contract_multiplier():
    net = compute_net_delta(
        existing_shares=0.0,
        option_qty=1.0,
        option_delta=-0.50,
        side="buy",
        contract_multiplier=10.0,  # e.g. a mini-contract, not the standard 100
    )

    assert net == -5.0


# --- NetDeltaFloorRule.check() ---------------------------------------------


def test_hedge_that_overshoots_neutral_hard_blocks():
    # 100 existing shares (+100 delta), buying 3 puts at delta -0.50:
    # net = 100 + 3 * (-0.50) * 100 = 100 - 150 = -50, below the floor (0).
    rule, calls = _rule(
        _quote(delta=-0.50),
        net_delta_floor=0.0,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "3"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered
    assert "proposed hedge exceeds neutral delta" in outcome.reason
    assert "this is a directional short, not a hedge" in outcome.reason
    assert calls == ["AAPL260918P00220000"]


def test_properly_sized_hedge_passes():
    # 100 existing shares, buying 2 puts at delta -0.50:
    # net = 100 + 2 * (-0.50) * 100 = 100 - 100 = 0, not below floor 0.
    rule, _ = _rule(
        _quote(delta=-0.50),
        net_delta_floor=0.0,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "2"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert not outcome.triggered


def test_underlying_looked_up_by_parsed_ticker_not_option_symbol():
    # state key is keyed by the UNDERLYING ticker (parsed from the
    # option's OCC symbol), not the option contract's own symbol.
    rule, _ = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL260918P00220000": 100.0}},  # wrong key
    )

    # Looked up "AAPL" (not found) -> defaults to 0 shares -> net = -50,
    # below floor 0 -> triggers. Proves the lookup key is the underlying.
    assert outcome.triggered


def test_missing_underlying_position_defaults_to_zero_shares_not_fail_closed():
    # Absence of an entry for this underlying means "flat" (0 shares),
    # matching position_cap's own established convention
    # (positions.get(symbol, 0.0)) -- not a market-data failure. A naked
    # option buy against an unverified/absent position correctly reads
    # as a pure directional bet, which floor=0 correctly blocks.
    rule, _ = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {},  # no underlying_share_positions state at all
    )

    assert outcome.triggered  # 0 + 1*(-0.50)*100 = -50, below floor 0


def test_stock_order_is_unchecked():
    rule, calls = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": "10"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert not outcome.triggered
    assert calls == []


def test_multi_leg_order_is_not_checked():
    rule, calls = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert not outcome.triggered
    assert calls == []


def test_unfetchable_quote_fails_closed():
    rule, _ = _rule(
        OptionQuoteResult(ok=False, reason="timed out"),
        net_delta_floor=0.0,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_missing_delta_fails_closed():
    # A snapshot with no greeks (or no parseable delta) means this rule
    # cannot assess neutrality at all -- fails closed like a missing
    # quote, distinct from option_spread_guard which never needs delta.
    rule, _ = _rule(
        OptionQuoteResult(ok=True, quote=OptionQuote(bid=4.15, ask=4.30, delta=None)),
        net_delta_floor=0.0,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_string_typed_qty_is_parsed():
    # Real place_option_order schema types qty as a JSON string
    # (conformance-audit finding A4) -- must be parsed, not skipped.
    rule, _ = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "3"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered  # same as the int-qty=3 case above


def test_unparseable_qty_fails_closed():
    rule, _ = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "not-a-number"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered
    assert "cannot compute net delta" in outcome.reason.lower()


def test_default_side_is_buy():
    rule, _ = _rule(_quote(delta=-0.50), net_delta_floor=0.0)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "qty": "3"},  # no "side" field
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered  # same as the explicit side="buy" case above


def test_default_floor_is_zero():
    rule, _ = _rule(_quote(delta=-0.50))  # no override

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "3"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered


# --- structural_delta_floor (raw per-contract delta, pre-scaling) ----------
#
# A separate, independently-scored check from net_delta_floor above: this
# one looks at the RAW delta straight off the snapshot -- before any x100
# contract-multiplier or quantity scaling -- and asks "is this contract even
# sensitive enough to function as a hedge," not "does the resulting position
# stay net-hedged." The two checks must never share a scaled value: a case
# engineered to pass one and fail the other proves that.


def test_raw_delta_below_structural_floor_hard_blocks():
    # Deep out-of-the-money put, |delta| 0.10 < the 0.15 structural floor.
    # No existing shares, so the net_delta ceiling (default floor 0.0) would
    # ALSO reject this order (net = 0 + 1*(-0.10)*100 = -10) -- this test
    # only proves the structural check fires and carries its own message;
    # test_structural_floor_is_independent_of_net_delta_scaling below proves
    # it fires even when the aggregate check would have passed.
    rule, calls = _rule(
        _quote(delta=-0.10),
        net_delta_floor=0.0,
        structural_delta_floor=0.15,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL": 0.0}},
    )

    assert outcome.triggered
    assert "proposed hedge delta below minimum structural threshold" in outcome.reason
    assert "too far out-of-the-money" in outcome.reason
    assert calls == ["AAPL260918P00220000"]


def test_raw_delta_at_structural_floor_passes():
    # |delta| exactly equal to the floor must pass (strict "<", not "<=").
    # existing_shares=15 also keeps the net_delta ceiling from firing
    # (net = 15 + 1*(-0.15)*100 = 0, not below the default floor of 0.0),
    # isolating this as a pure boundary test of the structural check.
    rule, _ = _rule(
        _quote(delta=-0.15),
        net_delta_floor=0.0,
        structural_delta_floor=0.15,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL": 15.0}},
    )

    assert not outcome.triggered


def test_structural_floor_is_independent_of_net_delta_scaling():
    # Constructed to PASS the net_delta ceiling but FAIL the structural
    # floor -- the case that would incorrectly pass if the structural check
    # were accidentally implemented against the scaled net_delta value
    # instead of the raw per-contract delta.
    #
    # 10 existing shares, buying 1 put at raw delta -0.05:
    #   net_delta = 10 + 1 * (-0.05) * 100 = 10 - 5 = 5, NOT below the
    #   net_delta_floor of 0.0 -- the aggregate check alone would pass this.
    #   But |raw delta| = 0.05 < structural_delta_floor 0.15 -- too far
    #   out-of-the-money to be a meaningful hedge, regardless of how the
    #   resulting aggregate position nets out.
    args = {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"}
    state = {"underlying_share_positions": {"AAPL": 10.0}}

    rule, _ = _rule(
        _quote(delta=-0.05),
        net_delta_floor=0.0,
        structural_delta_floor=0.15,
    )
    outcome = rule.check("place_option_order", args, state)

    assert outcome.triggered
    assert "minimum structural threshold" in outcome.reason

    # Prove the "would have passed" half of the claim, not just assert it
    # in a comment: the same arguments, with the structural check disabled
    # (floor 0.0 -- no raw delta clears a floor of zero), must pass. This
    # is what makes the test actually demonstrate independent scaling --
    # if the structural check were implemented against compute_net_delta's
    # scaled output instead of the raw quote delta, this second call would
    # incorrectly trigger too.
    passthrough_rule, _ = _rule(
        _quote(delta=-0.05),
        net_delta_floor=0.0,
        structural_delta_floor=0.0,
    )
    passthrough_outcome = passthrough_rule.check("place_option_order", args, state)

    assert not passthrough_outcome.triggered


def test_structural_floor_does_not_block_when_net_delta_ceiling_would():
    # The mirror case: raw delta comfortably clears the structural floor,
    # but the resulting aggregate position overshoots neutral -- this is
    # exactly test_hedge_that_overshoots_neutral_hard_blocks above, and it
    # must still trigger for the net_delta_floor reason, not get relabeled
    # as a structural-floor failure now that both checks run.
    rule, _ = _rule(
        _quote(delta=-0.50),
        net_delta_floor=0.0,
        structural_delta_floor=0.15,
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "3"},
        {"underlying_share_positions": {"AAPL": 100.0}},
    )

    assert outcome.triggered
    assert "proposed hedge exceeds neutral delta" in outcome.reason
    assert "minimum structural threshold" not in outcome.reason


def test_default_structural_delta_floor_is_point_one_five():
    # No override: default structural_delta_floor is 0.15.
    rule, _ = _rule(_quote(delta=-0.10))  # no net_delta_floor override either

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"underlying_share_positions": {"AAPL": 0.0}},
    )

    assert outcome.triggered
    assert "minimum structural threshold" in outcome.reason

"""Tests for the symbol_allowlist rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.symbol_allowlist import SymbolAllowlistRule


def _rule(**params) -> SymbolAllowlistRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-symbol-allowlist",
            "type": "symbol_allowlist",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(2)(ii)",
            **params,
        }
    )
    return SymbolAllowlistRule(config)


def test_symbol_not_on_allowlist_triggers():
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("place_order", {"symbol": "TSLA"}, {})

    assert outcome.triggered


def test_symbol_on_allowlist_passes():
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("place_order", {"symbol": "AAPL"}, {})

    assert not outcome.triggered


def test_replace_order_by_id_is_deliberately_unchecked():
    # Documents a verified decision, not an oversight -- see the module
    # docstring in src/firewall/rules/symbol_allowlist.py: Alpaca's real
    # replace_order_by_id schema has no symbol/side/instrument-identifying
    # field at all, so there is no argument through which this call could
    # redirect an order to a disallowed symbol. Even a symbol matching no
    # entry on the allowlist (TSLA, absent here) is irrelevant, since
    # replace_order_by_id never carries a symbol argument in the first
    # place -- this call has none.
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("replace_order_by_id", {"order_id": "abc123", "qty": 100000}, {})

    assert not outcome.triggered


def test_single_leg_option_order_occ_symbol_never_matches_plain_ticker():
    # Alpaca's real place_option_order schema carries an OCC-format symbol
    # for single-leg orders (e.g. "AAPL250321C00150000"), never the plain
    # ticker on the allowlist -- confirmed against the live inputSchema.
    # This is a pre-existing, deliberately-conservative side effect (every
    # single-leg option order is hard-blocked today), not something this
    # rule needs to special-case.
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL250321C00150000", "side": "buy", "qty": "1"},
        {},
    )

    assert outcome.triggered


def test_multi_leg_order_with_all_legs_on_allowed_underlyings_passes():
    # Multi-leg place_option_order calls carry no parent "symbol" at all
    # (verified against the live inputSchema: "Symbol and side on the
    # parent are not needed for multi-leg") -- the allowlist must be
    # checked against each leg's underlying instead.
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL250321C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL250321C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert not outcome.triggered


def test_multi_leg_order_with_one_leg_on_disallowed_underlying_triggers():
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL250321C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "TSLA250321C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert outcome.triggered


def test_multi_leg_order_with_unparseable_leg_symbol_fails_closed():
    # A leg whose symbol doesn't parse as a valid OCC option symbol can't
    # be assessed against the allowlist at all -- unlike replace_order_by_id
    # (which structurally never carries a symbol), this call does carry
    # leg data that just isn't in a recognizable shape, so it must fail
    # closed rather than silently pass.
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [{"symbol": "not-an-occ-symbol", "ratio_qty": "1", "side": "buy"}],
        },
        {},
    )

    assert outcome.triggered


def test_multi_leg_order_with_no_legs_and_no_symbol_is_unchecked():
    # Same precedent as replace_order_by_id: nothing to check against.
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("place_option_order", {"qty": "10", "order_class": "mleg"}, {})

    assert not outcome.triggered

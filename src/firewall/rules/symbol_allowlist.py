"""symbol_allowlist — hard block any order for a symbol not on the list.

Regulation: SEC Rule 15c3-5(c)(2)(ii) (algorithmic trading must be
restricted to a defined, reviewed set of instruments).

Confirmed decision on `replace_order_by_id` (conformance-audit finding A4):
this rule's `tool_match: ["order"]` matches replace_order_by_id along with
every place_* tool, but `check()` below no-ops on it (`arguments.get(
"symbol")` is always None) rather than treating a missing symbol as
incomplete-data-fail-closed the way notional_cap/position_cap now do for
their own None case. That is deliberate, not an oversight -- verified
directly against the real, live `alpaca-mcp-server` package's
`replace_order_by_id` tool schema (`uvx alpaca-mcp-server`, MCP
`list_tools()`, inputSchema properties, checked 2026-08-18):

    ['advanced_instructions', 'client_order_id', 'limit_price', 'notional',
     'order_id', 'qty', 'stop_price', 'time_in_force', 'trail']

No `symbol`, `side`, `asset_class`, `order_class`, or any other
instrument-identifying field exists anywhere in that schema -- the only way
to identify *which* order to modify is `order_id`, and every other
parameter only adjusts size, price, time-in-force, or routing on that same
existing order. There is structurally no argument through which a replace
call could redirect an order to a different instrument, so a symbol
allowlist violation cannot be introduced via this endpoint the way it can
via a place_* call.

This safety argument has one real precondition worth stating plainly: it
only holds for an order this firewall itself validated at placement time
(every place_stock_order/place_option_order/place_crypto_order call also
matches tool_match=["order"] and does carry `symbol`, so it was checked
here already). An open order that predates this proxy -- placed before the
firewall was running, or through a different client -- has no such
guarantee; amending it via replace_order_by_id is invisible to this rule
either way, same limitation FirewallMiddleware's own docstring already
notes for order_history/pnl_history being in-memory-since-startup only.

MULTI-LEG place_option_order is a second, genuinely different case from
replace_order_by_id's "no symbol field exists at all" -- verified against
the live inputSchema (uvx --offline alpaca-mcp-server, checked
2026-08-24): a multi-leg call ("order_class": "mleg") carries no parent
`symbol` either ("Symbol and side on the parent are not needed for
multi-leg"), but it DOES carry a `legs` array (up to 4 dicts, each with
its own OCC-format `symbol` and `ratio_qty`) that a plain missing-symbol
no-op would silently skip entirely -- unlike replace_order_by_id, there is
real instrument-identifying data on this call, it just isn't in the
parent's `symbol` field. So: symbol absent AND legs present checks each
leg's underlying (parsed via `_util.parse_occ_underlying`) against the
allowlist instead, failing closed on any leg whose symbol doesn't parse as
a valid OCC symbol (unlike a genuinely absent field, an unparseable one
means real data existed here and couldn't be assessed). Single-leg
place_option_order is unaffected by this: its `symbol` is also
OCC-format, so it already falls through to the exact-match check below
and is hard-blocked today (no plain ticker will ever match an OCC
string) -- a pre-existing, deliberately-conservative side effect, not
something this addition changes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any, parse_occ_underlying
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    allowed_symbols: list[str]
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    legs_field: str = "legs"
    leg_symbol_field: str = "symbol"


class SymbolAllowlistRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._allowed = {s.upper() for s in self.cfg.allowed_symbols}

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            legs = arguments.get(self.cfg.legs_field)
            if not legs:
                # Confirmed safe for replace_order_by_id specifically --
                # see module docstring above for the verified schema and
                # the precondition this relies on.
                return RuleOutcome(False)
            return self._check_legs(legs)

        if str(symbol).upper() not in self._allowed:
            return RuleOutcome(True, f"symbol {symbol!r} is not on the allowlist")
        return RuleOutcome(False)

    def _check_legs(self, legs: Any) -> RuleOutcome:
        for leg in legs:
            leg_symbol = leg.get(self.cfg.leg_symbol_field) if isinstance(leg, dict) else None
            underlying = parse_occ_underlying(leg_symbol)
            if underlying is None:
                return RuleOutcome(
                    True,
                    f"multi-leg order has an unparseable leg symbol {leg_symbol!r} — "
                    "cannot verify against the allowlist, failing closed",
                )
            if underlying not in self._allowed:
                return RuleOutcome(
                    True,
                    f"multi-leg order leg {leg_symbol!r} has underlying {underlying!r} "
                    "which is not on the allowlist",
                )
        return RuleOutcome(False)

"""notional_cap — max USD notional per single order.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must cap the value
of a single order).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    max_usd: float
    tool_match: list[str] = ["order"]
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    # place_option_order's qty is a contract count, not a share count, and
    # limit_price is a per-share premium/net-debit (single-leg) or net
    # debit/credit (multi-leg) -- one option contract represents 100
    # shares. Verified against the live inputSchema (uvx --offline
    # alpaca-mcp-server, checked 2026-08-24): "qty" description reads
    # "Number of contracts... For multi-leg, this is the strategy
    # multiplier"; "limit_price" description reads "For multi-leg, this is
    # the net debit/credit". Without this multiplier, qty * limit_price
    # undercounts real premium by 100x. Single-leg option orders are
    # already fully hard-blocked by symbol_allowlist today (their `symbol`
    # is OCC-format, e.g. "AAPL250321C00150000", which never matches a
    # plain-ticker allowlist entry), so this multiplier's only live effect
    # is on multi-leg orders -- the one shape symbol_allowlist's parent-
    # symbol check can't see at all (see symbol_allowlist.py's own fix for
    # that), leaving this rule as the sole live defense against an
    # oversized multi-leg options structure until position_cap/cvar_gate/
    # pct_of_adv gain a per-leg notional model of their own (a disclosed,
    # not-yet-built gap -- see README's "What this does not do").
    option_tool_match: list[str] = ["place_option_order"]
    option_contract_multiplier: float = 100.0
    # Tools where a None notional means "incomplete data for an order that
    # was already sized," not "this order type just doesn't carry a price."
    # Deliberately NOT place_stock_order/place_option_order/
    # place_crypto_order: verified against alpaca-mcp-server's real
    # place_stock_order input schema (uvx alpaca-mcp-server, tool
    # inputSchema, checked 2026-08-18) -- `type` defaults to "market", and
    # limit_price is documented "Required for limit and stop_limit orders"
    # only. A plain market order (the common case -- "buy 10 shares of
    # AAPL") legitimately carries qty and no price at all; extract_notional
    # returning None for one is normal, not incomplete data, and a
    # malformed *placement* call missing a required limit_price for a
    # limit/stop_limit order is rejected by Alpaca itself at submission,
    # not silently executed -- there's no exploitable window there for this
    # rule to close.
    #
    # replace_order_by_id is different in kind, not degree: verified
    # against its real input schema, `limit_price` there is "Required if
    # original order's `type` field was `limit` or `stop_limit`" -- i.e.
    # Alpaca lets a caller omit it and leaves the *existing* resting price
    # untouched. This firewall has no visibility into that existing price
    # (it isn't in the call's arguments, and isn't tracked in
    # OrderHistory), so a qty-only replace is a call Alpaca will genuinely
    # accept and execute at an unknown price -- exactly the case this
    # module's docstring and AUDIT.md's A4 finding describe. Fail closed
    # here until a purpose-built amendment rule with real order-state
    # lookup exists (see policies/default.yaml's comment on this rule and
    # README's "What this does not do").
    sizing_tool_match: list[str] = ["replace_order_by_id"]


class NotionalCapRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        multiplier = (
            self.cfg.option_contract_multiplier
            if matches_any(tool_name, self.cfg.option_tool_match)
            else 1.0
        )
        notional = extract_notional(
            arguments,
            self.cfg.notional_field,
            self.cfg.qty_field,
            self.cfg.price_field,
            multiplier,
        )
        if notional is None:
            if matches_any(tool_name, self.cfg.sizing_tool_match):
                return RuleOutcome(
                    True,
                    "cannot compute notional — incomplete order data, failing closed",
                )
            return RuleOutcome(False)

        if notional > self.cfg.max_usd:
            return RuleOutcome(
                True,
                f"order notional ${notional:,.2f} exceeds per-order cap "
                f"${self.cfg.max_usd:,.2f}",
            )
        return RuleOutcome(False)

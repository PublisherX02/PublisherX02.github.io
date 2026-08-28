"""option_sell_guard — hard block ALL option-sell orders (single-leg
`side == "sell"`, or any leg of a multi-leg order), pending dedicated
collateral/assignment-risk checks.

STATED SCOPE LIMITATION, not a silent gap: every check this task added
alongside this one -- `option_expiry_floor`, `option_spread_guard`,
`net_delta_floor`'s two checks, `hedge_cost_cap` -- prices or sizes an
option order on the assumption that premium is being SPENT (a buy).
Writing/selling an option receives premium instead, and its real risk is
uncapped loss on assignment, not a bounded premium outlay -- none of those
checks, nor `notional_cap`/`position_cap`, model that risk. Building real
collateral/margin/assignment-risk controls for short options is out of
scope for this change; the safe default is to hard-block the sell side
outright until those controls exist, not to let it through with checks
that don't actually protect it.

Consequence, stated plainly: any structure with a short leg -- a covered
call, a cash-secured put, a collar (sell call + buy put) -- is out of
scope for this firewall today. A trader wanting one of those must go
around this proxy entirely; that is the accepted cost of this scope
decision, not an oversight.

SINGLE-LEG: reads the call's own `side` field, defaulting to "buy" when
absent -- matching the verified `place_option_order` schema default
already documented in `notional_cap.py`/`position_cap.py`'s module
docstrings.

MULTI-LEG: an `order_class: "mleg"` call carries no parent `side` field at
all (verified against the live inputSchema, same evidence
`symbol_allowlist.py`'s module docstring cites: "Symbol and side on the
parent are not needed for multi-leg") -- each leg carries its own `side`
instead. Every leg is checked, and a leg dict present but with a missing
or non-string `side` FAILS CLOSED (blocked), rather than defaulting to
"buy": unlike the top-level default (a call that legitimately omits the
field by schema design), a leg that exists but doesn't state a parseable
side is missing real data this rule cannot positively clear as a buy --
the same distinction `symbol_allowlist._check_legs` already draws between
a genuinely absent `legs` array (no-op) and an unparseable leg symbol
(fail closed).

GATED ON `place_option_order` SPECIFICALLY, not the generic "order"
substring `tool_match` most rules in this package use: `side == "sell"` is
completely normal and carries no comparable risk for
`place_stock_order`/`place_crypto_order` (selling shares/coins already
held), so this rule must never even see those calls.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

_SCOPE_LIMITATION_REASON = (
    "option-sell orders are not yet supported by this firewall -- the "
    "premium-cap and delta-floor checks in this corridor all assume a buy, "
    "and no collateral/assignment-risk control exists yet for the "
    "uncapped loss a short option can produce on assignment. Hard-blocked "
    "as a stated scope limitation, not a silent gap: covered calls, "
    "cash-secured puts, and collars (sell + buy legs together) are out of "
    "scope until dedicated controls exist."
)


class _Params(BaseModel):
    tool_match: list[str] = ["place_option_order"]
    side_field: str = "side"
    legs_field: str = "legs"
    leg_side_field: str = "side"


class OptionSellGuardRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        side = str(arguments.get(self.cfg.side_field, "buy")).strip().lower()
        if side == "sell":
            return RuleOutcome(True, _SCOPE_LIMITATION_REASON)

        for leg in arguments.get(self.cfg.legs_field) or []:
            if not isinstance(leg, dict):
                continue
            leg_side = leg.get(self.cfg.leg_side_field)
            if not isinstance(leg_side, str) or leg_side.strip().lower() != "buy":
                reported = leg_side if leg_side is not None else "<missing>"
                return RuleOutcome(
                    True,
                    "multi-leg option order has a leg that is not a verified "
                    f"buy (leg side {reported!r}) -- an option-sell leg is "
                    "not yet supported by this firewall (same scope "
                    "limitation as a single-leg sell; see this rule's "
                    "module docstring), and a missing or unparseable leg "
                    "side cannot be positively cleared as a buy, so this "
                    "fails closed rather than assuming one",
                )

        return RuleOutcome(False)

"""hedge_regime_call_guard — hard block buying a CALL option while a
CVaR/drawdown hedge-trigger regime is active, since a call is structurally
the wrong instrument to buy while this firewall's own risk signals say
exposure should be getting hedged, not added to.

NOT REDUNDANT WITH `net_delta_floor` (this corridor's own delta floor/
ceiling checks): a call has POSITIVE delta, so buying one against an
assumed long stock position pushes net delta MORE positive, not below any
floor. `net_delta_floor`'s checks are lower-bound checks by construction
(`net_delta < floor` / `abs(raw_delta) < structural_floor`) -- they can
only ever catch a position that becomes too directionally SHORT/thin, and
structurally cannot catch a position becoming too directionally LONG,
regardless of the call's delta magnitude. This rule catches exactly that
failure mode instead, and does so on the option's TYPE (a free symbol
parse), not its delta -- it does not need a live quote at all.

REGIME DETECTION REUSES `hedge_proposal.compute_proposal()` VERBATIM, not
a re-derived trigger calculation: this rule calls it with the exact same
`tool_name`/`arguments`/`state` it itself receives, and the exact same
live `CVaRGateRule`/`DrawdownKillswitchRule` instances and
`HedgeProposalRule.cfg` thresholds hedge_proposal.py's own trigger checks
use -- read from `state` (same established pattern `iv_hv_ratio` already
uses for its own `cvar_gate_rule` state key). A non-None `HedgeProposal`
means the regime is active; `check()` never inspects the proposal's own
mechanical strike/expiry/contract fields, only whether one exists.

THIS IS A BEST-EFFORT REUSE OF A DETECTION FEATURE, NOT A FAIL-CLOSED
MARKET-DATA CHECK -- a deliberate distinction from `net_delta_floor`/
`option_spread_guard`/`hedge_cost_cap`, which fail closed when live
market data can't be fetched. If `state["hedge_proposal_rule"]` (or the
cvar/drawdown rule references it needs) isn't populated, this rule cannot
determine whether the regime is active and does NOT block -- mirroring
`compute_proposal()`'s own behavior for the exact same missing inputs
(silently returns no proposal; `HedgeProposalRule.check()` never blocks
regardless). Treating "can't determine" as "must block" here would mean
blocking every call BUY unconditionally under the current, undisclosed
wiring state, which is a materially different (and much more aggressive)
default than any of this rule's siblings adopt for their own missing-input
cases.

WHAT'S ACTUALLY LIVE TODAY, stated plainly, not glossed over: the
`drawdown_killswitch` trigger path IS reachable through the real proxy --
`state["session_pnl_usd"]` is populated on every order-related call (see
`account_data`/`FirewallMiddleware._populate_session_pnl`) and
`state["order_history"]` always exists. The `cvar_gate` trigger path is
NOT reachable for this rule's own call shape: `_check_cvar_trigger` reads
`arguments.get(cvar_gate_rule.cfg.symbol_field)`, which for a
`place_option_order` call is the option's own OCC symbol, not a stock
ticker -- `cvar_gate_rule._bars_fetcher` cannot resolve that as a real
symbol, so this path returns `None` (same "structurally unreachable"
shape README already documents for `cvar_gate`-on-options generally, not
a new gap this rule introduces). BOTH paths additionally require
`state["hedge_proposal_rule"]`/`state["cvar_gate_rule"]`/
`state["drawdown_killswitch_rule"]` to be wired by `FirewallMiddleware` --
which, as of this writing, nothing in `src/` does; wiring that is a
separate, disclosed, not-yet-built piece of work (the same shape of gap
`iv_hv_ratio`'s own `cvar_gate_rule` state key already has).

ASSUMES THE FLAGGED POSITION IS LONG STOCK -- stated explicitly, not
implicitly inherited: this is the same assumption `hedge_proposal.py`
itself already makes (it only ever proposes a protective PUT, never a
protective call), so this rule's restriction is consistent with that
existing design, not a new one. A SHORT stock position facing the same
regime would need a protective CALL instead of a put -- exactly the
instrument this rule blocks -- and that case is explicitly OUT OF SCOPE:
this rule has no way to distinguish "a call that increases directional
exposure against a long position" from "a call that correctly hedges a
short position," since nothing in `src/` tracks position direction at the
share level in a form this rule reads (see `net_delta_floor`'s own
`underlying_share_positions` gap). Revisit only alongside a real
short-position hedge design, not as a silent extension here.

BUY ONLY: `side == "sell"` is unconditionally hard-blocked by the separate
`option_sell_guard` rule regardless of regime, so this rule is a plain
no-op on it, not a fail-closed case.

SINGLE-LEG ONLY, same scope boundary as `option_spread_guard`/
`net_delta_floor`/`hedge_cost_cap`: self-scopes via a valid OCC-format
parent `symbol`. A multi-leg order (a collar, e.g.) is a disclosed,
deliberate gap: `option_sell_guard` already forces every leg of a
multi-leg order to be a verified buy today (see its own module
docstring), so a collar's short call leg is unconditionally blocked
there regardless of this rule -- collars remain out of scope for this
firewall until that separate limitation is lifted, at which point this
rule's own multi-leg scope would need revisiting too, not silently
extended now for a structure nothing can submit yet.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules import hedge_proposal
from firewall.rules._util import matches_any, parse_occ_option_type
from firewall.rules.base import Rule, RuleConfig, RuleOutcome
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
from firewall.rules.hedge_proposal import HedgeProposalRule

_REASON_PREFIX = (
    "authorized hedging structures are limited to protective puts or "
    "collars while a hedge-trigger regime is active"
)


class _Params(BaseModel):
    tool_match: list[str] = ["place_option_order"]
    symbol_field: str = "symbol"
    side_field: str = "side"
    hedge_proposal_rule_state_key: str = "hedge_proposal_rule"
    cvar_gate_rule_state_key: str = "cvar_gate_rule"
    drawdown_killswitch_rule_state_key: str = "drawdown_killswitch_rule"


class HedgeRegimeCallGuardRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        option_type = parse_occ_option_type(symbol) if symbol else None
        if option_type != "C":
            # A put (the authorized structure), or not a parseable
            # single-leg call symbol at all (multi-leg/malformed) --
            # deliberately out of scope; see module docstring.
            return RuleOutcome(False)

        side = str(arguments.get(self.cfg.side_field, "buy")).strip().lower()
        if side != "buy":
            # option_sell_guard already hard-blocks this unconditionally.
            return RuleOutcome(False)

        hedge_rule = state.get(self.cfg.hedge_proposal_rule_state_key)
        if not isinstance(hedge_rule, HedgeProposalRule):
            # Can't determine whether the regime is active -- best-effort
            # detection reuse, not a fail-closed market-data check. See
            # module docstring.
            return RuleOutcome(False)

        cvar_gate_rule = state.get(self.cfg.cvar_gate_rule_state_key)
        drawdown_killswitch_rule = state.get(self.cfg.drawdown_killswitch_rule_state_key)

        proposal = hedge_proposal.compute_proposal(
            tool_name,
            arguments,
            state,
            hedge_cfg=hedge_rule.cfg,
            cvar_gate_rule=(
                cvar_gate_rule if isinstance(cvar_gate_rule, CVaRGateRule) else None
            ),
            drawdown_killswitch_rule=(
                drawdown_killswitch_rule
                if isinstance(drawdown_killswitch_rule, DrawdownKillswitchRule)
                else None
            ),
        )
        if proposal is None:
            return RuleOutcome(False)

        return RuleOutcome(
            True,
            f"{_REASON_PREFIX} ({proposal.trigger} flagged {proposal.symbol}, "
            f"notional ${proposal.flagged_notional:,.2f}) -- buying a call adds "
            "positive delta on top of an assumed LONG stock position, "
            "increasing directional exposure instead of hedging it. This "
            "assumes the flagged position is long stock; a short position "
            "would need a protective call instead, which is out of scope "
            "for this rule. Resubmit as a put, or use a collar once "
            "short-option controls exist.",
        )

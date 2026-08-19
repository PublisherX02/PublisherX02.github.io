"""Rule type registry for the firewall policy engine.

Adding a new rule type means writing a module here and registering its
class in RULE_TYPES -- PolicyEngine.from_yaml raises for any `type` in a
policy file that isn't in this registry.
"""

from __future__ import annotations

from firewall.rules.base import Rule, RuleConfig, RuleOutcome
from firewall.rules.blast_radius import BlastRadiusRule
from firewall.rules.cooldown_after_loss import CooldownAfterLossRule
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
from firewall.rules.gtc_restriction import GTCRestrictionRule
from firewall.rules.layering_detector import LayeringDetectorRule
from firewall.rules.notional_cap import NotionalCapRule
from firewall.rules.order_rate_throttle import OrderRateThrottleRule
from firewall.rules.pct_of_adv import PctOfAdvRule
from firewall.rules.place_cancel_ratio import PlaceCancelRatioRule
from firewall.rules.position_cap import PositionCapRule
from firewall.rules.symbol_allowlist import SymbolAllowlistRule
from firewall.rules.unrecognized_tool_catchall import UnrecognizedToolCatchallRule
from firewall.rules.wash_trade_detector import WashTradeDetectorRule

RULE_TYPES: dict[str, type[Rule]] = {
    "notional_cap": NotionalCapRule,
    "position_cap": PositionCapRule,
    "symbol_allowlist": SymbolAllowlistRule,
    "blast_radius": BlastRadiusRule,
    "drawdown_killswitch": DrawdownKillswitchRule,
    "order_rate_throttle": OrderRateThrottleRule,
    "place_cancel_ratio": PlaceCancelRatioRule,
    "layering_detector": LayeringDetectorRule,
    "wash_trade_detector": WashTradeDetectorRule,
    "cvar_gate": CVaRGateRule,
    "pct_of_adv": PctOfAdvRule,
    "gtc_restriction": GTCRestrictionRule,
    "cooldown_after_loss": CooldownAfterLossRule,
    "unrecognized_tool_catchall": UnrecognizedToolCatchallRule,
}

__all__ = ["Rule", "RuleConfig", "RuleOutcome", "RULE_TYPES"]

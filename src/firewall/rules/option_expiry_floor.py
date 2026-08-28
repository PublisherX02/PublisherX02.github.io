"""option_expiry_floor — hard block option orders whose contract(s) expire
within a configured minimum number of days, to guard against pin risk and
accelerated theta decay near expiration.

NO MARKET-DATA CALL: the expiration date is parsed directly from the
order's own OCC-format option symbol already present in the tool call
(YYMMDD immediately after the root ticker, e.g. "AAPL260918P00220000"
decodes to 2026-09-18 -- see `firewall.rules._util.parse_occ_expiry`).
This is a pure string-format decode, not a lookup.

CALENDAR DAYS, not trading days -- a deliberate, explicitly stated choice.
Trading-day counting needs a market holiday calendar (exchange holidays,
early closes), a dependency this firewall does not otherwise carry
anywhere for a payload check like this one (contrast cvar_gate/
pct_of_adv, which already accept a market-data dependency, but for a
different purpose -- computing risk from historical prices, not counting
days). Calendar days is the simpler, dependency-free, mechanically
unambiguous choice. The practical effect: `days_to_expiry_floor: 7` can
correspond to anywhere from 5 to 7 *trading* days depending on how many
weekend days fall in the window (this rule does not know about exchange
holidays either, so a holiday in the window narrows the trading-day count
further) -- stated here so a threshold tuned against trading-day
intuition isn't silently off by a day or two.

Self-scoping, no option-specific tool_match needed: like
symbol_allowlist's multi-leg handling, this rule reads whatever
OCC-format symbol(s) the call actually carries (parent `symbol` for
single-leg, each leg's `symbol` for multi-leg) rather than gating on tool
name. A call with no parseable OCC symbol anywhere (a stock order, a
crypto order, an option call missing symbol/legs entirely) is simply not
assessable by this rule and passes through untouched -- the same
`tool_match: ["order"]`-then-shape-check pattern every other rule in this
package uses.

Multi-leg: blocks if ANY leg's expiry is inside the floor (the
conservative reading -- a calendar spread can legitimately carry
different expiries per leg, and the near-dated leg is exactly the one
pin-risk/theta-decay concern applies to). A leg whose symbol doesn't
parse as valid OCC is skipped, not failed closed: this rule's job is DTE,
not symbol-format validation -- an unparseable leg symbol is
symbol_allowlist's fail-closed responsibility (see its own module
docstring), and duplicating that check here would blur which rule owns
which failure mode.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any, parse_occ_expiry
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    days_to_expiry_floor: int = 7
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    legs_field: str = "legs"
    leg_symbol_field: str = "symbol"


class OptionExpiryFloorRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        today = _today(state)

        symbol = arguments.get(self.cfg.symbol_field)
        expiry = parse_occ_expiry(symbol)
        if expiry is not None:
            outcome = self._check_expiry(symbol, expiry, today)
            if outcome is not None:
                return outcome
            return RuleOutcome(False)

        for leg in arguments.get(self.cfg.legs_field) or []:
            leg_symbol = leg.get(self.cfg.leg_symbol_field) if isinstance(leg, dict) else None
            leg_expiry = parse_occ_expiry(leg_symbol)
            if leg_expiry is None:
                continue
            outcome = self._check_expiry(leg_symbol, leg_expiry, today)
            if outcome is not None:
                return outcome

        return RuleOutcome(False)

    def _check_expiry(self, symbol: str, expiry: date, today: date) -> RuleOutcome | None:
        dte = (expiry - today).days
        if dte < self.cfg.days_to_expiry_floor:
            return RuleOutcome(
                True,
                f"contract expiration within {self.cfg.days_to_expiry_floor} days -- "
                "restricted for hedging due to pin risk and accelerated theta decay "
                f"({symbol} expires {expiry.isoformat()}, {dte} calendar day(s) from now)",
            )
        return None


def _today(state: dict[str, Any]) -> date:
    now = state.get("now", time.time())
    return datetime.fromtimestamp(now, tz=timezone.utc).date()

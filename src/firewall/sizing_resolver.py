"""sizing_resolver — single source of truth for how large an order is
allowed to be, in consistent USD-notional units. Composes every
independent sizing constraint into one `final_size` and records which
constraint(s) actually bound it, so the audit log can show *why* a trade
was sized the way it was, not just that some limit applied.

    final_size = min(proposed_notional, kelly_suggested_size, cvar_max_size,
                      notional_cap, position_cap, pct_of_adv_cap)

`proposed_notional` participates in the min() too, not just the other
five -- this function must never suggest trading *more* than was asked
for. This feeds the approval-token preview step; no rule downstream of
this should re-derive a size independently.

Constraint sources, all in USD notional:

- kelly_suggested_size: compute_kelly_size(mu, sigma_squared).f_used *
  account_equity. mu/sigma_squared are NOT derived here from historical
  bars -- they must arrive via account_state["mu"]/["sigma_squared"], the
  same way account_equity and positions already arrive in `state`
  elsewhere in this codebase (see CVaRGateRule.account_equity_state_key,
  PositionCapRule.positions_state_key). This module is a composer, not a
  forecaster: deriving an expected-return forecast from realized
  historical returns here would silently make sizing_resolver decide
  trading edge, and it would be wrong in a specific direction -- a symbol
  that rallied over the lookback gets a large mu, suggesting a large size
  exactly when momentum is most likely to mean-revert. If no forecast is
  supplied, kelly_suggested_size is unconstrained (no opinion, doesn't
  bind the min()) rather than failing closed to $0 -- kelly_sizing.py is
  documented as advisory-only, and treating "no forecast" the same as a
  hard risk-control failure would make every order route through the
  kelly term at $0 the moment nobody wires a forecast source, which
  defeats the point of it being advisory. A forecast *is* supplied but
  account_equity is missing, that's different: sizing_resolver can't
  translate the advisory fraction into dollars, so the term fails closed
  to $0, consistent with how cvar_max_size treats missing equity.
- cvar_max_size: the largest notional keeping CVaR within cvar-gate's
  cvar_max_loss_pct_of_equity * account_equity, via the same
  historical-simulation approach as cvar_gate.py (reuses its
  `compute_cvar`). CVaR is linear in notional, so this is
  max_loss_usd / abs(compute_cvar(unit_returns, alpha)). Fails closed to
  $0 on missing/bad market data or missing account_equity, matching
  cvar_gate's own fail-closed behavior exactly.
- notional_cap / position_cap: read from policies/default.yaml's
  notional-cap-single-order / position-cap-per-symbol rule configs (the
  latter net of current exposure, floored at $0) -- never hardcoded here,
  so a policy edit takes effect without touching this file. The policy
  file is re-read on every call, not cached at import time.
- pct_of_adv_cap: the largest notional keeping proposed shares within
  pct-of-adv's max_percent_of_adv * ADV, using the same bars-derived ADV
  and last-close-as-current-price approach as pct_of_adv.py. Fails closed
  to $0 on missing/bad market data, matching pct_of_adv's own fail-closed
  behavior.

If any of the four required policy rule ids (notional-cap-single-order,
position-cap-per-symbol, cvar-gate, pct-of-adv) is missing from the
policy file, or present but disabled, `resolve_size` raises rather than
silently dropping that cap from the min() -- a missing constraint here is
the one failure mode that makes this module dangerous rather than merely
wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules.cvar_gate import _Params as _CVaRParams
from firewall.rules.cvar_gate import compute_cvar
from firewall.rules.kelly_sizing import KellySizingConfig, compute_kelly_size
from firewall.rules.notional_cap import _Params as _NotionalCapParams
from firewall.rules.pct_of_adv import _Params as _PctOfAdvParams
from firewall.rules.position_cap import _Params as _PositionCapParams

DEFAULT_POLICY_PATH = Path(__file__).parent.parent.parent / "policies" / "default.yaml"

_REQUIRED_RULE_IDS = {
    "notional_cap": "notional-cap-single-order",
    "position_cap": "position-cap-per-symbol",
    "cvar_gate": "cvar-gate",
    "pct_of_adv": "pct-of-adv",
}

BarsFetcher = Callable[[str, int], BarsResult]


@dataclass
class SizeResult:
    final_size: float
    constraints: dict[str, float]
    binding: list[str]
    notes: dict[str, str] = field(default_factory=dict)
    reason: str = ""


def _load_rule_dicts(policy_path: Path | str) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8")) or {}
    by_id = {rule_dict["id"]: rule_dict for rule_dict in raw.get("rules", [])}

    missing = [
        rule_id for rule_id in _REQUIRED_RULE_IDS.values() if rule_id not in by_id
    ]
    if missing:
        raise ValueError(
            f"sizing_resolver requires these rule ids in {policy_path}, but they "
            f"are missing: {missing}"
        )
    disabled = [
        rule_id
        for rule_id in _REQUIRED_RULE_IDS.values()
        if not by_id[rule_id].get("enabled", True)
    ]
    if disabled:
        raise ValueError(
            f"sizing_resolver requires these rule ids to be enabled in "
            f"{policy_path}, but they are disabled: {disabled}"
        )
    return by_id


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def resolve_size(
    symbol: str,
    proposed_notional: float,
    account_state: dict[str, Any],
    *,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    bars_fetcher: BarsFetcher | None = None,
) -> SizeResult:
    rule_dicts = _load_rule_dicts(policy_path)
    fetch = bars_fetcher or fetch_daily_bars

    notes: dict[str, str] = {}

    # --- notional_cap -------------------------------------------------
    notional_cfg = _NotionalCapParams.model_validate(
        rule_dicts[_REQUIRED_RULE_IDS["notional_cap"]]
    )
    notional_cap = notional_cfg.max_usd

    # --- position_cap (net of current exposure, floored at $0) --------
    position_cfg = _PositionCapParams.model_validate(
        rule_dicts[_REQUIRED_RULE_IDS["position_cap"]]
    )
    positions: dict[str, Any] = account_state.get(position_cfg.positions_state_key) or {}
    current_exposure = _as_number(positions.get(symbol)) or 0.0
    position_cap = max(0.0, position_cfg.max_usd_per_symbol - current_exposure)

    # --- cvar_max_size --------------------------------------------------
    cvar_cfg = _CVaRParams.model_validate(rule_dicts[_REQUIRED_RULE_IDS["cvar_gate"]])
    equity = _as_number(account_state.get(cvar_cfg.account_equity_state_key))
    cvar_max_size, cvar_note = _resolve_cvar_max_size(symbol, equity, cvar_cfg, fetch)
    if cvar_note:
        notes["cvar_max_size"] = cvar_note

    # --- pct_of_adv_cap ---------------------------------------------------
    adv_cfg = _PctOfAdvParams.model_validate(rule_dicts[_REQUIRED_RULE_IDS["pct_of_adv"]])
    pct_of_adv_cap, adv_note = _resolve_pct_of_adv_cap(symbol, adv_cfg, fetch)
    if adv_note:
        notes["pct_of_adv_cap"] = adv_note

    # --- kelly_suggested_size -------------------------------------------
    kelly_suggested_size, kelly_note = _resolve_kelly_suggested_size(account_state, equity)
    if kelly_note:
        notes["kelly_suggested_size"] = kelly_note

    constraints = {
        "proposed_notional": proposed_notional,
        "kelly_suggested_size": kelly_suggested_size,
        "cvar_max_size": cvar_max_size,
        "notional_cap": notional_cap,
        "position_cap": position_cap,
        "pct_of_adv_cap": pct_of_adv_cap,
    }

    final_size = min(constraints.values())
    binding = [name for name, value in constraints.items() if value == final_size]

    reason = _build_reason(final_size, binding, constraints, notes)
    return SizeResult(
        final_size=final_size,
        constraints=constraints,
        binding=binding,
        notes=notes,
        reason=reason,
    )


def _resolve_cvar_max_size(
    symbol: str,
    equity: float | None,
    cfg: _CVaRParams,
    fetch: BarsFetcher,
) -> tuple[float, str | None]:
    if equity is None:
        return 0.0, "failing closed: account_equity missing/not numeric"

    result = fetch(symbol, cfg.cvar_lookback_days)
    if not result.ok or len(result.closes) < 2:
        detail = result.reason if not result.ok else "fewer than 2 daily bars returned"
        return 0.0, f"failing closed: insufficient market data ({detail})"

    returns = _log_returns(result.closes)
    unit_cvar = compute_cvar(returns, cfg.cvar_alpha)  # CVaR per $1 of notional
    if unit_cvar is None:
        return 0.0, "failing closed: no return series could be computed"
    if unit_cvar >= 0:
        return float("inf"), "no downside in historical sample -- unconstrained by CVaR"

    max_loss_usd = equity * cfg.cvar_max_loss_pct_of_equity
    return max_loss_usd / abs(unit_cvar), None


def _resolve_pct_of_adv_cap(
    symbol: str,
    cfg: _PctOfAdvParams,
    fetch: BarsFetcher,
) -> tuple[float, str | None]:
    result = fetch(symbol, cfg.adv_lookback_days)
    if not result.ok or len(result.closes) < 2:
        detail = result.reason if not result.ok else "fewer than 2 daily bars returned"
        return 0.0, f"failing closed: insufficient market data ({detail})"

    avg_daily_volume = sum(result.volumes) / len(result.volumes)
    if avg_daily_volume <= 0:
        return 0.0, "failing closed: average daily volume is zero"

    current_price = result.closes[-1]
    if current_price <= 0:
        return 0.0, f"failing closed: non-positive close price ${current_price:,.2f}"

    return cfg.max_percent_of_adv * avg_daily_volume * current_price, None


def _resolve_kelly_suggested_size(
    account_state: dict[str, Any], equity: float | None
) -> tuple[float, str | None]:
    mu = _as_number(account_state.get("mu"))
    sigma_squared = _as_number(account_state.get("sigma_squared"))
    if mu is None or sigma_squared is None:
        return float("inf"), "no forecast supplied (mu/sigma_squared absent) -- unconstrained"

    if equity is None:
        return 0.0, "failing closed: forecast supplied but account_equity missing/not numeric"

    kelly_result = compute_kelly_size(mu, sigma_squared, KellySizingConfig())
    return kelly_result.f_used * equity, kelly_result.reason


def _build_reason(
    final_size: float,
    binding: list[str],
    constraints: dict[str, float],
    notes: dict[str, str],
) -> str:
    def fmt(name: str, value: float) -> str:
        value_str = "$inf" if math.isinf(value) else f"${value:,.2f}"
        note = f" ({notes[name]})" if name in notes else ""
        return f"{name}={value_str}{note}"

    binding_str = ", ".join(binding)
    all_str = "; ".join(fmt(name, value) for name, value in constraints.items())
    final_str = "$inf" if math.isinf(final_size) else f"${final_size:,.2f}"
    return (
        f"final_size={final_str}; binding constraint(s): {binding_str}; "
        f"all constraints: {all_str}"
    )

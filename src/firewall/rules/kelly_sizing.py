"""kelly_sizing — advisory position-size suggestion using the
continuous-return Kelly formula:

    f_star = mu / sigma_squared
    f_used = kelly_fraction * f_star

where mu is expected excess return (edge, annualized) and sigma_squared is
the variance of the position's returns, annualized on the same time basis
as mu. This is the continuous-return Kelly criterion, not the binary-bet
(win probability / payout ratio) formula -- callers must not pass in a
daily variance against an annualized mu, or vice versa.

`compute_kelly_size` is advisory only: it never independently approves a
trade size. Its output is one input that gets composed with the hard
notional/position caps enforced elsewhere in policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class KellySizingConfig(BaseModel):
    kelly_fraction: float = 0.25
    sigma_squared_floor: float = 1e-4
    max_assumed_edge: float = 0.15
    max_kelly_output: float = 2.0


@dataclass
class KellySizingResult:
    f_used: float
    reason: str


def compute_kelly_size(
    mu: float,
    sigma_squared: float,
    config: KellySizingConfig | None = None,
) -> KellySizingResult:
    cfg = config or KellySizingConfig()

    if sigma_squared <= cfg.sigma_squared_floor:
        reason = "insufficient variance estimate"
        logger.warning(
            "kelly_sizing: %s (sigma_squared=%.6g <= floor=%.6g) -- returning size 0",
            reason,
            sigma_squared,
            cfg.sigma_squared_floor,
        )
        return KellySizingResult(f_used=0.0, reason=reason)

    mu_effective = mu
    if mu_effective > cfg.max_assumed_edge:
        logger.info(
            "kelly_sizing: clamping mu %.6g down to max_assumed_edge %.6g",
            mu_effective,
            cfg.max_assumed_edge,
        )
        mu_effective = cfg.max_assumed_edge

    if mu_effective <= 0:
        return KellySizingResult(
            f_used=0.0,
            reason="mu <= 0: no long edge, shorting not enabled",
        )

    f_star = mu_effective / sigma_squared
    f_used = cfg.kelly_fraction * f_star

    if f_used > cfg.max_kelly_output:
        logger.warning(
            "kelly_sizing: clamping f_used %.6g down to max_kelly_output %.6g",
            f_used,
            cfg.max_kelly_output,
        )
        f_used = cfg.max_kelly_output

    return KellySizingResult(
        f_used=f_used,
        reason=f"f_star={f_star:.6g}, kelly_fraction={cfg.kelly_fraction:g}",
    )

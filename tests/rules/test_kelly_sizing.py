"""Tests for the kelly_sizing advisory sizing module."""

import logging

import pytest

from firewall.rules.kelly_sizing import KellySizingConfig, compute_kelly_size


def test_continuous_return_kelly_formula():
    # mu=0.10 annualized edge, sigma_squared=0.04 (20% annualized vol).
    # f_star = 0.10 / 0.04 = 2.5; f_used = 0.25 * 2.5 = 0.625.
    result = compute_kelly_size(mu=0.10, sigma_squared=0.04)

    assert result.f_used == pytest.approx(0.625)


def test_kelly_fraction_scales_f_star():
    config = KellySizingConfig(kelly_fraction=0.5)

    result = compute_kelly_size(mu=0.10, sigma_squared=0.04, config=config)

    assert result.f_used == pytest.approx(1.25)


def test_variance_below_floor_returns_zero_and_logs_reason(caplog):
    with caplog.at_level(logging.WARNING, logger="firewall.rules.kelly_sizing"):
        result = compute_kelly_size(mu=0.10, sigma_squared=1e-5)

    assert result.f_used == 0.0
    assert result.reason == "insufficient variance estimate"
    assert "insufficient variance estimate" in caplog.text


def test_variance_equal_to_floor_is_blocked():
    # sigma_squared == floor must be treated the same as below the floor
    # ("<=", not strict "<") -- an estimate this thin is not trustworthy
    # enough to divide by.
    result = compute_kelly_size(mu=0.10, sigma_squared=1e-4)

    assert result.f_used == 0.0
    assert result.reason == "insufficient variance estimate"


def test_custom_variance_floor_is_respected():
    config = KellySizingConfig(sigma_squared_floor=0.01)

    result = compute_kelly_size(mu=0.10, sigma_squared=0.005, config=config)

    assert result.f_used == 0.0
    assert result.reason == "insufficient variance estimate"


@pytest.mark.parametrize("mu", [0.0, -0.05])
def test_nonpositive_mu_returns_zero(mu):
    result = compute_kelly_size(mu=mu, sigma_squared=0.04)

    assert result.f_used == 0.0


def test_negative_mu_does_not_produce_a_short_size():
    result = compute_kelly_size(mu=-0.20, sigma_squared=0.04)

    assert result.f_used == 0.0
    assert result.f_used >= 0.0


def test_mu_above_max_assumed_edge_is_clamped_and_logged(caplog):
    # max_assumed_edge defaults to 0.15; mu=0.50 must be clamped to 0.15
    # before the formula runs: f_star = 0.15 / 0.04 = 3.75, f_used = 0.9375.
    with caplog.at_level(logging.INFO, logger="firewall.rules.kelly_sizing"):
        result = compute_kelly_size(mu=0.50, sigma_squared=0.04)

    assert result.f_used == pytest.approx(0.9375)
    assert "clamping" in caplog.text.lower()


def test_mu_within_max_assumed_edge_is_not_clamped_or_logged(caplog):
    with caplog.at_level(logging.INFO, logger="firewall.rules.kelly_sizing"):
        result = compute_kelly_size(mu=0.10, sigma_squared=0.04)

    assert result.f_used == pytest.approx(2.5 * 0.25)
    assert "clamping" not in caplog.text.lower()


def test_custom_max_assumed_edge_is_respected():
    config = KellySizingConfig(max_assumed_edge=0.05)

    result = compute_kelly_size(mu=0.10, sigma_squared=0.04, config=config)

    # mu clamped to 0.05: f_star = 0.05 / 0.04 = 1.25, f_used = 0.25 * 1.25 = 0.3125.
    assert result.f_used == pytest.approx(0.3125)


def test_extreme_edge_over_tiny_variance_is_capped_at_max_kelly_output(caplog):
    # mu is clamped to max_assumed_edge (0.15) before the formula runs, but
    # sigma_squared is user-supplied and can still be made tiny (just above
    # the floor) to blow the formula up: f_star = 0.15 / 1.0001e-4 ~= 1500,
    # f_used ~= 375. The output ceiling must independently cap this -- it
    # is not relying on the mu/variance guards alone.
    with caplog.at_level(logging.WARNING, logger="firewall.rules.kelly_sizing"):
        result = compute_kelly_size(mu=50.0, sigma_squared=1.0001e-4)

    assert result.f_used <= KellySizingConfig().max_kelly_output
    assert "max_kelly_output" in caplog.text


def test_custom_max_kelly_output_is_respected():
    config = KellySizingConfig(max_kelly_output=0.5)

    result = compute_kelly_size(mu=0.10, sigma_squared=0.04, config=config)

    # Unclamped f_used would be 0.625 (see test_continuous_return_kelly_formula).
    assert result.f_used == pytest.approx(0.5)


def test_output_within_ceiling_is_not_clamped_or_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="firewall.rules.kelly_sizing"):
        result = compute_kelly_size(mu=0.10, sigma_squared=0.04)

    assert result.f_used == pytest.approx(0.625)
    assert "max_kelly_output" not in caplog.text

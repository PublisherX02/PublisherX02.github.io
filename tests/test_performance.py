import pytest

from performance import build_performance_summary


def test_performance_summary_attributes_and_compares_to_benchmark():
    summary = build_performance_summary(
        session_pnl_usd=1_000.0,
        last_equity=100_000.0,
        intraday_pnl={"AAPL": 700.0, "MSFT": -100.0},
        benchmark_closes=[500.0, 502.5],
    )

    assert summary["portfolio_return"] == pytest.approx(0.01)
    assert summary["benchmark_return"] == pytest.approx(0.005)
    assert summary["excess_return"] == pytest.approx(0.005)
    assert summary["attribution"] == [("AAPL", 700.0), ("MSFT", -100.0)]
    assert summary["residual_usd"] == pytest.approx(400.0)


def test_performance_summary_is_honest_when_inputs_are_missing():
    summary = build_performance_summary(
        session_pnl_usd=None,
        last_equity=None,
        intraday_pnl=None,
        benchmark_closes=[500.0],
    )

    assert summary["portfolio_return"] is None
    assert summary["benchmark_return"] is None
    assert summary["excess_return"] is None
    assert summary["attribution"] == []
    assert summary["residual_usd"] is None

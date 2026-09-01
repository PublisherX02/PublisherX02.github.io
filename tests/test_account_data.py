"""Tests for the account_data fetcher (session_pnl_usd, sourced from
Alpaca's own GET /v2/account -- see AUDIT.md's E3/E4 follow-up decision:
pull PnL from Alpaca's account endpoint rather than compute it locally
from fills against live marks, reusing the calculation Alpaca already does
correctly instead of duplicating it."""

import socket

import pytest

from firewall import account_data


@pytest.fixture(autouse=True)
def _clear_cache():
    account_data._cache = None
    account_data._cache_fetched_at = None
    account_data._positions_cache = None
    yield
    account_data._cache = None
    account_data._cache_fetched_at = None
    account_data._positions_cache = None


@pytest.fixture(autouse=True)
def _paper_mode(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _account_body(equity: float, last_equity: float) -> bytes:
    import json

    return json.dumps({"equity": str(equity), "last_equity": str(last_equity)}).encode()


def test_session_pnl_is_equity_minus_last_equity(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_account_body(equity=98_500.0, last_equity=100_000.0)),
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.ok is True
    assert result.session_pnl_usd == pytest.approx(-1500.0)


def test_gain_day_is_positive_session_pnl(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_account_body(equity=101_200.0, last_equity=100_000.0)),
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.ok is True
    assert result.session_pnl_usd == pytest.approx(1200.0)


def test_result_carries_raw_equity_from_the_same_fetch(monkeypatch):
    """`equity` must come from the same GET /v2/account response
    session_pnl_usd is derived from, not a second fetch -- one urlopen call
    populates both fields."""
    calls = []
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: (
            calls.append(1),
            _FakeResponse(_account_body(equity=101_200.0, last_equity=100_000.0)),
        )[1],
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.equity == pytest.approx(101_200.0)
    assert len(calls) == 1


def test_result_carries_account_identity_and_last_equity(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(
            b'{"id":"competition-account","equity":"101200","last_equity":"100000"}'
        ),
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.account_id == "competition-account"
    assert result.last_equity == pytest.approx(100_000.0)


def test_timeout_returns_ok_false_not_a_raised_exception(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(account_data.urllib.request, "urlopen", _raise_timeout)

    result = account_data.fetch_session_pnl(timeout_seconds=5.0, cache_ttl_seconds=0)

    assert result.ok is False
    assert "timed out" in result.reason
    assert result.session_pnl_usd is None


def test_malformed_response_returns_ok_false_not_a_raised_exception(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"not json")
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.ok is False
    assert "malformed" in result.reason.lower()


def test_missing_equity_fields_returns_ok_false(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(b'{"status": "ACTIVE"}'),
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.ok is False
    assert "equity" in result.reason.lower()


def test_non_paper_mode_refuses_to_fetch_without_making_a_request(monkeypatch):
    # Same posture as firewall.proxy._require_paper_trade_mode: an unset or
    # non-paper ALPACA_PAPER_TRADE is refused, never defaulted to live --
    # this fetcher must not silently pull a live account's real PnL.
    monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    calls = []
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: calls.append(1) or _FakeResponse(_account_body(100.0, 100.0)),
    )

    result = account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert result.ok is False
    assert "paper" in result.reason.lower()
    assert calls == []


def test_successful_fetch_is_cached_within_ttl(monkeypatch):
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)
        return _FakeResponse(_account_body(equity=99_000.0, last_equity=100_000.0))

    monkeypatch.setattr(account_data.urllib.request, "urlopen", fake_urlopen)

    first = account_data.fetch_session_pnl(cache_ttl_seconds=300)
    second = account_data.fetch_session_pnl(cache_ttl_seconds=300)

    assert first.ok is True
    assert second == first
    assert len(calls) == 1


def test_cache_expires_after_ttl(monkeypatch):
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)
        return _FakeResponse(_account_body(equity=99_000.0, last_equity=100_000.0))

    monkeypatch.setattr(account_data.urllib.request, "urlopen", fake_urlopen)

    account_data.fetch_session_pnl(cache_ttl_seconds=0)
    account_data.fetch_session_pnl(cache_ttl_seconds=0)

    assert len(calls) == 2


def test_failed_fetch_is_not_cached(monkeypatch):
    # A transient outage shouldn't keep every subsequent call blind to PnL
    # for the full TTL after the API recovers (same reasoning as
    # market_data.fetch_daily_bars: only successful fetches are cached).
    calls = {"n": 0}

    def fake_urlopen(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise socket.timeout("timed out")
        return _FakeResponse(_account_body(equity=99_000.0, last_equity=100_000.0))

    monkeypatch.setattr(account_data.urllib.request, "urlopen", fake_urlopen)

    first = account_data.fetch_session_pnl(cache_ttl_seconds=300)
    second = account_data.fetch_session_pnl(cache_ttl_seconds=300)

    assert first.ok is False
    assert second.ok is True
    assert calls["n"] == 2


# --- fetch_positions: GET /v2/positions (position_cap's "positions" state
# key) -- verified live against the real paper account, 2026-08-29:
# `market_value` (string-typed) is the real field name -----------------


def _positions_body(*positions: tuple[str, float]) -> bytes:
    import json

    return json.dumps(
        [{"symbol": sym, "market_value": str(value)} for sym, value in positions]
    ).encode()


def test_fetch_positions_returns_symbol_to_market_value_map(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_positions_body(("AAPL", 639.4), ("SPY", 1200.0))),
    )

    result = account_data.fetch_positions(cache_ttl_seconds=0)

    assert result.ok is True
    assert result.positions == {"AAPL": 639.4, "SPY": 1200.0}
    assert result.fetched_at is not None


def test_fetch_positions_returns_quantities_for_fail_closed_cycle_sizing(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(
            b'[{"symbol":"AAPL","market_value":"639.4","qty":"2"}]'
        ),
    )
    result = account_data.fetch_positions(cache_ttl_seconds=0)
    assert result.ok is True
    assert result.quantities == {"AAPL": 2.0}


def test_fetch_positions_carries_intraday_pnl_attribution(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(
            b'[{"symbol":"AAPL","market_value":"639.4",'
            b'"unrealized_intraday_pl":"12.30"}]'
        ),
    )

    result = account_data.fetch_positions(cache_ttl_seconds=0)

    assert result.intraday_pnl == {"AAPL": pytest.approx(12.30)}


def test_fetch_positions_empty_list_is_ok_with_empty_map(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_positions_body()),
    )

    result = account_data.fetch_positions(cache_ttl_seconds=0)

    assert result.ok is True
    assert result.positions == {}


def test_fetch_positions_fetched_at_is_wall_clock_not_monotonic(monkeypatch):
    """position_cap compares this against OrderHistory event timestamps,
    which use time.time() (see firewall.proxy's state["now"]) -- if this
    were time.monotonic() instead, the comparison would be meaningless."""
    import time

    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_positions_body(("AAPL", 100.0))),
    )

    before = time.time()
    result = account_data.fetch_positions(cache_ttl_seconds=0)
    after = time.time()

    assert before <= result.fetched_at <= after


def test_fetch_positions_is_cached_within_ttl(monkeypatch):
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)
        return _FakeResponse(_positions_body(("AAPL", 100.0)))

    monkeypatch.setattr(account_data.urllib.request, "urlopen", fake_urlopen)

    first = account_data.fetch_positions(cache_ttl_seconds=300)
    second = account_data.fetch_positions(cache_ttl_seconds=300)

    assert second == first
    assert len(calls) == 1


def test_fetch_positions_non_paper_mode_refuses_to_fetch(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    calls = []
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: calls.append(1) or _FakeResponse(_positions_body()),
    )

    result = account_data.fetch_positions(cache_ttl_seconds=0)

    assert result.ok is False
    assert "paper" in result.reason.lower()
    assert calls == []


def test_fetch_positions_missing_market_value_field_fails_not_ok(monkeypatch):
    import json

    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(json.dumps([{"symbol": "AAPL"}]).encode()),
    )

    result = account_data.fetch_positions(cache_ttl_seconds=0)

    assert result.ok is False
    assert "market_value" in result.reason


def _orders_body(orders):
    import json
    return json.dumps(orders).encode()


def test_fetch_open_orders_aggregates_unfilled_cross_cycle_exposure(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_orders_body([
            {"id": "prior-aapl", "symbol": "AAPL", "side": "buy", "qty": "100",
             "filled_qty": "20", "limit_price": "200", "asset_class": "us_equity"},
            {"id": "prior-put", "symbol": "SPY261218P00600000", "side": "buy", "qty": "2",
             "filled_qty": "0", "limit_price": "5", "asset_class": "us_option"},
        ])),
    )

    result = account_data.fetch_open_orders({})

    assert result.ok is True
    assert len(result.orders) == 2
    assert result.orders[0].remaining_qty == 80
    assert result.aggregate_outstanding_notional == pytest.approx(17_000)


def test_pending_order_is_included_before_new_cycle_sizing():
    order = account_data.OpenOrder(
        "prior", "AAPL", "buy", 80, 200, 16_000, "us_equity"
    )
    committed, pending = account_data.include_pending_equity_orders(
        {"AAPL": 0}, (order,), ("AAPL",)
    )

    assert committed == {"AAPL": 80.0}
    assert pending == {"AAPL": 80.0}
    # A 100-share target now permits only 20 additional shares, not 100.
    assert int(100 - committed["AAPL"]) == 20


def test_fetch_open_orders_fails_closed_when_exposure_price_is_missing(monkeypatch):
    monkeypatch.setattr(
        account_data.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(_orders_body([
            {"id": "unknown", "symbol": "OUTSIDE", "side": "buy", "qty": "10",
             "filled_qty": "0", "asset_class": "us_equity"},
        ])),
    )
    result = account_data.fetch_open_orders({"AAPL": 200})
    assert result.ok is False
    assert "incomplete open-order exposure data" in result.reason


def test_fetch_open_orders_fails_closed_when_page_may_be_incomplete(monkeypatch):
    monkeypatch.setattr(
        account_data,
        "_fetch_open_orders_raw",
        lambda timeout, limit: [
            {"id": str(i), "symbol": "AAPL", "side": "buy", "qty": "1",
             "filled_qty": "0", "limit_price": "200", "asset_class": "us_equity"}
            for i in range(limit)
        ],
    )
    result = account_data.fetch_open_orders({}, limit=2)
    assert result.ok is False
    assert "completeness unknown" in result.reason


def test_open_order_transport_error_never_exposes_alpaca_credentials(monkeypatch):
    api_key = "alpaca-test-api-key-that-must-not-escape"
    secret_key = "alpaca-test-secret-key-that-must-not-escape"
    monkeypatch.setenv("ALPACA_API_KEY", api_key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret_key)

    def _raise(*args, **kwargs):
        raise RuntimeError(f"transport echoed {api_key} and {secret_key}")

    monkeypatch.setattr(account_data.urllib.request, "urlopen", _raise)
    result = account_data.fetch_open_orders({})

    assert result.ok is False
    assert api_key not in (result.reason or "")
    assert secret_key not in (result.reason or "")
    assert (result.reason or "").count("[REDACTED]") == 2

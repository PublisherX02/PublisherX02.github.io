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
    yield
    account_data._cache = None
    account_data._cache_fetched_at = None


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

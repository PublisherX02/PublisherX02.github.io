"""Tests for the shared market_data fetcher."""

import socket

from firewall import market_data


def test_timeout_returns_ok_false_not_a_raised_exception(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _raise_timeout)

    result = market_data.fetch_daily_bars("TIMEOUTTEST", 90, timeout_seconds=5.0)

    assert result.ok is False
    assert "timed out" in result.reason
    assert result.bars == []


def test_malformed_response_returns_ok_false_not_a_raised_exception(monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b"not json"

    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse()
    )

    result = market_data.fetch_daily_bars("MALFORMEDTEST", 90)

    assert result.ok is False
    assert "malformed" in result.reason.lower()


def test_empty_bars_response_returns_ok_false(monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b'{"bars": []}'

    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse()
    )

    result = market_data.fetch_daily_bars("EMPTYTEST", 90)

    assert result.ok is False
    assert "empty" in result.reason.lower()

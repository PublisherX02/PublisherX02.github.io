"""Tests for the shared market_data fetcher."""

import json
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


# --- fetch_option_latest_quote --------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def test_option_quote_timeout_returns_ok_false_not_a_raised_exception(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _raise_timeout)

    result = market_data.fetch_option_latest_quote("OPTQUOTETIMEOUT", timeout_seconds=5.0)

    assert result.ok is False
    assert "timed out" in result.reason
    assert result.quote is None


def test_option_quote_malformed_response_returns_ok_false(monkeypatch):
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"not json")
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEMALFORMED")

    assert result.ok is False
    assert "malformed" in result.reason.lower()


def test_option_quote_symbol_absent_from_response_returns_ok_false(monkeypatch):
    body = json.dumps({"snapshots": {}, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEABSENT")

    assert result.ok is False
    assert "no snapshot" in result.reason.lower()


def test_option_quote_missing_latest_quote_returns_ok_false(monkeypatch):
    # Verified against the real spec: latestQuote is NOT a required field
    # of option_snapshot -- a contract with no recent quote activity can
    # have a snapshot with no latestQuote at all.
    body = json.dumps(
        {"snapshots": {"OPTQUOTENOACTIVITY": {}}, "next_page_token": None}
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTENOACTIVITY")

    assert result.ok is False
    assert "no recent quote activity" in result.reason.lower()


def test_option_quote_non_positive_ask_returns_ok_false(monkeypatch):
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEZEROASK": {
                    "latestQuote": {"ap": 0.0, "bp": 0.0, "as": 1, "bs": 1, "t": "x"}
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEZEROASK")

    assert result.ok is False
    assert "non-usable" in result.reason.lower()


def test_option_quote_successful_fetch_parses_bid_ask(monkeypatch):
    # Real response shape verified against alpaca-mcp-server's bundled
    # market-data-api.json OpenAPI spec (checked 2026-08-25):
    # snapshots[symbol].latestQuote.{ap, bp} are ask/bid price.
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEOK260918C00220000": {
                    "latestQuote": {
                        "ap": 4.30,
                        "bp": 4.15,
                        "as": 91,
                        "bs": 16,
                        "ax": "B",
                        "bx": "C",
                        "c": "A",
                        "t": "2026-08-25T00:00:00Z",
                    }
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEOK260918C00220000")

    assert result.ok is True
    assert result.quote.bid == 4.15
    assert result.quote.ask == 4.30
    # No "greeks" key in this response at all -- delta must be None, not
    # a parse error, since greeks is separately optional from latestQuote.
    assert result.quote.delta is None
    # No "impliedVolatility" key either -- same optional-field treatment.
    assert result.quote.iv is None


def test_option_quote_parses_delta_from_greeks(monkeypatch):
    # Real response shape verified against the spec's option_snapshot
    # schema: snapshots[symbol].greeks.delta.
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEDELTA260918P00220000": {
                    "latestQuote": {"ap": 4.30, "bp": 4.15, "as": 1, "bs": 1, "t": "x"},
                    "greeks": {
                        "delta": -0.50,
                        "gamma": 0.05,
                        "theta": -0.10,
                        "vega": 0.20,
                        "rho": 0.01,
                    },
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEDELTA260918P00220000")

    assert result.ok is True
    assert result.quote.delta == -0.50


def test_option_quote_missing_delta_within_greeks_does_not_fail_the_quote(monkeypatch):
    # Malformed/incomplete greeks must not sink an otherwise-good bid/ask
    # -- option_spread_guard never touches delta and must still work.
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEBADGREEKS260918P00220000": {
                    "latestQuote": {"ap": 4.30, "bp": 4.15, "as": 1, "bs": 1, "t": "x"},
                    "greeks": {"gamma": 0.05},  # no "delta" key
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEBADGREEKS260918P00220000")

    assert result.ok is True
    assert result.quote.bid == 4.15
    assert result.quote.delta is None


def test_option_quote_parses_implied_volatility(monkeypatch):
    # Real response shape verified against the spec's option_snapshot
    # schema: snapshots[symbol].impliedVolatility -- a sibling of
    # latestQuote/greeks, not nested inside either.
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEIV260918P00220000": {
                    "latestQuote": {"ap": 4.30, "bp": 4.15, "as": 1, "bs": 1, "t": "x"},
                    "impliedVolatility": 0.42,
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEIV260918P00220000")

    assert result.ok is True
    assert result.quote.iv == 0.42


def test_option_quote_missing_implied_volatility_does_not_fail_the_quote(monkeypatch):
    # Same optional-field leniency as greeks/delta: a snapshot with no
    # impliedVolatility at all must not sink an otherwise-good bid/ask.
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTENOIV260918P00220000": {
                    "latestQuote": {"ap": 4.30, "bp": 4.15, "as": 1, "bs": 1, "t": "x"},
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTENOIV260918P00220000")

    assert result.ok is True
    assert result.quote.bid == 4.15
    assert result.quote.iv is None


def test_option_quote_malformed_implied_volatility_does_not_fail_the_quote(monkeypatch):
    body = json.dumps(
        {
            "snapshots": {
                "OPTQUOTEBADIV260918P00220000": {
                    "latestQuote": {"ap": 4.30, "bp": 4.15, "as": 1, "bs": 1, "t": "x"},
                    "impliedVolatility": "not-a-number",
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.fetch_option_latest_quote("OPTQUOTEBADIV260918P00220000")

    assert result.ok is True
    assert result.quote.iv is None


def test_option_quote_request_url_uses_symbol_and_feed(monkeypatch):
    captured = {}

    def _fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        body = json.dumps(
            {
                "snapshots": {
                    "OPTQUOTEURL260918C00220000": {
                        "latestQuote": {"ap": 1.0, "bp": 0.9, "as": 1, "bs": 1, "t": "x"}
                    }
                },
                "next_page_token": None,
            }
        ).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)

    market_data.fetch_option_latest_quote("OPTQUOTEURL260918C00220000", feed="indicative")

    assert "symbols=OPTQUOTEURL260918C00220000" in captured["url"]
    assert "feed=indicative" in captured["url"]
    assert captured["url"].startswith(
        "https://data.alpaca.markets/v1beta1/options/snapshots"
    )

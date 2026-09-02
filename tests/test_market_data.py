"""Tests for the shared market_data fetcher."""

import json
import socket

from firewall import market_data


def test_fetch_stock_latest_price_parses_latest_trade_and_marks_success(monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b'{"trade":{"p":325.25}}'

    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse()
    )

    result = market_data.fetch_stock_latest_price("AAPL")

    assert result.ok is True
    assert result.price == 325.25


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


# --- resolve_listed_contract -----------------------------------------------


def _fake_fetch_option_quotes(deltas: dict):
    """Build a fake fetch_option_quotes: symbols present in `deltas` come
    back as a usable quote with that delta; any other requested symbol
    comes back ok=False (no usable quote), matching a real "no recent
    quote activity" contract."""

    def fake(symbols, *, timeout_seconds=None, cache_ttl_seconds=None, feed=None):
        results = {}
        for symbol in symbols:
            if symbol in deltas:
                results[symbol] = market_data.OptionQuoteResult(
                    ok=True,
                    quote=market_data.OptionQuote(bid=1.0, ask=1.1, delta=deltas[symbol]),
                )
            else:
                results[symbol] = market_data.OptionQuoteResult(
                    ok=False, reason="no snapshot returned"
                )
        return results

    return fake


def _contract(symbol, strike, expiration_date, option_type="put", tradable=True):
    return {
        "symbol": symbol,
        "strike_price": str(strike),
        "expiration_date": expiration_date,
        "type": option_type,
        "tradable": tradable,
        "status": "active",
    }


def test_resolve_listed_contract_timeout_returns_ok_false(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _raise_timeout)

    result = market_data.resolve_listed_contract(
        "RESOLVETIMEOUT", 100.0, "2026-12-01", "P", timeout_seconds=5.0
    )

    assert result.ok is False
    assert "timed out" in result.reason
    assert result.contract is None


def test_resolve_listed_contract_malformed_response_returns_ok_false(monkeypatch):
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"not json")
    )

    result = market_data.resolve_listed_contract("RESOLVEMALFORMED", 100.0, "2026-12-01", "P")

    assert result.ok is False
    assert "malformed" in result.reason.lower()


def test_resolve_listed_contract_unrecognized_option_type_returns_ok_false():
    result = market_data.resolve_listed_contract("AAPL", 100.0, "2026-12-01", "X")

    assert result.ok is False
    assert "unrecognized option_type" in result.reason


def test_resolve_listed_contract_no_tradable_contracts_returns_ok_false(monkeypatch):
    body = json.dumps({"option_contracts": [], "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.resolve_listed_contract("NOCONTRACTS", 100.0, "2026-12-01", "P")

    assert result.ok is False
    assert "no tradable" in result.reason


def test_resolve_listed_contract_picks_nearest_expiry_then_delta_over_price(monkeypatch):
    """Two candidate expiries, several strikes each -- must pick the expiry
    nearest the target FIRST (price-only at this stage), then, AT that
    expiry, the strike whose DELTA lands closest to the corridor center --
    not the strike nearest the target by price."""
    # A distinct fake underlying symbol per test in this section --
    # resolve_listed_contract's chain cache is keyed by
    # (underlying_symbol, type, date window), which persists across tests
    # in this process; reusing "AAPL" with an overlapping now/target_expiry
    # would silently serve another test's cached response instead of
    # calling the fake urlopen this test installs.
    contracts = [
        # Nearer expiry (2026-12-01, target). 130.0 is closer to the
        # target strike (142.50) BY PRICE than 150.0 is -- but its delta
        # (-0.10) is farther from the 0.325 corridor center than 150.0's
        # (-0.30), so 150.0 must still be the final pick.
        _contract("NEARPICK261201P00130000", 130.0, "2026-12-01"),
        _contract("NEARPICK261201P00150000", 150.0, "2026-12-01"),
        # Farther expiry (2026-12-15) -- must NOT be picked (and its
        # strike's delta must never even be looked up), because expiry is
        # chosen before delta ever enters the decision.
        _contract("NEARPICK261215P00142000", 142.0, "2026-12-15"),
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    queried_symbols: list[str] = []

    def _tracking_quotes(symbols, **kwargs):
        queried_symbols.extend(symbols)
        return _fake_fetch_option_quotes(
            {"NEARPICK261201P00130000": -0.10, "NEARPICK261201P00150000": -0.30}
        )(symbols, **kwargs)

    monkeypatch.setattr(market_data, "fetch_option_quotes", _tracking_quotes)

    result = market_data.resolve_listed_contract(
        "NEARPICK", 142.50, "2026-12-01", "P", now=1_764_547_200.0  # 2025-12-01
    )

    assert result.ok is True
    assert result.contract.expiry == "2026-12-01"
    assert result.contract.strike == 150.0  # delta -0.30, closer to 0.325 than -0.10
    assert result.contract.occ_symbol == "NEARPICK261201P00150000"
    assert result.contract.delta == -0.30
    assert "NEARPICK261215P00142000" not in queried_symbols  # wrong expiry, never queried


def test_resolve_listed_contract_dte_floor_snaps_past_nearest_expiry(monkeypatch):
    """If the single nearest listed expiry violates min_dte, resolution
    must snap to the nearest listed expiry that ALSO satisfies the floor --
    not the literal nearest, and not a fallback that ignores the floor."""
    now = 1_735_689_600.0  # 2025-01-01T00:00:00Z
    contracts = [
        # Nearest to target (2025-01-10) but only 5 calendar days from
        # `now` -- violates a 7-day floor.
        _contract("DTESNAP250106P00100000", 100.0, "2025-01-06"),
        # 12 days out -- satisfies the floor, and is the nearest listed
        # expiry that does.
        _contract("DTESNAP250113P00100000", 100.0, "2025-01-13"),
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    monkeypatch.setattr(
        market_data,
        "fetch_option_quotes",
        _fake_fetch_option_quotes({"DTESNAP250113P00100000": -0.32}),
    )

    result = market_data.resolve_listed_contract(
        "DTESNAP", 100.0, "2025-01-10", "P", min_dte=7, now=now
    )

    assert result.ok is True
    assert result.contract.expiry == "2025-01-13"
    assert result.contract.occ_symbol == "DTESNAP250113P00100000"


def test_resolve_listed_contract_picks_delta_closest_to_corridor_center_not_extremes(monkeypatch):
    """A middle strike whose delta is closest to 0.325 must win over BOTH
    a too-shallow strike (delta far below the floor) and a too-deep one
    (delta well above the ceiling) -- proving this is a genuine
    closest-to-center pick, not an accidental floor- or ceiling-seeking
    one."""
    contracts = [
        _contract("DELTAMID261201P00090000", 90.0, "2026-12-01"),  # far OTM, shallow delta
        _contract("DELTAMID261201P00100000", 100.0, "2026-12-01"),  # near corridor center
        _contract("DELTAMID261201P00120000", 120.0, "2026-12-01"),  # deep ITM, high delta
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    monkeypatch.setattr(
        market_data,
        "fetch_option_quotes",
        _fake_fetch_option_quotes(
            {
                "DELTAMID261201P00090000": -0.08,  # |diff from 0.325| = 0.245
                "DELTAMID261201P00100000": -0.30,  # |diff from 0.325| = 0.025 -- closest
                "DELTAMID261201P00120000": -0.70,  # |diff from 0.325| = 0.375
            }
        ),
    )

    # target_strike (100.0) happens to equal the corridor-center contract's
    # own strike here on purpose -- the OTHER tests already prove price
    # and delta can diverge; this one isolates "does the middle delta win"
    # without conflating it with the price-anchor logic.
    result = market_data.resolve_listed_contract(
        "DELTAMID", 100.0, "2026-12-01", "P", now=1_764_547_200.0
    )

    assert result.ok is True
    assert result.contract.occ_symbol == "DELTAMID261201P00100000"
    assert result.contract.delta == -0.30


def test_resolve_listed_contract_no_searched_strike_has_usable_delta_returns_ok_false(monkeypatch):
    """Every price-nearest candidate at the chosen expiry lacks a usable
    delta (e.g. no recent quote activity on any of them) -- must fail
    closed with a clear reason, never fall back to a price-only pick."""
    contracts = [_contract("NODELTA261201P00100000", 100.0, "2026-12-01")]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    monkeypatch.setattr(market_data, "fetch_option_quotes", _fake_fetch_option_quotes({}))

    result = market_data.resolve_listed_contract(
        "NODELTA", 100.0, "2026-12-01", "P", now=1_764_547_200.0
    )

    assert result.ok is False
    assert "usable delta quote" in result.reason


def test_resolve_listed_contract_fails_closed_when_best_delta_still_below_floor(monkeypatch):
    """Even after exhausting the full listed chain at the chosen expiry,
    if the closest-to-center strike found still has |delta| below
    net_delta_floor's real 0.15 floor, resolution must fail closed --
    never propose a contract that rule would hard-block on arrival."""
    contracts = [
        _contract("BELOWFLOOR261201P00090000", 90.0, "2026-12-01"),
        _contract("BELOWFLOOR261201P00080000", 80.0, "2026-12-01"),
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    monkeypatch.setattr(
        market_data,
        "fetch_option_quotes",
        _fake_fetch_option_quotes(
            {
                "BELOWFLOOR261201P00090000": -0.05,
                "BELOWFLOOR261201P00080000": -0.09,  # closest to center of the two, still < 0.15
            }
        ),
    )

    result = market_data.resolve_listed_contract(
        "BELOWFLOOR", 100.0, "2026-12-01", "P", now=1_764_547_200.0
    )

    assert result.ok is False
    assert "structural floor" in result.reason
    assert "BELOWFLOOR261201P00080000" in result.reason


def test_resolve_listed_contract_does_not_expand_past_initial_window_when_center_already_bracketed(
    monkeypatch,
):
    """If the STARTING window's |delta| values already straddle the
    corridor center on both sides, resolution must stop there -- a
    farther-by-price strike outside that window is never queried, even if
    it exists."""
    contracts = [
        _contract("NOEXPAND261201P00099000", 99.0, "2026-12-01"),  # nearest by price
        _contract("NOEXPAND261201P00098000", 98.0, "2026-12-01"),  # 2nd-nearest
        _contract("NOEXPAND261201P00050000", 50.0, "2026-12-01"),  # far by price
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    queried_symbols: list[str] = []

    def _tracking_quotes(symbols, **kwargs):
        queried_symbols.extend(symbols)
        return _fake_fetch_option_quotes(
            {
                "NOEXPAND261201P00099000": -0.10,  # below center (0.325)
                "NOEXPAND261201P00098000": -0.50,  # above center -- brackets it
                "NOEXPAND261201P00050000": -0.325,  # would be a PERFECT match, must never be seen
            }
        )(symbols, **kwargs)

    monkeypatch.setattr(market_data, "fetch_option_quotes", _tracking_quotes)

    result = market_data.resolve_listed_contract(
        "NOEXPAND", 100.0, "2026-12-01", "P", strike_search_count=2, now=1_764_547_200.0
    )

    assert "NOEXPAND261201P00050000" not in queried_symbols  # never expanded to it
    assert result.ok is True
    assert result.contract.occ_symbol == "NOEXPAND261201P00098000"  # -0.50, closest of the two seen


def test_resolve_listed_contract_expands_search_when_initial_window_does_not_bracket_center(
    monkeypatch,
):
    """If every delta in the starting window sits on the SAME side of the
    corridor center, the search must widen -- reaching a farther-by-price
    strike that actually straddles the center -- rather than settling for
    the closest-available-so-far within an under-sized window."""
    contracts = [
        _contract("EXPAND261201P00099000", 99.0, "2026-12-01"),  # starting window
        _contract("EXPAND261201P00098000", 98.0, "2026-12-01"),  # starting window
        _contract("EXPAND261201P00050000", 50.0, "2026-12-01"),  # only reachable by expanding
    ]
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )
    queried_symbols: list[str] = []

    def _tracking_quotes(symbols, **kwargs):
        queried_symbols.extend(symbols)
        return _fake_fetch_option_quotes(
            {
                "EXPAND261201P00099000": -0.05,  # both starting-window deltas are
                "EXPAND261201P00098000": -0.08,  # well below the 0.325 center
                "EXPAND261201P00050000": -0.33,  # only found after expanding
            }
        )(symbols, **kwargs)

    monkeypatch.setattr(market_data, "fetch_option_quotes", _tracking_quotes)

    result = market_data.resolve_listed_contract(
        "EXPAND", 100.0, "2026-12-01", "P", strike_search_count=2, now=1_764_547_200.0
    )

    assert "EXPAND261201P00050000" in queried_symbols  # expansion actually reached it
    assert result.ok is True
    assert result.contract.occ_symbol == "EXPAND261201P00050000"
    assert result.contract.delta == -0.33


def test_resolve_listed_contract_no_expiry_satisfies_dte_floor_returns_ok_false(monkeypatch):
    now = 1_735_689_600.0  # 2025-01-01T00:00:00Z
    contracts = [_contract("DTENONE250103P00100000", 100.0, "2025-01-03")]  # 2 days out
    body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    result = market_data.resolve_listed_contract(
        "DTENONE", 100.0, "2025-01-10", "P", min_dte=7, now=now
    )

    assert result.ok is False
    assert "7-day DTE floor" in result.reason


def test_resolve_listed_contract_request_url_and_type_translation(monkeypatch):
    captured = {}

    def _fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        body = json.dumps({"option_contracts": [], "next_page_token": None}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)

    market_data.resolve_listed_contract("SPY", 500.0, "2026-12-01", "C", now=1_764_547_200.0)

    assert captured["url"].startswith("https://paper-api.alpaca.markets/v2/options/contracts")
    assert "underlying_symbols=SPY" in captured["url"]
    assert "type=call" in captured["url"]  # OCC "C" translated to Alpaca's "call"
    assert "status=active" in captured["url"]


def test_resolve_listed_contract_caches_raw_chain_fetch(monkeypatch):
    """Two resolutions with different target strikes but the same
    underlying/type/date-window share one HTTP round trip for the chain
    (contracts) fetch -- separate from, and unaffected by, delta lookup."""
    call_count = 0

    def _fake_urlopen(request, timeout):
        nonlocal call_count
        call_count += 1
        contracts = [_contract("CACHECHAIN261201P00100000", 100.0, "2026-12-01")]
        body = json.dumps({"option_contracts": contracts, "next_page_token": None}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        market_data,
        "fetch_option_quotes",
        _fake_fetch_option_quotes({"CACHECHAIN261201P00100000": -0.30}),
    )

    now = 1_764_547_200.0
    r1 = market_data.resolve_listed_contract("CACHECHAIN", 95.0, "2026-12-01", "P", now=now)
    r2 = market_data.resolve_listed_contract("CACHECHAIN", 105.0, "2026-12-01", "P", now=now)

    assert r1.ok is True and r2.ok is True
    assert call_count == 1


# --- fetch_option_quotes (batched) ------------------------------------------


def test_fetch_option_quotes_empty_list_returns_empty_dict():
    assert market_data.fetch_option_quotes([]) == {}


def test_fetch_option_quotes_parses_multiple_symbols_in_one_request(monkeypatch):
    captured = {}

    def _fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        body = json.dumps(
            {
                "snapshots": {
                    "BATCHA260918P00100000": {
                        "latestQuote": {"ap": 2.0, "bp": 1.9, "as": 1, "bs": 1, "t": "x"},
                        "greeks": {"delta": -0.30},
                    },
                    "BATCHB260918P00200000": {
                        "latestQuote": {"ap": 3.0, "bp": 2.9, "as": 1, "bs": 1, "t": "x"},
                        "greeks": {"delta": -0.45},
                    },
                },
                "next_page_token": None,
            }
        ).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)

    results = market_data.fetch_option_quotes(
        ["BATCHA260918P00100000", "BATCHB260918P00200000"]
    )

    assert "symbols=BATCHA260918P00100000%2CBATCHB260918P00200000" in captured["url"]
    assert results["BATCHA260918P00100000"].ok is True
    assert results["BATCHA260918P00100000"].quote.delta == -0.30
    assert results["BATCHB260918P00200000"].ok is True
    assert results["BATCHB260918P00200000"].quote.delta == -0.45


def test_fetch_option_quotes_symbol_missing_from_response_is_ok_false_for_that_symbol_only(
    monkeypatch,
):
    body = json.dumps(
        {
            "snapshots": {
                "BATCHPRESENT260918P00100000": {
                    "latestQuote": {"ap": 2.0, "bp": 1.9, "as": 1, "bs": 1, "t": "x"}
                }
            },
            "next_page_token": None,
        }
    ).encode()
    monkeypatch.setattr(
        market_data.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
    )

    results = market_data.fetch_option_quotes(
        ["BATCHPRESENT260918P00100000", "BATCHMISSING260918P00100000"]
    )

    assert results["BATCHPRESENT260918P00100000"].ok is True
    assert results["BATCHMISSING260918P00100000"].ok is False
    assert "no snapshot returned" in results["BATCHMISSING260918P00100000"].reason


def test_fetch_option_quotes_request_failure_marks_every_symbol_in_chunk_ok_false(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _raise_timeout)

    results = market_data.fetch_option_quotes(
        ["BATCHFAILA260918P00100000", "BATCHFAILB260918P00100000"], timeout_seconds=5.0
    )

    assert results["BATCHFAILA260918P00100000"].ok is False
    assert "timed out" in results["BATCHFAILA260918P00100000"].reason
    assert results["BATCHFAILB260918P00100000"].ok is False
    assert "timed out" in results["BATCHFAILB260918P00100000"].reason


def test_fetch_option_quotes_shares_cache_with_fetch_option_latest_quote(monkeypatch):
    """A symbol fetched via the batched call must be a cache hit for a
    later single-symbol fetch_option_latest_quote call -- no second HTTP
    round trip -- and vice versa."""
    call_count = 0

    def _fake_urlopen(request, timeout):
        nonlocal call_count
        call_count += 1
        body = json.dumps(
            {
                "snapshots": {
                    "CACHESHARE260918P00100000": {
                        "latestQuote": {"ap": 2.0, "bp": 1.9, "as": 1, "bs": 1, "t": "x"},
                        "greeks": {"delta": -0.30},
                    }
                },
                "next_page_token": None,
            }
        ).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)

    batch_result = market_data.fetch_option_quotes(["CACHESHARE260918P00100000"])
    assert batch_result["CACHESHARE260918P00100000"].ok is True

    single_result = market_data.fetch_option_latest_quote("CACHESHARE260918P00100000")
    assert single_result.ok is True
    assert single_result.quote.delta == -0.30
    assert call_count == 1  # the single-symbol call was a cache hit, not a second request


def test_fetch_option_quotes_chunks_requests_above_the_symbols_limit(monkeypatch):
    """Above the 100-symbols-per-request cap, requests are split into
    multiple chunked calls rather than violating the documented Alpaca
    limit or silently dropping symbols."""
    request_sizes: list[int] = []

    def _fake_urlopen(request, timeout):
        query = request.full_url.split("symbols=", 1)[1].split("&", 1)[0]
        symbols = query.split("%2C")
        request_sizes.append(len(symbols))
        snapshots = {
            s: {"latestQuote": {"ap": 1.0, "bp": 0.9, "as": 1, "bs": 1, "t": "x"}}
            for s in symbols
        }
        body = json.dumps({"snapshots": snapshots, "next_page_token": None}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(market_data.urllib.request, "urlopen", _fake_urlopen)

    symbols = [f"CHUNKTEST{i:03d}260918P00100000" for i in range(150)]
    results = market_data.fetch_option_quotes(symbols)

    assert len(results) == 150
    assert all(r.ok for r in results.values())
    assert request_sizes == [100, 50]  # two chunks, not one oversized request

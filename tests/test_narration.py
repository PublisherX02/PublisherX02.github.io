"""Unit and resilience test suite for AI Market Commentary (src/narration/market_brief.py)."""

import json
import os
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    from narration.market_brief import (
        DEFAULT_FEATHERLESS_MODEL,
        MarketBriefResult,
        build_featherless_prompt,
        build_narration_context,
        build_narration_context_from_cache,
        fetch_sec_edgar_current_ratios,
        generate_market_brief,
        generate_market_brief_sync,
        get_latest_cached_brief,
        schedule_market_brief_generation,
    )
except ImportError:
    from src.narration.market_brief import (
        DEFAULT_FEATHERLESS_MODEL,
        MarketBriefResult,
        build_featherless_prompt,
        build_narration_context,
        build_narration_context_from_cache,
        fetch_sec_edgar_current_ratios,
        generate_market_brief,
        generate_market_brief_sync,
        get_latest_cached_brief,
        schedule_market_brief_generation,
    )


def test_narration_context_structure():
    """Verify context contains SEC EDGAR current ratios, contractor groups, and weights."""
    sample_basket_state = {
        "weights": {
            "AAPL": {"target_w": 0.087, "current_w": 0.085, "drift": 0.002},
            "GD": {"target_w": 0.120, "current_w": 0.115, "drift": 0.005},
            "SPY": {"target_w": 0.200, "current_w": 0.198, "drift": 0.002},
        }
    }
    ctx = build_narration_context(
        sample_basket_state,
        sec_current_ratios={"GD": 1.38, "MSFT": 1.23},
        usaspending_links={"GD": {"total_subaward_dollar_flow_usd": 123.0}},
    )

    assert "sec_edgar_current_ratios" in ctx
    assert "usaspending_subaward_links" in ctx
    assert "portfolio_allocation_by_group" in ctx
    assert "basket_weights" in ctx
    assert ctx["basket_weights"]["AAPL"]["current_w"] == "8.5%"
    assert ctx["basket_weights"]["AAPL"]["drift"] == "0.2%"

    # Check key ratios
    ratios = ctx["sec_edgar_current_ratios"]
    assert "GD" in ratios
    assert "MSFT" in ratios
    assert ratios["GD"] > 0.5


def test_narration_context_uses_real_contract_evaluator_output_only():
    contract_links = {
        "GD": {
            "usable": True,
            "total_subaward_dollar_flow_usd": 123.0,
            "total_linked_entities": 2,
            "top_partners": [{"entity_name": "Example Partner"}],
        }
    }
    context = build_narration_context(
        sec_current_ratios={}, usaspending_links=contract_links
    )

    assert context["usaspending_subaward_links"] == contract_links
    assert "contractor_classifications" not in context
    assert "CONTRACTOR_CLASSIFICATIONS" not in json.dumps(context)


def test_narration_prompt_contains_mandatory_non_trading_constraints():
    """Verify prompt explicitly instructs the model NOT to provide trading recommendations."""
    ctx = build_narration_context(sec_current_ratios={}, usaspending_links={})
    messages = build_featherless_prompt(ctx)

    assert len(messages) == 2
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]

    # Check explicit anti-trading constraints in system prompt
    assert "NOT provide any trading advice" in system_content or "educational" in system_content
    assert "non-actionable" in system_content or "not a trading input" in system_content.lower()
    assert "recommendations" in system_content

    # Check user prompt
    assert "Do NOT include any trading advice" in user_content or "not include trading suggestions" in user_content.lower()


def test_narration_degrades_gracefully_on_network_failure():
    """Verify that a network failure/timeout degrades to fallback text without raising."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_brief.json"

        # Mock urllib.request.urlopen to simulate network connection error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = generate_market_brief_sync(
                api_key="fake_key",
                cache_file=cache_file,
                timeout_seconds=1.0,
            )

            assert isinstance(result, MarketBriefResult)
            assert result.ok is False
            assert "offline" in result.text.lower() or "unavailable" in result.text.lower()
            assert result.error_reason is not None


def test_narration_degrades_gracefully_on_http_error():
    """Verify HTTP 429/500/401 returns fallback text gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_brief.json"

        mock_http_error = urllib.error.HTTPError(
            url="https://api.featherless.ai/v1/chat/completions",
            code=429,
            msg="Rate limit exceeded",
            hdrs={},
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=mock_http_error):
            result = generate_market_brief_sync(
                api_key="fake_key",
                cache_file=cache_file,
            )

            assert isinstance(result, MarketBriefResult)
            assert result.ok is False
            assert "429" in (result.error_reason or "")
            assert "unavailable" in result.text.lower() or "rate" in result.text.lower()


def test_narration_uses_cached_brief_when_offline():
    """Verify that when network is down, the last persisted brief is returned."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_brief.json"

        # 1. Create a successful mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [
                {
                    "message": {
                        "content": "The basket maintains strong liquidity. Current ratios average above 1.2. Contract exposure remains diversified."
                    }
                }
            ]
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response):
            res_online = generate_market_brief_sync(
                api_key="valid_test_key",
                cache_file=cache_file,
            )
            assert res_online.ok is True
            assert "strong liquidity" in res_online.text

        # 2. Now simulate network crash, should retrieve cached brief
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network unreachable")):
            res_offline = generate_market_brief_sync(
                api_key="valid_test_key",
                cache_file=cache_file,
            )
            assert res_offline.cached is True
            assert "strong liquidity" in res_offline.text


def test_async_market_brief_generation_is_non_blocking():
    """Verify async wrapper runs without blocking and returns child result."""
    import asyncio

    async def _run():
        with patch.dict(os.environ, {"FEATHERLESS_API_KEY": ""}, clear=False):
            return await generate_market_brief(api_key=None)

    res = asyncio.run(_run())
    # With an existing successful cache, graceful degradation deliberately
    # returns that brief; without one it returns an explicit unavailable result.
    assert res.cached or res.ok is False
    assert res.error_reason is not None


def test_no_cache_headline_explicitly_says_generation_unavailable(tmp_path):
    result = get_latest_cached_brief(tmp_path / "missing.json")
    assert result.ok is False
    assert result.cached is False
    assert "generation is unavailable" in result.text.lower()
    assert "contractor exposure" not in result.text.lower()


def test_async_timeout_is_a_hard_process_bound(tmp_path):
    """A blocking call cannot survive past the advertised API-call limit."""
    import asyncio

    started = time.monotonic()
    result = asyncio.run(generate_market_brief(
        context_data={"test": True},
        api_key="unused",
        timeout_seconds=0.05,
        cache_file=tmp_path / "missing.json",
    ))
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "hard timeout after 0.05s" in (result.error_reason or "")
    # Process startup/teardown adds overhead, but the old +2 second ceiling
    # and abandoned thread would violate this comfortably.
    assert elapsed < 1.5


def test_context_builder_performs_no_live_source_calls():
    module = build_narration_context.__module__
    with patch(f"{module}.fetch_sec_edgar_current_ratios") as sec_fetch, patch(
        f"{module}.fetch_usaspending_contract_links"
    ) as spending_fetch:
        context = build_narration_context(
            {"weights": {}}, {"AAPL": 1.0}, {"GD": {"usable": True}}
        )

    sec_fetch.assert_not_called()
    spending_fetch.assert_not_called()
    assert context["sec_edgar_current_ratios"] == {"AAPL": 1.0}


def test_detached_generation_never_waits_for_featherless(tmp_path):
    """The cycle-facing scheduler only starts a worker and returns."""
    module = schedule_market_brief_generation.__module__
    with patch(f"{module}.subprocess.Popen") as popen:
        started = time.monotonic()
        queued = schedule_market_brief_generation(
            {"real_snapshot": True}, tmp_path / "brief.json"
        )
        elapsed = time.monotonic() - started

    assert queued is True
    assert elapsed < 0.25
    popen.assert_called_once()


def test_transport_error_never_exposes_featherless_key(tmp_path):
    secret = "featherless-test-secret-that-must-not-escape"
    module = generate_market_brief_sync.__module__
    with patch(
        f"{module}.urllib.request.urlopen",
        side_effect=RuntimeError(f"transport echoed credential {secret}"),
    ):
        result = generate_market_brief_sync(
            context_data={"market": "test"},
            api_key=secret,
            cache_file=tmp_path / "brief.json",
        )

    assert result.ok is False
    assert secret not in (result.error_reason or "")
    assert "[REDACTED]" in (result.error_reason or "")
    if (tmp_path / "brief.json").exists():
        assert secret not in (tmp_path / "brief.json").read_text(encoding="utf-8")

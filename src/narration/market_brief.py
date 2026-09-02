"""Isolated Plain-English AI Market Brief Module for Dashboard Narration.

This module generates an informational, plain-English summary of the basket's
fundamental liquidity and government contract / commercial diversification
using Featherless AI.

STRICT ISOLATION CONSTRAINTS:
1. NO TOOL-CALL ACCESS: This module cannot call any MCP or execution tools.
2. NO FEEDBACK LOOP: This module NEVER feeds data or signals back into
   `core_strategy` or the firewall policy engine.
3. READ-ONLY CONSUMER: It only reads already-computed data (SEC EDGAR current
   ratios, USASpending contract linkages, and basket weights/drift) and writes
   text for a human to read on the dashboard.
4. NON-BLOCKING & RESILIENT: The public async entry point runs the HTTP call
   in a disposable child process and terminates it at
   DEFAULT_TIMEOUT_SECONDS (18s). This is a real wall-clock cancellation,
   not merely urllib's socket-timeout hint or an abandoned worker thread.
"""

from __future__ import annotations

import asyncio
import io
import json
import multiprocessing
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Default model on Featherless (verified ungated, hosted, fast, and active --
# see https://featherless.ai/models/Qwen/Qwen2.5-7B-Instruct: 131K context,
# instruction-following, "Open Weights Warm" i.e. actively serving)
DEFAULT_FEATHERLESS_MODEL = "Qwen/Qwen2.5-7B-Instruct"
FEATHERLESS_API_URL = "https://api.featherless.ai/v1/chat/completions"
# Passed to urllib as its socket timeout and independently enforced as the
# child-process lifetime by generate_market_brief.
DEFAULT_TIMEOUT_SECONDS = 18.0


def _redact_error(value: object, *secrets: str) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
DEFAULT_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "last_market_brief.json"

# Pre-computed SEC EDGAR Current Ratio baselines (fallback cache if network offline)
KNOWN_SEC_EDGAR_CURRENT_RATIOS: dict[str, float] = {
    "GD": 1.38,
    "CACI": 1.48,
    "LDOS": 1.63,
    "NOC": 1.17,
    "BAH": 1.59,
    "MSFT": 1.23,
    "AAPL": 1.00,
    "ACN": 1.25,
    "J": 1.42,
}

# Federal-prime tickers with a validated USASpending.gov subaward search name
# (see scripts/usaspending_candidates.py's own CONTRACTOR_TICKER_MAP and this
# project's prior data-quality check: viable for these federal-prime names,
# not for consumer-facing tickers like AAPL -- so SPY/QQQ/AAPL/MSFT simply
# carry no USASpending figure rather than a guessed one).
USASPENDING_PRIME_TICKERS: dict[str, str] = {
    "GD": "GENERAL DYNAMICS",
    "LDOS": "LEIDOS",
    "CACI": "CACI",
    "NOC": "NORTHROP GRUMMAN",
    "BAH": "BOOZ ALLEN",
    "J": "JACOBS",
    "ACN": "ACCENTURE",
}
DEFAULT_USASPENDING_CACHE_FILE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "usaspending_contract_links_cache.json"
)
DEFAULT_SEC_EDGAR_CACHE_FILE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "sec_edgar_current_ratios_cache.json"
)
# Subaward filings don't change intraday; each ticker is its own ~15s live
# call (usaspending_candidates.py's own fixed timeout, not overridable from
# here), so querying all of them fresh every rebalance cycle would make this
# exactly the kind of live external dependency this module must stay off the
# critical path from. Refreshed at most once a day instead.
USASPENDING_CACHE_TTL_SECONDS = 86400.0


@dataclass
class MarketBriefResult:
    """Represents the generated AI market brief or fallback commentary."""

    text: str
    model: str
    timestamp: str
    ok: bool
    cached: bool = False
    latency_seconds: float = 0.0
    error_reason: str | None = None
    context_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketBriefResult:
        return cls(
            text=data.get("text", "Commentary unavailable."),
            model=data.get("model", "unknown"),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            ok=data.get("ok", False),
            cached=data.get("cached", True),
            latency_seconds=data.get("latency_seconds", 0.0),
            error_reason=data.get("error_reason"),
            context_summary=data.get("context_summary", {}),
        )


def _load_persisted_brief(cache_file: Path | str = DEFAULT_CACHE_FILE) -> MarketBriefResult | None:
    """Loads the last successful market brief from disk if present."""
    path = Path(cache_file)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        res = MarketBriefResult.from_dict(data)
        res.cached = True
        return res
    except Exception:
        return None


def _save_persisted_brief(brief: MarketBriefResult, cache_file: Path | str = DEFAULT_CACHE_FILE) -> None:
    """Persists a market brief to disk for instant dashboard display."""
    path = Path(cache_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(brief.to_dict(), indent=2), encoding="utf-8")
    except Exception:
        pass


def fetch_sec_edgar_current_ratios() -> dict[str, float]:
    """Return the validated cache-only ratios used by the narration layer.

    Live SEC refresh is intentionally not performed in the trading process;
    the previous timeout argument did not bound its multi-request loop.
    """
    return dict(KNOWN_SEC_EDGAR_CURRENT_RATIOS)


def fetch_usaspending_contract_links(
    tickers: dict[str, str] | None = None,
    cache_file: Path | str = DEFAULT_USASPENDING_CACHE_FILE,
    cache_ttl_seconds: float = USASPENDING_CACHE_TTL_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Real USASpending.gov subaward figures per federal-prime ticker, from
    usaspending_candidates.evaluate_company_contract_links -- the actual
    function this codebase already validated, not a static description and
    not a reimplementation of its query logic.

    Disk-cached with a 24h TTL (see USASPENDING_CACHE_TTL_SECONDS): each
    ticker not found in cache costs its own ~15s live network call, so a
    cold cache queries every configured ticker once and every other call
    this day reads the file. A ticker whose live call fails is simply
    omitted from the result -- never backfilled with a guessed or stale
    description.
    """
    tickers = tickers if tickers is not None else USASPENDING_PRIME_TICKERS
    path = Path(cache_file)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached.get("_fetched_at", 0.0) < cache_ttl_seconds:
                return cached.get("data", {})
        except Exception:
            pass

    try:
        workspace_root = Path(__file__).resolve().parent.parent.parent
        scripts_dir = workspace_root / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import usaspending_candidates  # type: ignore
    except Exception:
        # No live source and no usable cache -- narrate without USASpending
        # figures rather than fabricate any.
        return {}

    results: dict[str, dict[str, Any]] = {}
    for ticker, search_name in tickers.items():
        try:
            with redirect_stdout(io.StringIO()):  # the script is print()-heavy CLI output
                raw = usaspending_candidates.evaluate_company_contract_links(search_name, ticker)
            results[ticker] = {
                "usable": raw.get("usable", False),
                "total_subaward_dollar_flow_usd": raw.get("total_dollar_flow", 0.0),
                "total_linked_entities": raw.get("total_entities", 0),
                "top_partners": [
                    {
                        "entity_name": e.get("entity_name"),
                        "relationship": e.get("relationship"),
                        "total_amount_usd": e.get("total_amount_usd"),
                    }
                    for e in (raw.get("all_entities") or [])[:3]
                ],
            }
        except Exception:
            continue

    if results:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"_fetched_at": time.time(), "data": results}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return results


def load_precomputed_sec_ratios(
    cache_file: Path | str = DEFAULT_SEC_EDGAR_CACHE_FILE,
) -> dict[str, float]:
    """Read sec_edgar_current_ratio.py's persisted output; never fetch live."""
    try:
        data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.get("data", data).items()}
    except Exception:
        return {}


def load_precomputed_contract_links(
    cache_file: Path | str = DEFAULT_USASPENDING_CACHE_FILE,
) -> dict[str, dict[str, Any]]:
    """Read usaspending_candidates.py's persisted output; never fetch live."""
    try:
        data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        return data.get("data", {})
    except Exception:
        return {}


def build_narration_context(
    basket_state: dict[str, Any] | None = None,
    sec_current_ratios: dict[str, float] | None = None,
    usaspending_links: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compiles already-computed fundamental, contract, and portfolio data.

    `basket_state["weights"]` is expected in the shape
    `core_strategy.compute_weights_and_drift` returns --
    `{symbol: {"current_w", "target_w", "drift"}}` -- built once by
    core_strategy and passed in identically by both run_agent.py's
    post-cycle call and the dashboard's periodic refresh, so this function
    never has to guess or reconstruct "current state" itself.
    """
    # Formatting only: upstream computations and network access are forbidden
    # here. Callers pass snapshots produced elsewhere.
    ratios = dict(sec_current_ratios or {})
    usaspending_links = dict(usaspending_links or {})

    weights: dict[str, dict[str, str]] = {}
    current_w_by_symbol: dict[str, float] = {}
    if basket_state and "weights" in basket_state:
        for sym, info in basket_state["weights"].items():
            current_w = float(info.get("current_w", 0.0))
            current_w_by_symbol[sym] = current_w
            weights[sym] = {
                "current_w": f"{current_w:.1%}",
                "target_w": f"{float(info.get('target_w', 0.0)):.1%}",
                "drift": f"{float(info.get('drift', 0.0)):.1%}",
            }
    # else: no basket_state supplied (e.g. this module imported/tested
    # standalone) -- narrate without weight/drift figures rather than
    # fabricate a snapshot that could go stale the moment it's written.

    # Contract groupings, summed from real current weights when available.
    defense_primes = ["GD", "LDOS", "CACI", "NOC", "BAH", "J"]
    commercial_tech = ["AAPL", "MSFT", "ACN"]
    index_etfs = ["SPY", "QQQ"]
    allocation_by_group: dict[str, str] | None = None
    if current_w_by_symbol:
        cash_w = float(basket_state.get("cash_w", 0.0)) if basket_state else 0.0
        allocation_by_group = {
            f"Defense & Federal IT Contractors ({', '.join(defense_primes)})": (
                f"{sum(current_w_by_symbol.get(s, 0.0) for s in defense_primes):.1%}"
            ),
            f"Commercial Tech & Enterprise IT ({', '.join(commercial_tech)})": (
                f"{sum(current_w_by_symbol.get(s, 0.0) for s in commercial_tech):.1%}"
            ),
            f"Broad Index ETFs ({', '.join(index_etfs)})": (
                f"{sum(current_w_by_symbol.get(s, 0.0) for s in index_etfs):.1%}"
            ),
            "Cash Buffer": f"{cash_w:.1%}",
        }

    basket_universe = sorted(set(weights) | set(ratios) | set(usaspending_links))

    return {
        "basket_universe": basket_universe,
        "sec_edgar_current_ratios": ratios,
        "usaspending_subaward_links": usaspending_links,
        "portfolio_allocation_by_group": allocation_by_group,
        "basket_weights": weights,
    }


def build_narration_context_from_cache(
    basket_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build context from previously persisted source outputs, read-only."""
    return build_narration_context(
        basket_state=basket_state,
        sec_current_ratios=load_precomputed_sec_ratios(),
        usaspending_links=load_precomputed_contract_links(),
    )


def build_featherless_prompt(context_data: dict[str, Any]) -> list[dict[str, str]]:
    """Builds the strict prompt enforcing informational commentary without trading advice."""
    system_prompt = (
        "You are an informational financial market commentator for an automated trading dashboard. "
        "Your task is to write exactly four short, plain-English sentences summarizing the "
        "portfolio's current fundamental solvency/liquidity posture (based on SEC EDGAR current ratios) "
        "and its government contract versus commercial diversification.\n\n"
        "STRICT CONSTRAINTS (MANDATORY):\n"
        "1. This is strictly non-actionable, educational market commentary for human dashboard monitoring.\n"
        "2. You MUST NOT provide any trading advice, investment recommendations, buy/sell ratings, or execution suggestions.\n"
        "3. Do NOT suggest rebalancing actions or speculate on future prices.\n"
        "4. Focus entirely on describing the factual fundamental health and contract exposure mix in clear language.\n"
        "5. Output exactly four complete sentences, each ending with a period. Do not combine them with semicolons or bullets."
    )

    user_prompt = (
        f"Here is the current portfolio data:\n"
        f"{json.dumps(context_data, indent=2)}\n\n"
        f"Please provide exactly four short sentences in plain English about the basket's fundamental liquidity posture "
        f"and contract-exposure diversification. End every sentence with a period. Do NOT include any trading advice "
        f"or action recommendations."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _unavailable_result(
    reason: str,
    model: str,
    context_data: dict[str, Any],
    cache_file: Path | str,
    latency_seconds: float = 0.0,
) -> MarketBriefResult:
    """The one 'commentary unavailable' fallback, used by every failure path
    (missing key, HTTP error, transport exception, hard timeout). Prefers
    the last real cached brief; only when none exists yet does it fall back
    to a headline that plainly says generation failed -- never a canned
    analysis with specific numbers standing in for real output, which a
    reader skimming just the bold headline (not a small status caption)
    could mistake for a live result.
    """
    cached = _load_persisted_brief(cache_file)
    if cached:
        cached.error_reason = reason
        return cached
    return MarketBriefResult(
        text=(
            f"AI commentary generation is currently unavailable ({reason}). "
            "No cached brief exists yet from a prior successful run."
        ),
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        ok=False,
        cached=False,
        latency_seconds=round(latency_seconds, 2),
        error_reason=reason,
        context_summary=context_data,
    )


def generate_market_brief_sync(
    basket_state: dict[str, Any] | None = None,
    context_data: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_FEATHERLESS_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_file: Path | str = DEFAULT_CACHE_FILE,
) -> MarketBriefResult:
    """Synchronous generator for the AI market brief with fail-open fallback."""
    start_time = time.time()
    if context_data is None:
        context_data = build_narration_context(basket_state)

    # 1. Resolve API Key
    key = api_key or os.environ.get("FEATHERLESS_API_KEY", "")
    if not key:
        return _unavailable_result(
            "FEATHERLESS_API_KEY not configured", model, context_data, cache_file
        )

    # 2. Build Request
    messages = build_featherless_prompt(context_data)
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 250,
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        FEATHERLESS_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "MCP-Trade-Firewall/1.0",
        },
    )

    # 3. Time-bounded external call with graceful degradation
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency = time.time() - start_time
            content = data["choices"][0]["message"]["content"].strip()
            sentence_count = len(re.findall(r"[.!?](?:\s|$)", content))
            if not 3 <= sentence_count <= 5:
                return _unavailable_result(
                    f"model returned {sentence_count} sentences; expected 3-5",
                    model, context_data, cache_file, latency,
                )

            result = MarketBriefResult(
                text=content,
                model=model,
                timestamp=datetime.now(timezone.utc).isoformat(),
                ok=True,
                cached=False,
                latency_seconds=round(latency, 2),
                error_reason=None,
                context_summary=context_data,
            )
            # Persist successful brief
            _save_persisted_brief(result, cache_file)
            return result

    except urllib.error.HTTPError as err:
        latency = time.time() - start_time
        err_msg = f"HTTP {err.code}: {_redact_error(err.reason, key)}"
        return _unavailable_result(err_msg, model, context_data, cache_file, latency)

    except Exception as exc:
        latency = time.time() - start_time
        err_msg = _redact_error(exc, key)
        return _unavailable_result(err_msg, model, context_data, cache_file, latency)


async def generate_market_brief(
    basket_state: dict[str, Any] | None = None,
    context_data: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_FEATHERLESS_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_file: Path | str = DEFAULT_CACHE_FILE,
) -> MarketBriefResult:
    """Generate off-loop and hard-cancel the HTTP worker at the stated limit.

    The request runs in a spawned child process. At ``timeout_seconds`` that
    process is terminated and joined, so no blocking request keeps running
    after this coroutine returns. Callers may pass their already-built shared
    narration context via ``context_data``.
    """
    if context_data is None:
        context_data = build_narration_context(basket_state)
    mp = multiprocessing.get_context("spawn")
    receiver, sender = mp.Pipe(duplex=False)
    process = mp.Process(
        target=_generate_market_brief_process,
        args=(sender, context_data, api_key, model, timeout_seconds, str(cache_file)),
        daemon=True,
    )
    process.start()
    sender.close()
    has_result = await asyncio.to_thread(receiver.poll, timeout_seconds)
    if not has_result:
        process.terminate()
        await asyncio.to_thread(process.join)
        receiver.close()
        return _unavailable_result(
            f"hard timeout after {timeout_seconds:g}s (no response from Featherless)",
            model, context_data, cache_file, latency_seconds=timeout_seconds,
        )
    try:
        result = MarketBriefResult.from_dict(receiver.recv())
        await asyncio.to_thread(process.join)
        return result
    except (EOFError, OSError) as exc:
        return _unavailable_result(
            f"commentary worker failed: {exc}", model, context_data, cache_file
        )
    finally:
        receiver.close()


def _generate_market_brief_process(
    sender: Any,
    context_data: dict[str, Any],
    api_key: str | None,
    model: str,
    timeout_seconds: float,
    cache_file: str,
) -> None:
    """Child-process entry point; top-level so Windows can spawn it."""
    try:
        result = generate_market_brief_sync(
            context_data=context_data,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            cache_file=cache_file,
        )
        sender.send(result.to_dict())
    finally:
        sender.close()


def get_latest_cached_brief(cache_file: Path | str = DEFAULT_CACHE_FILE) -> MarketBriefResult:
    """Returns the most recent persisted brief or a safe default status."""
    cached = _load_persisted_brief(cache_file)
    if cached:
        return cached

    return MarketBriefResult(
        text=(
            "AI commentary generation is unavailable because no successful "
            "brief has been cached yet."
        ),
        model=DEFAULT_FEATHERLESS_MODEL,
        timestamp=datetime.now(timezone.utc).isoformat(),
        ok=False,
        cached=False,
        latency_seconds=0.0,
        error_reason="no cached brief exists yet",
        context_summary={},
    )


def schedule_market_brief_generation(
    context_data: dict[str, Any],
    cache_file: Path | str = DEFAULT_CACHE_FILE,
) -> bool:
    """Launch a detached cache-refresh worker and return immediately.

    The trading process neither awaits nor owns the worker, so Featherless
    latency, DNS failures, and process teardown cannot extend the cycle.
    """
    pending_dir = Path(cache_file).parent / "narration_pending"
    try:
        pending_dir.mkdir(parents=True, exist_ok=True)
        context_file = pending_dir / f"context-{os.getpid()}-{time.time_ns()}.json"
        context_file.write_text(json.dumps(context_data), encoding="utf-8")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, "-m", __name__, "--context-file", str(context_file),
             "--cache-file", str(cache_file)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        return True
    except Exception:
        return False


def _worker_main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--context-file", required=True)
    parser.add_argument("--cache-file", required=True)
    args = parser.parse_args()
    context_file = Path(args.context_file)
    try:
        context = json.loads(context_file.read_text(encoding="utf-8"))
        asyncio.run(generate_market_brief(context_data=context, cache_file=args.cache_file))
    finally:
        context_file.unlink(missing_ok=True)


if __name__ == "__main__":
    _worker_main()

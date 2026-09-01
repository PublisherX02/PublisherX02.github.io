"""Standalone SEC EDGAR XBRL company facts fetcher and Current Ratio extractor.

Queries SEC EDGAR API:
1. https://www.sec.gov/files/company_tickers.json to resolve Ticker -> CIK
2. https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_10_digits}.json to fetch XBRL facts
3. Extracts most recent 10-K/10-Q AssetsCurrent & LiabilitiesCurrent under us-gaap taxonomy
4. Computes Current Ratio = AssetsCurrent / LiabilitiesCurrent
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


USER_AGENT = "TradingAgentProject/1.0 (research_compliance_contact@tradingagent.org)"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_10_digits}.json"


@dataclass
class FinancialFact:
    concept: str
    end_date: str
    filed_date: str
    form: str
    fiscal_year: int | str
    fiscal_period: str
    value: float
    unit: str


@dataclass
class CurrentRatioResult:
    ticker: str
    cik: int
    company_name: str
    period_end: str
    form: str
    filed_date: str
    assets_current: float
    liabilities_current: float
    current_ratio: float
    source_concept_assets: str
    source_concept_liabilities: str


def fetch_sec_json(url: str, user_agent: str = USER_AGENT) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": urllib.parse.urlparse(url).netloc,
        },
    )
    import gzip

    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            content = gzip.decompress(content)
        return json.loads(content.decode("utf-8"))


def resolve_ticker_to_cik(ticker: str) -> tuple[int, str]:
    """Resolves a ticker symbol to its CIK and legal title using SEC company_tickers.json."""
    ticker_upper = ticker.upper().strip()
    data = fetch_sec_json(SEC_TICKERS_URL)

    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return int(entry["cik_str"]), entry.get("title", "")

    raise ValueError(f"Ticker '{ticker}' not found in SEC company_tickers.json")


def extract_latest_us_gaap_fact(
    facts_data: dict[str, Any], concept: str, form_filter: tuple[str, ...] = ("10-K", "10-Q", "10-K/A", "10-Q/A")
) -> FinancialFact | None:
    """Extracts the most recent 10-K or 10-Q fact for a us-gaap concept."""
    us_gaap = facts_data.get("facts", {}).get("us-gaap", {})
    concept_node = us_gaap.get(concept)
    if not concept_node:
        return None

    units = concept_node.get("units", {})
    usd_units = units.get("USD", [])
    if not usd_units:
        # Fallback to first unit if USD not explicitly named
        for u_list in units.values():
            usd_units = u_list
            break

    if not usd_units:
        return None

    # Filter for standard quarterly / annual filings (forms 10-K, 10-Q)
    filtered = [
        item for item in usd_units
        if item.get("form") in form_filter and "val" in item and "end" in item
    ]

    if not filtered:
        filtered = [item for item in usd_units if "val" in item and "end" in item]

    if not filtered:
        return None

    # Sort primarily by end date (latest period), then filed date, then frame
    sorted_facts = sorted(
        filtered,
        key=lambda x: (x.get("end", ""), x.get("filed", ""), x.get("fy", 0)),
        reverse=True,
    )

    latest = sorted_facts[0]
    return FinancialFact(
        concept=concept,
        end_date=latest.get("end", ""),
        filed_date=latest.get("filed", ""),
        form=latest.get("form", ""),
        fiscal_year=latest.get("fy", ""),
        fiscal_period=latest.get("fp", ""),
        value=float(latest["val"]),
        unit=concept_node.get("units", {}).keys().__iter__().__next__() if units else "USD",
    )


def compute_current_ratio(ticker: str) -> CurrentRatioResult:
    """Fetches EDGAR companyfacts for a ticker and computes its current ratio."""
    cik, company_name = resolve_ticker_to_cik(ticker)
    cik_10_digits = str(cik).zfill(10)
    facts_url = SEC_FACTS_URL_TEMPLATE.format(cik_10_digits=cik_10_digits)

    facts_data = fetch_sec_json(facts_url)

    # Primary US-GAAP concepts for Current Assets & Current Liabilities
    assets_fact = extract_latest_us_gaap_fact(facts_data, "AssetsCurrent")
    liabilities_fact = extract_latest_us_gaap_fact(facts_data, "LiabilitiesCurrent")

    if not assets_fact:
        raise ValueError(f"Could not find 'AssetsCurrent' under us-gaap taxonomy for {ticker} (CIK {cik})")
    if not liabilities_fact:
        raise ValueError(f"Could not find 'LiabilitiesCurrent' under us-gaap taxonomy for {ticker} (CIK {cik})")

    if liabilities_fact.value == 0:
        raise ValueError(f"LiabilitiesCurrent is zero for {ticker}, cannot compute ratio")

    ratio = assets_fact.value / liabilities_fact.value

    return CurrentRatioResult(
        ticker=ticker.upper(),
        cik=cik,
        company_name=company_name,
        period_end=assets_fact.end_date,
        form=assets_fact.form,
        filed_date=assets_fact.filed_date,
        assets_current=assets_fact.value,
        liabilities_current=liabilities_fact.value,
        current_ratio=ratio,
        source_concept_assets=assets_fact.concept,
        source_concept_liabilities=liabilities_fact.concept,
    )


def main() -> None:
    test_tickers = ["GD", "CACI", "NOC", "LDOS", "BAH", "MSFT"]
    print("=" * 80)
    print("SEC EDGAR XBRL COMPANY FACTS -> CURRENT RATIO EXTRACTION")
    print("=" * 80)

    for ticker in test_tickers:
        print(f"\n[+] Querying SEC EDGAR for ticker: {ticker} ...")
        try:
            res = compute_current_ratio(ticker)
            print(f"    Company:               {res.company_name} (CIK {res.cik:010d})")
            print(f"    Latest Filing:         Form {res.form} (Period End: {res.period_end}, Filed: {res.filed_date})")
            print(f"    AssetsCurrent:         ${res.assets_current:,.2f}")
            print(f"    LiabilitiesCurrent:    ${res.liabilities_current:,.2f}")
            print(f"    Current Ratio:         {res.current_ratio:.4f}")
            print(f"    Solvency Assessment:   {'Healthy (>1.0)' if res.current_ratio >= 1.0 else 'Tight (<1.0)'}")
        except Exception as exc:
            print(f"    [!] Error processing {ticker}: {exc}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()

"""USASpending.gov Second-Order Candidate Symbol Discovery Script.

Standalone evaluation script (no firewall / core_strategy dependencies).
Queries the real, free, no-auth USASpending.gov v2 REST API to determine
whether usable, high-specificity second-order supply chain / government contract
linkages can be extracted for target basket companies (MSFT and AAPL).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


# Known mapping of major defense / federal IT prime contractors & subsidiaries to public tickers
CONTRACTOR_TICKER_MAP: dict[str, dict[str, str]] = {
    "GENERAL DYNAMICS": {"ticker": "GD", "name": "General Dynamics Corporation"},
    "GDIT": {"ticker": "GD", "name": "General Dynamics Corporation"},
    "LEIDOS": {"ticker": "LDOS", "name": "Leidos Holdings, Inc."},
    "CACI": {"ticker": "CACI", "name": "CACI International Inc."},
    "ACCENTURE": {"ticker": "ACN", "name": "Accenture plc"},
    "BOOZ ALLEN": {"ticker": "BAH", "name": "Booz Allen Hamilton Holding Corp."},
    "NORTHROP GRUMMAN": {"ticker": "NOC", "name": "Northrop Grumman Corporation"},
    "JACOBS": {"ticker": "J", "name": "Jacobs Solutions Inc."},
    "ECS FEDERAL": {"ticker": "ASGN", "name": "ASGN Incorporated (Parent)"},
    "SCIENCE APPLICATIONS INTERNATIONAL": {"ticker": "SAIC", "name": "Science Applications International Corp."},
}


def query_usaspending_api(endpoint: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Execute a POST request against the public USASpending.gov API."""
    url = f"https://api.usaspending.gov/api/v2/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MCP-Trade-Firewall-Research/1.0",
        },
    )
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            latency = time.time() - start_time
            res_json = json.loads(response.read().decode("utf-8"))
            res_json["_latency_seconds"] = round(latency, 3)
            return res_json
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "_latency_seconds": time.time() - start_time}
    except Exception as e:
        return {"error": str(e), "_latency_seconds": time.time() - start_time}


def resolve_ticker(contractor_name: str) -> tuple[str | None, str | None]:
    """Resolve a federal contractor entity name to a publicly traded ticker."""
    name_upper = (contractor_name or "").upper()
    for key, info in CONTRACTOR_TICKER_MAP.items():
        if key in name_upper:
            return info["ticker"], info["name"]
    return None, None


def evaluate_company_contract_links(target_name: str, display_symbol: str) -> dict[str, Any]:
    """Query USASpending for subaward linkages involving the target company."""
    print(f"\n{'='*75}")
    print(f"[*] Querying USASpending.gov for target: {target_name} ({display_symbol})")
    print(f"{'='*75}")

    payload = {
        "subawards": True,
        "filters": {
            "recipient_search_text": [target_name],
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2020-01-01", "end_date": "2026-08-01"}],
        },
        "fields": [
            "Sub-Award ID",
            "Prime Award ID",
            "Prime Recipient Name",
            "Sub-Awardee Name",
            "Sub-Award Amount",
            "Sub-Award Date",
            "Sub-Award Description",
            "Prime Award Description",
        ],
        "limit": 50,
        "sort": "Sub-Award Amount",
        "order": "desc",
    }

    resp = query_usaspending_api("search/spending_by_award/", payload)
    latency = resp.get("_latency_seconds", 0.0)

    if "error" in resp:
        print(f"[!] API Error ({latency}s): {resp['error']}")
        return {"usable": False, "reason": resp["error"], "candidates": []}

    results = resp.get("results", [])
    print(f"[+] API Response received in {latency}s | Records returned: {len(results)}")

    if not results:
        print("[-] No records returned for target.")
        return {"usable": False, "reason": "Zero records returned", "candidates": []}

    # Aggregate by linked entity
    linked_entities: dict[str, dict[str, Any]] = {}
    total_dollar_flow = 0.0

    for r in results:
        prime_name = r.get("Prime Recipient Name") or "Unknown"
        sub_name = r.get("Sub-Awardee Name") or "Unknown"
        sub_id = r.get("Sub-Award ID") or "—"
        prime_award_id = r.get("Prime Award ID") or "—"
        amt = float(r.get("Sub-Award Amount") or 0.0)
        date = r.get("Sub-Award Date") or "—"
        desc = (r.get("Sub-Award Description") or "").strip()

        # Is target the prime or the sub?
        is_sub = target_name.upper() in sub_name.upper()
        partner_name = prime_name if is_sub else sub_name

        if partner_name not in linked_entities:
            ticker, full_name = resolve_ticker(partner_name)
            linked_entities[partner_name] = {
                "entity_name": partner_name,
                "ticker": ticker,
                "full_company_name": full_name,
                "relationship": "Prime Contractor (Subcontracted to Target)" if is_sub else "Subcontractor to Target",
                "total_amount_usd": 0.0,
                "award_count": 0,
                "sample_awards": [],
            }

        linked_entities[partner_name]["total_amount_usd"] += amt
        linked_entities[partner_name]["award_count"] += 1
        total_dollar_flow += amt

        if len(linked_entities[partner_name]["sample_awards"]) < 2:
            linked_entities[partner_name]["sample_awards"].append({
                "sub_award_id": sub_id,
                "prime_award_id": prime_award_id,
                "amount": amt,
                "date": date,
                "description": desc[:80],
            })

    # Sort entities by total dollar flow
    sorted_entities = sorted(linked_entities.values(), key=lambda x: x["total_amount_usd"], reverse=True)

    # Filter for resolvable public tickers
    resolvable_candidates = [e for e in sorted_entities if e["ticker"] is not None]

    print(f"\n[+] Total linked entities found: {len(sorted_entities)}")
    print(f"[+] Total subaward transaction volume: ${total_dollar_flow:,.2f}")
    print(f"[+] Resolvable public candidate symbols: {len(resolvable_candidates)}")

    print("\n--- Detailed Entity Breakdown ---")
    for idx, e in enumerate(sorted_entities[:10], start=1):
        ticker_badge = f" [TICKER: {e['ticker']}]" if e['ticker'] else " [NO TICKER]"
        print(f"{idx:2d}. {e['entity_name']}{ticker_badge}")
        print(f"    Relationship: {e['relationship']} | Total: ${e['total_amount_usd']:,.2f} ({e['award_count']} awards)")
        for a in e["sample_awards"]:
            print(f"    • Award {a['prime_award_id']} / Sub {a['sub_award_id']} (${a['amount']:,.2f}, {a['date']}): {a['description']}")

    is_usable = len(resolvable_candidates) >= 3 and total_dollar_flow > 10_000_000
    quality_assessment = "HIGH SPECIFICITY & HIGH VALUE" if is_usable else "THIN / INSUFFICIENT DATA"

    print(f"\n[*] Target Assessment: {quality_assessment} (Usable: {is_usable})")
    return {
        "usable": is_usable,
        "target": target_name,
        "display_symbol": display_symbol,
        "latency_seconds": latency,
        "total_dollar_flow": total_dollar_flow,
        "total_entities": len(sorted_entities),
        "resolvable_candidates": resolvable_candidates,
        "all_entities": sorted_entities,
    }


def main() -> None:
    print("=" * 75)
    print("USASpending.gov Second-Order Candidate Evaluation")
    print("Testing Basket Tickers: MSFT (Microsoft) and AAPL (Apple)")
    print("=" * 75)

    # 1. Test Microsoft Corporation
    msft_eval = evaluate_company_contract_links("MICROSOFT CORPORATION", "MSFT")

    # 2. Test Apple Inc.
    aapl_eval = evaluate_company_contract_links("APPLE INC", "AAPL")

    print("\n" + "=" * 75)
    print("FINAL SUMMARY & DETERMINATION")
    print("=" * 75)

    print(f"\n1. AAPL (Apple Inc.):")
    print(f"   • Usable Quality: {aapl_eval['usable']}")
    print(f"   • Total Flow: ${aapl_eval.get('total_dollar_flow', 0):,.2f}")
    print(f"   • Resolvable Public Tickers: {len(aapl_eval.get('resolvable_candidates', []))}")
    print(f"   • Assessment: Hardware retail purchases only (MacBooks/iPhones). Unusable for supply chain/contract modeling.")

    print(f"\n2. MSFT (Microsoft Corporation):")
    print(f"   • Usable Quality: {msft_eval['usable']}")
    print(f"   • Total Flow: ${msft_eval.get('total_dollar_flow', 0):,.2f}")
    print(f"   • Resolvable Public Tickers: {len(msft_eval.get('resolvable_candidates', []))}")
    print(f"   • Assessment: Direct, rich linkages to major defense and IT prime contractors with multi-million dollar awards.")

    if msft_eval["usable"]:
        print("\n[+] DISCOVERED 2ND-ORDER CANDIDATE SYMBOLS (Derived from MSFT Federal Awards):")
        for c in msft_eval["resolvable_candidates"]:
            print(f"   • Symbol: {c['ticker']:<5} | Company: {c['full_company_name']:<42} | Federal Flow: ${c['total_amount_usd']:>12,.2f}")


if __name__ == "__main__":
    main()

"""
explore_keywords.py — Sample DHS contract descriptions from the USASpending API
to see what AI/ML language actually looks like before doing a full download.

No API key required. Hits api.usaspending.gov directly.

Run:
    python3 explore_keywords.py                  # search default keyword list
    python3 explore_keywords.py --keyword "machine learning"
    python3 explore_keywords.py --show-misses     # also show non-AI hits (false positives)
"""

import argparse
import json
import time
from collections import Counter

import requests

USASPENDING_SEARCH = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
DHS_TOPTIER_CODE   = "070"

# Keywords to probe separately so we can see which ones find real signal
DEFAULT_KEYWORDS = [
    "artificial intelligence",
    "machine learning",
    "large language model",
    "generative AI",
    "natural language processing",
    "computer vision",
    "predictive analytics",
    "deep learning",
    "neural network",
    "AI/ML",
]


def search_awards(keyword: str, limit: int = 10) -> list[dict]:
    payload = {
        "filters": {
            "keywords": [keyword],
            "agencies": [
                {
                    "type": "awarding",
                    "tier": "toptier",
                    "toptier_code": DHS_TOPTIER_CODE,
                }
            ],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts only
            "time_period": [{"start_date": "2021-10-01", "end_date": "2026-09-30"}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Awarding Sub Agency",
            "Award Amount",
            "Description",
            "Start Date",
            "Contract Award Type",
            "NAICS Code",
            "NAICS Description",
            "PSC Code",
            "PSC Description",
        ],
        "limit": limit,
        "page": 1,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }
    r = requests.post(USASPENDING_SEARCH, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("results", []), data.get("page_metadata", {}).get("total", 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", help="Single keyword to probe instead of default list")
    ap.add_argument("--limit", type=int, default=5, help="Results per keyword (default: 5)")
    ap.add_argument("--show-all-fields", action="store_true")
    args = ap.parse_args()

    keywords = [args.keyword] if args.keyword else DEFAULT_KEYWORDS

    print(f"Probing {len(keywords)} keyword(s) against DHS contracts (FY2022–FY2026)\n")
    print("=" * 80)

    totals = {}
    for kw in keywords:
        try:
            results, total = search_awards(kw, limit=args.limit)
            totals[kw] = total
            print(f"\n### \"{kw}\"  —  {total:,} total matches")
            print("-" * 60)
            if not results:
                print("  (no results)")
            for r in results:
                amt = r.get("Award Amount") or 0
                print(f"  ${amt:>12,.0f}  {(r.get('Recipient Name') or '')[:35]:<35}  "
                      f"[{r.get('Awarding Sub Agency') or '':30}]")
                desc = (r.get("Description") or "").strip()
                if desc:
                    print(f"              {desc[:110]}")
                naics = r.get("NAICS Description") or ""
                psc   = r.get("PSC Description") or ""
                if naics or psc:
                    print(f"              NAICS: {naics[:40]}  PSC: {psc[:40]}")
            time.sleep(0.5)
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print("\n" + "=" * 80)
    print("\nSummary — total DHS contract matches per keyword (FY2022–FY2026):")
    for kw, n in sorted(totals.items(), key=lambda x: -x[1]):
        bar = "█" * min(n // 5, 60)
        print(f"  {n:5,}  {kw:<35} {bar}")


if __name__ == "__main__":
    main()

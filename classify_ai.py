"""
classify_ai.py — LLM classification pass over keyword-matched DHS contracts.

Pulls candidate contracts from the USASpending API using the keyword list,
deduplicates by Award ID, then asks gpt-4o-mini whether each contract is
actually AI/ML work. Results are checkpointed so the script can resume.

Run:
    python3 classify_ai.py                    # pull candidates + classify
    python3 classify_ai.py --from-csv data/dhs_contracts.csv   # use bulk data
    python3 classify_ai.py --dry-run          # show candidates, no LLM calls
"""

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Optional

import litellm
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

CHECKPOINT  = Path("data/classify_checkpoint.json")
OUTPUT_CSV  = Path("data/dhs_ai_classified.csv")
MODEL       = "gpt-5.4-mini"

USASPENDING_SEARCH = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
DHS_AGENCY_NAME    = "Department of Homeland Security"

KEYWORDS = [
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "AI/ML", "generative AI", "gen AI", "GenAI",
    "large language model", "language model", "foundation model",
    "GPT", "chatbot", "conversational AI",
    "natural language processing", "NLP", "computer vision",
    "object detection", "image recognition", "facial recognition",
    "anomaly detection", "pattern recognition", "sentiment analysis",
    "text analytics", "optical character recognition",
    "data science", "predictive analytics", "predictive model",
    "data labeling", "training data", "synthetic data",
    "algorithm", "knowledge graph",
    "intelligent automation", "robotic process automation",
    "decision support", "cognitive computing", "autonomous systems",
    "biometric", "screening at speed",
]

FIELDS = [
    "Award ID", "Recipient Name", "Awarding Sub Agency",
    "Award Amount", "Description", "Start Date",
    "NAICS Code", "NAICS Description", "PSC Code", "PSC Description",
]


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class AIClassification(BaseModel):
    is_ai: bool = Field(
        description="True if this contract is primarily for AI or machine learning work"
    )
    explanation: str = Field(
        description="One sentence explaining why this is or is not AI/ML work"
    )


# ---------------------------------------------------------------------------
# Candidate fetch
# ---------------------------------------------------------------------------

def fetch_candidates_from_api() -> list[dict]:
    import requests

    seen: set[str] = set()
    candidates: list[dict] = []

    for kw in KEYWORDS:
        payload = {
            "filters": {
                "keywords": [kw],
                "agencies": [{"type": "awarding", "tier": "toptier", "name": DHS_AGENCY_NAME}],
                "award_type_codes": ["A", "B", "C", "D"],
                "time_period": [{"start_date": "2021-10-01", "end_date": "2026-09-30"}],
            },
            "fields": FIELDS + ["generated_internal_id"],
            "limit": 100,
            "page": 1,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }
        try:
            r = requests.post(USASPENDING_SEARCH, json=payload, timeout=30)
            r.raise_for_status()
            for row in r.json().get("results", []):
                aid = row.get("Award ID", "")
                if aid and aid not in seen:
                    seen.add(aid)
                    row["matched_keyword"] = kw
                    row["generated_internal_id"] = row.get("generated_internal_id", "")
                    candidates.append(row)
        except Exception as exc:
            print(f"  warning: keyword '{kw}' failed — {exc}")
        time.sleep(0.3)

    return candidates


def fetch_candidates_from_csv(csv_path: Path) -> list[dict]:
    import re

    pattern = re.compile(
        "|".join(re.escape(kw) for kw in KEYWORDS), re.IGNORECASE
    )
    seen: set[str] = set()
    candidates: list[dict] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            desc = " ".join([
                row.get("transaction_description", ""),
                row.get("prime_award_base_transaction_description", ""),
                row.get("award_description", ""),
            ])
            if not pattern.search(desc):
                continue
            aid = row.get("contract_award_unique_key", "") or row.get("award_id_piid", "")
            if aid in seen:
                continue
            seen.add(aid)
            candidates.append({
                "Award ID":           aid,
                "Recipient Name":     row.get("recipient_name", ""),
                "Awarding Sub Agency": row.get("awarding_sub_agency_name", ""),
                "Award Amount":       row.get("total_dollars_obligated", ""),
                "Description":        (row.get("transaction_description") or
                                       row.get("prime_award_base_transaction_description", "")),
                "Start Date":         row.get("period_of_performance_start_date", ""),
                "NAICS Code":         row.get("naics_code", ""),
                "NAICS Description":  row.get("naics_description", ""),
                "PSC Code":           row.get("product_or_service_code", ""),
                "PSC Description":    row.get("product_or_service_code_description", ""),
                "matched_keyword":    "bulk_keyword_scan",
            })

    return candidates


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert on federal government contracting. You are classifying whether
a DHS contract is primarily for artificial intelligence or machine learning work.

AI/ML work includes: training or deploying ML models, LLMs, computer vision,
NLP, predictive modeling, AI-powered decision systems, and AI research/SBIR.

AI/ML work does NOT include: general IT operations, biometric identity systems
that are primarily database/enrollment infrastructure (not ML-based), robotic
process automation that is purely rule-based scripting, or generic "data
analytics" without a modeling component.

When in doubt, lean toward True — we will review borderline cases manually.\
"""


def classify_contract(contract: dict) -> Optional[AIClassification]:
    desc = (contract.get("Description") or "").strip()
    if not desc:
        return AIClassification(
            is_ai=False,
            explanation="No description available to classify."
        )

    prompt = f"""\
Classify this DHS contract:

Vendor: {contract.get("Recipient Name", "unknown")}
Sub-agency: {contract.get("Awarding Sub Agency", "unknown")}
Amount: ${contract.get("Award Amount", 0):,}
NAICS: {contract.get("NAICS Code", "")} — {contract.get("NAICS Description", "")}
PSC: {contract.get("PSC Code", "")} — {contract.get("PSC Description", "")}
Description: {desc[:800]}

Is this primarily AI/ML work?"""

    for attempt in range(3):
        try:
            response = litellm.completion(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format=AIClassification,
                temperature=0.0,
            )
            raw = response.choices[0].message.content
            data = json.loads(raw)
            return AIClassification(**data)
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"  classification failed after 3 attempts: {exc}")
                return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-csv", metavar="PATH",
                    help="Classify from bulk CSV instead of USASpending API")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show candidates without making LLM calls")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of LLM calls (0 = no limit)")
    args = ap.parse_args()

    if not args.dry_run and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set")

    # Load checkpoint
    checkpoint: dict[str, dict] = {}
    if CHECKPOINT.exists():
        checkpoint = json.loads(CHECKPOINT.read_text())
    print(f"Checkpoint: {len(checkpoint)} already classified")

    # Fetch candidates
    print("Fetching candidates...")
    if args.from_csv:
        candidates = fetch_candidates_from_csv(Path(args.from_csv))
    else:
        candidates = fetch_candidates_from_api()

    print(f"Found {len(candidates)} unique candidate contracts")

    if args.dry_run:
        for c in candidates:
            print(f"  [{c.get('Awarding Sub Agency',''):30}] "
                  f"${float(c.get('Award Amount') or 0):>12,.0f}  "
                  f"{(c.get('Description') or '')[:80]}")
        return

    # Classify
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    classified = 0
    out_rows = []

    for contract in candidates:
        aid = contract.get("Award ID", "")

        if aid in checkpoint:
            out_rows.append({**contract, **checkpoint[aid]})
            continue

        if args.limit and classified >= args.limit:
            break

        result = classify_contract(contract)
        classified += 1

        row_result = {
            "is_ai":       result.is_ai if result else None,
            "explanation": result.explanation if result else "classification failed",
        }
        checkpoint[aid] = row_result
        out_rows.append({**contract, **row_result})

        label = "YES" if (result and result.is_ai) else "no "
        print(f"  [{label}] {(contract.get('Description') or '')[:80]}")

        # Save checkpoint every 10 calls
        if classified % 10 == 0:
            CHECKPOINT.write_text(json.dumps(checkpoint, indent=2))

    CHECKPOINT.write_text(json.dumps(checkpoint, indent=2))

    # Write output CSV
    if out_rows:
        fieldnames = list(out_rows[0].keys())
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(out_rows)

    ai_count = sum(1 for r in out_rows if r.get("is_ai"))
    print(f"\nDone: {len(out_rows)} contracts, {ai_count} classified as AI, "
          f"{len(out_rows) - ai_count} not AI")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()

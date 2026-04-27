"""
build_web.py — Generate web/data/results.json from classified contract data.

Merges three sources:
  1. classify_ai.py output  — keyword-matched + LLM classified
  2. enriched.json          — modification text reclassifications, IDV siblings
  3. (future)               — rfp_text results

Run after classify_ai.py (and optionally enrich_contracts.py) completes:
    python3 build_web.py
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INPUT_CSV     = Path("data/dhs_ai_classified.csv")
ENRICHED_JSON = Path("data/enriched.json")
OUTPUT_DIR    = Path("web/data")
OUTPUT_JSON   = OUTPUT_DIR / "results.json"

KEYWORDS_SEARCHED = [
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


def fmt_dollars(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def make_permalink(gid, aid):
    return f"https://www.usaspending.gov/award/{gid or aid}"


def main() -> None:
    if not INPUT_CSV.exists():
        raise SystemExit(f"{INPUT_CSV} not found — run classify_ai.py first")

    # ── Source 1: keyword-classified contracts ───────────────────────────────
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    ai_rows = [r for r in rows if str(r.get("is_ai", "")).lower() == "true"]

    contracts: dict[str, dict] = {}
    for r in ai_rows:
        aid = r.get("Award ID", "")
        contracts[aid] = {
            "award_id":              aid,
            "vendor":                r.get("Recipient Name", ""),
            "agency":                r.get("Awarding Sub Agency", ""),
            "amount":                fmt_dollars(r.get("Award Amount")),
            "description":           (r.get("Description") or "")[:300],
            "naics":                 r.get("NAICS Description", ""),
            "psc":                   r.get("PSC Description", ""),
            "start_date":            r.get("Start Date", ""),
            "keyword":               r.get("matched_keyword", ""),
            "explanation":           r.get("explanation", ""),
            "generated_internal_id": r.get("generated_internal_id", ""),
            "permalink":             make_permalink(r.get("generated_internal_id"), aid),
            "source":                "keyword_search",
        }

    # ── Source 2: enrichment passes ─────────────────────────────────────────
    enriched_counts = {"mod_reclassified": 0, "idv_siblings": 0}
    if ENRICHED_JSON.exists():
        enriched = json.loads(ENRICHED_JSON.read_text())

        for r in enriched.get("mod_reclassified", []):
            aid = r.get("award_id", "")
            if not aid or aid in contracts:
                continue
            gid = r.get("generated_internal_id", "")
            contracts[aid] = {
                "award_id":              aid,
                "vendor":                r.get("vendor", ""),
                "agency":                r.get("agency", ""),
                "amount":                fmt_dollars(r.get("amount")),
                "description":           (r.get("description") or "")[:300],
                "naics":                 "",
                "psc":                   "",
                "start_date":            "",
                "keyword":               "modification_text",
                "explanation":           r.get("explanation", ""),
                "generated_internal_id": gid,
                "permalink":             make_permalink(gid, aid),
                "source":                "modification_text",
            }
            enriched_counts["mod_reclassified"] += 1

        for r in enriched.get("idv_siblings", []):
            aid = r.get("award_id", "")
            if not aid or aid in contracts:
                continue
            gid = r.get("generated_internal_id", "")
            contracts[aid] = {
                "award_id":              aid,
                "vendor":                "",
                "agency":                r.get("agency", ""),
                "amount":                fmt_dollars(r.get("amount")),
                "description":           (r.get("description") or "")[:300],
                "naics":                 "",
                "psc":                   "",
                "start_date":            "",
                "keyword":               f"idv_sibling:{r.get('parent_idv','')}",
                "explanation":           r.get("explanation", ""),
                "generated_internal_id": gid,
                "permalink":             make_permalink(gid, aid),
                "source":                "idv_expansion",
            }
            enriched_counts["idv_siblings"] += 1

    # ── Aggregate ────────────────────────────────────────────────────────────
    contract_list = sorted(contracts.values(), key=lambda x: -x["amount"])

    by_agency: dict[str, dict] = defaultdict(lambda: {"contracts": 0, "dollars": 0.0})
    for c in contract_list:
        ag = c.get("agency") or "Unknown"
        by_agency[ag]["contracts"] += 1
        by_agency[ag]["dollars"]   += c["amount"]

    by_agency_list = sorted(
        [{"agency": k, **v} for k, v in by_agency.items()],
        key=lambda x: -x["dollars"],
    )

    total_dollars    = sum(c["amount"] for c in contract_list)
    total_contracts  = len(contract_list)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "summary": {
            "total_ai_contracts":        total_contracts,
            "total_ai_dollars":          total_dollars,
            "total_candidates":          len(rows),
            "not_ai_filtered_out":       len(rows) - len(ai_rows),
            "sub_agencies_with_ai":      len(by_agency),
            "from_keyword_search":       len(ai_rows),
            "from_modification_text":    enriched_counts["mod_reclassified"],
            "from_idv_expansion":        enriched_counts["idv_siblings"],
        },
        "by_agency":        by_agency_list,
        "contracts":        contract_list,
        "keywords_searched": KEYWORDS_SEARCHED,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUTPUT_JSON}")
    print(f"  {len(ai_rows)} keyword-classified + "
          f"{enriched_counts['mod_reclassified']} mod-reclassified + "
          f"{enriched_counts['idv_siblings']} IDV siblings "
          f"= {total_contracts} total AI contracts")
    print(f"  ${total_dollars:,.0f} total obligated")
    print(f"  {len(by_agency)} sub-agencies")


if __name__ == "__main__":
    main()

"""
build_web.py — Generate web/data/results.json from classified contract data.

Run after classify_ai.py completes:
    python3 build_web.py
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

INPUT_CSV  = Path("data/dhs_ai_classified.csv")
OUTPUT_DIR = Path("web/data")
OUTPUT_JSON = OUTPUT_DIR / "results.json"


def fmt_dollars(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    if not INPUT_CSV.exists():
        raise SystemExit(f"{INPUT_CSV} not found — run classify_ai.py first")

    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    ai_rows = [r for r in rows if str(r.get("is_ai", "")).lower() == "true"]
    not_ai  = total - len(ai_rows)

    # By sub-agency
    by_agency: dict[str, dict] = defaultdict(lambda: {"contracts": 0, "dollars": 0.0})
    for r in ai_rows:
        ag = r.get("Awarding Sub Agency") or "Unknown"
        by_agency[ag]["contracts"] += 1
        by_agency[ag]["dollars"]   += fmt_dollars(r.get("Award Amount"))

    by_agency_list = sorted(
        [{"agency": k, **v} for k, v in by_agency.items()],
        key=lambda x: -x["dollars"],
    )

    # Summary stats
    total_dollars = sum(r["dollars"] for r in by_agency_list)
    total_contracts = len(ai_rows)

    # Contract table (all AI-classified, sorted by dollars desc)
    contracts = sorted(
        [
            {
                "award_id":    r.get("Award ID", ""),
                "vendor":      r.get("Recipient Name", ""),
                "agency":      r.get("Awarding Sub Agency", ""),
                "amount":      fmt_dollars(r.get("Award Amount")),
                "description": (r.get("Description") or "")[:300],
                "naics":       r.get("NAICS Description", ""),
                "psc":         r.get("PSC Description", ""),
                "start_date":  r.get("Start Date", ""),
                "keyword":     r.get("matched_keyword", ""),
                "explanation": r.get("explanation", ""),
                "permalink":   f"https://www.usaspending.gov/award/{r.get('Award ID', '')}",
            }
            for r in ai_rows
        ],
        key=lambda x: -x["amount"],
    )

    output = {
        "generated_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ"),
        "summary": {
            "total_ai_contracts":     total_contracts,
            "total_ai_dollars":       total_dollars,
            "total_candidates":       total,
            "not_ai_filtered_out":    not_ai,
            "sub_agencies_with_ai":   len(by_agency),
        },
        "by_agency": by_agency_list,
        "contracts": contracts,
        "keywords_searched": [
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
        ],
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUTPUT_JSON}")
    print(f"  {total_contracts} AI contracts, ${total_dollars:,.0f} total")
    print(f"  {not_ai} candidates filtered out by LLM")
    print(f"  {len(by_agency)} sub-agencies represented")


if __name__ == "__main__":
    main()

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
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INPUT_CSV       = Path("data/dhs_ai_classified.csv")
ENRICHED_JSON   = Path("data/enriched.json")
BULK_CSV        = Path("data/dhs_contracts.csv")
FETCH_METADATA  = Path("data/fetch_metadata.json")
OUTPUT_DIR      = Path("web/data")
OUTPUT_JSON     = OUTPUT_DIR / "results.json"

# Per-shipped-field provenance map. Every key that appears on a contract
# row in results.json must have an entry here pointing at its upstream
# USASpending column (the literal CSV header in the bulk archive). This
# is what raw.html's "where did this value come from" rendering reads,
# and what tests/fields_glossary_test.py asserts is complete.
#
# The "source" key is one of:
#   - "usaspending_csv"  : a column from the USASpending bulk Contracts CSV
#   - "derived"          : computed/joined from upstream data by this pipeline
#   - "llm"              : produced by the LLM classification pass
#   - "internal"         : housekeeping (URLs we construct, source labels)
FIELD_PROVENANCE: dict[str, dict] = {
    "award_id":              {"source": "usaspending_csv", "column": "award_id_piid",
                              "note": "The PIID — the contract's unique identifier within the awarding agency."},
    "vendor":                {"source": "usaspending_csv", "column": "recipient_name",
                              "note": "Legal name of the contractor at time of action."},
    "agency":                {"source": "usaspending_csv", "column": "awarding_sub_agency_name",
                              "note": "DHS sub-agency that awarded the contract (CBP, ICE, TSA, USCIS, FEMA, ...)."},
    "amount":                {"source": "usaspending_csv", "column": "total_dollars_obligated",
                              "note": "Total dollars obligated to date across all actions on this award."},
    "description":           {"source": "usaspending_csv",
                              "column": "transaction_description / award_description / prime_award_base_transaction_description",
                              "note": "Joined from up to three description fields; first non-empty wins."},
    "naics":                 {"source": "usaspending_csv", "column": "naics_description",
                              "note": "NAICS industry classification (e.g. \"Computer Systems Design Services\")."},
    "psc":                   {"source": "usaspending_csv", "column": "product_or_service_code_description",
                              "note": "Product/Service Code description (the federal classification of what was bought)."},
    "start_date":            {"source": "usaspending_csv", "column": "period_of_performance_start_date",
                              "note": "When work was scheduled to begin."},
    "keyword":               {"source": "derived",
                              "column": "(see classification path)",
                              "note": "Literal AI keyword(s) that matched the description, OR a sentinel like 'modification_text' / 'idv_sibling:<parent>' for non-keyword paths."},
    "explanation":           {"source": "llm",
                              "column": "gpt-5.4-mini classification output",
                              "note": "One-sentence justification from the LLM classifier."},
    "generated_internal_id": {"source": "usaspending_csv", "column": "contract_award_unique_key",
                              "note": "USASpending's internal unique key (CONT_AWD_... / CONT_IDV_...). Used to build the per-action permalink."},
    "permalink":             {"source": "internal",
                              "column": f"https://www.usaspending.gov/award/{{generated_internal_id}}",
                              "note": "Direct link to the contract's page on USASpending.gov."},
    "raw_url":               {"source": "internal", "column": "raw.html?piid={award_id}",
                              "note": "Per-contract provenance viewer (this page)."},
    "source":                {"source": "internal",
                              "column": "(classification path label)",
                              "note": "Which path landed this contract in the dataset: keyword_search, modification_text, or idv_expansion."},
    "mod_text":              {"source": "usaspending_csv",
                              "column": "transaction_description (joined across modifications)",
                              "note": "Concatenated modification descriptions — the trigger text the LLM saw when reclassifying."},
    "idv_parent":            {"source": "usaspending_csv", "column": "parent_award_id_piid",
                              "note": "Parent IDV vehicle whose other task orders are confirmed AI."},
    "idv_siblings":          {"source": "derived",
                              "column": "(joined from confirmed-AI rows under the same parent_award_id_piid)",
                              "note": "Other AI awards under the same parent IDV; the audit trail for this expansion."},
}

# Single source of truth for the LLM model name on the dashboard. If
# classify_ai.py / enrich_contracts.py ever change models, update this too —
# the methodology block on the website (and raw.html provenance) reads it.
LLM_MODEL = "gpt-5.4-mini"
DHS_AGENCY_NAME = "Department of Homeland Security"

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


# Pre-compiled keyword patterns for re-deriving the literal matched
# keyword from a description. Old checkpoints stored "bulk_keyword_scan"
# as a placeholder; build_web rewrites it to the actual matching keyword
# string(s) so raw.html can highlight them.
_KEYWORD_PATTERNS = [(kw, re.compile(re.escape(kw), re.IGNORECASE))
                     for kw in KEYWORDS_SEARCHED]


def derive_matched_keywords(description: str) -> str:
    """Return a `|`-joined list of every KEYWORDS_SEARCHED term that
    appears in the description (case-insensitive). Empty string if none —
    that means the row was classified via API search (the keyword came
    from the search itself, not the description text)."""
    if not description:
        return ""
    matched = [kw for kw, pat in _KEYWORD_PATTERNS if pat.search(description)]
    return "|".join(matched)


def load_bulk_index() -> dict[str, dict]:
    """One-time pass over the bulk USASpending CSV to build a per-PIID
    lookup used for two backfills at build time:

    1. **Matched keyword + full description** — old checkpoint rows
       stored only the truncated `transaction_description`, but some
       bulk-scan hits matched against `award_description` (which wasn't
       stored), so derive_matched_keywords() returns nothing on the
       short version. We re-derive against the wider concat here.

    2. **Vendor name** — `enriched.idv_siblings` doesn't carry the
       recipient_name, so IDV-expansion contracts ship with empty
       vendor unless we look it up here.

    Returns {aid: {keywords, full_desc, vendor}}. Empty dict when the
    bulk CSV isn't present (e.g. fresh checkout) — affected rows just
    keep their original sparse values."""
    if not BULK_CSV.exists():
        return {}
    out: dict[str, dict] = {}
    with open(BULK_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            piid = row.get("award_id_piid") or ""
            gid  = row.get("contract_award_unique_key") or ""
            if not piid and not gid:
                continue
            full_desc = " ".join([
                row.get("transaction_description") or "",
                row.get("prime_award_base_transaction_description") or "",
                row.get("award_description") or "",
            ]).strip()
            entry = {
                "keywords":  derive_matched_keywords(full_desc),
                "full_desc": full_desc,
                "vendor":    row.get("recipient_name") or "",
            }
            # Index under BOTH the PIID (for keyword_search rows whose
            # award_id is the PIID) and the gid (for idv_expansion rows
            # whose award_id is just the modnum stub like "7013" — the
            # only way to map them back to the bulk CSV is via the
            # contract_award_unique_key the pipeline propagated).
            # Keep first-seen for each key; vendor name is stable across
            # the lifetime of an award.
            for k in (piid, gid):
                if k and k not in out:
                    out[k] = entry
    return out


def make_raw_link(aid):
    """Per-contract raw-data URL (provenance viewer). aid must be the same
    Award ID we shipped in the contract record so raw.html can look it up."""
    from urllib.parse import quote
    return f"raw.html?piid={quote(str(aid))}"


def main() -> None:
    if not INPUT_CSV.exists():
        raise SystemExit(f"{INPUT_CSV} not found — run classify_ai.py first")

    # ── Source 1: keyword-classified contracts ───────────────────────────────
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    ai_rows = [r for r in rows if str(r.get("is_ai", "")).lower() == "true"]

    bulk_index = load_bulk_index()

    contracts: dict[str, dict] = {}
    for r in ai_rows:
        aid = r.get("Award ID", "")
        gid = r.get("generated_internal_id", "")
        description = (r.get("Description") or "")[:300]
        # Old checkpoint rows wrote "bulk_keyword_scan" as a placeholder;
        # newer fetch_candidates_from_csv stores the literal matched
        # keyword. For legacy rows: try the truncated description first,
        # then fall back to the full bulk-CSV description (which is what
        # the original scanner actually matched against — `award_description`
        # often holds the keyword that's missing from `transaction_description`).
        stored_kw = (r.get("matched_keyword") or "").strip()
        if not stored_kw or stored_kw == "bulk_keyword_scan":
            keyword = derive_matched_keywords(description)
            if not keyword and aid in bulk_index:
                bk = bulk_index[aid]
                keyword = bk["keywords"]
                # Prefer the wider description so the highlight has the
                # keyword to anchor on. Cap at the same length the rest
                # of the dataset uses for consistency.
                if not description or not derive_matched_keywords(description):
                    description = bk["full_desc"][:300]
            if not keyword:
                keyword = stored_kw  # last-ditch: keep the placeholder
        else:
            keyword = stored_kw
        contracts[aid] = {
            "award_id":              aid,
            "vendor":                r.get("Recipient Name", ""),
            "agency":                r.get("Awarding Sub Agency", ""),
            "amount":                fmt_dollars(r.get("Award Amount")),
            "description":           description,
            "naics":                 r.get("NAICS Description", ""),
            "psc":                   r.get("PSC Description", ""),
            "start_date":            r.get("Start Date", ""),
            "keyword":               keyword,
            "explanation":           r.get("explanation", ""),
            "generated_internal_id": gid,
            "permalink":             make_permalink(gid, aid),
            "raw_url":               make_raw_link(aid),
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
                "raw_url":               make_raw_link(aid),
                "source":                "modification_text",
                # Trigger text: the modification text that flipped this
                # contract from "not AI" to "AI." raw.html highlights it.
                "mod_text":              (r.get("mod_text") or "")[:1000],
            }
            enriched_counts["mod_reclassified"] += 1

        for r in enriched.get("idv_siblings", []):
            aid = r.get("award_id", "")
            if not aid or aid in contracts:
                continue
            gid = r.get("generated_internal_id", "")
            parent = r.get("parent_idv", "")
            # enriched.idv_siblings doesn't carry recipient_name; look it
            # up from the bulk CSV index so the dashboard isn't littered
            # with "(no vendor)" rows. award_id is the modnum stub for
            # IDV expansions; the gid (contract_award_unique_key) is
            # what indexes back into the bulk CSV reliably.
            vendor = ((bulk_index.get(gid) or bulk_index.get(aid))
                      or {}).get("vendor", "")
            contracts[aid] = {
                "award_id":              aid,
                "vendor":                vendor,
                "agency":                r.get("agency", ""),
                "amount":                fmt_dollars(r.get("amount")),
                "description":           (r.get("description") or "")[:300],
                "naics":                 "",
                "psc":                   "",
                "start_date":            "",
                "keyword":               f"idv_sibling:{parent}",
                "explanation":           r.get("explanation", ""),
                "generated_internal_id": gid,
                "permalink":             make_permalink(gid, aid),
                "raw_url":               make_raw_link(aid),
                "source":                "idv_expansion",
                "idv_parent":            parent,
                # idv_siblings is filled in below, after all sources merge.
                "idv_siblings":          [],
            }
            enriched_counts["idv_siblings"] += 1

    # Now that every source has been merged into `contracts`, populate
    # idv_siblings for each idv_expansion row from the unified dataset.
    # "Sibling" = any OTHER contract whose generated_internal_id contains
    # this contract's parent_idv. This includes both keyword_search hits
    # under the same IDV (the seeds) and other idv_expansion rows.
    for c in contracts.values():
        if c.get("source") != "idv_expansion":
            continue
        parent = c.get("idv_parent") or ""
        if not parent:
            continue
        siblings = []
        for other in contracts.values():
            if other is c:
                continue
            if parent and parent in (other.get("generated_internal_id") or ""):
                siblings.append({
                    "award_id":              other.get("award_id", ""),
                    "amount":                other.get("amount", 0.0),
                    "description":           (other.get("description") or "")[:200],
                    "generated_internal_id": other.get("generated_internal_id", ""),
                    "source":                other.get("source", ""),
                    "explanation":           other.get("explanation", ""),
                })
        c["idv_siblings"] = siblings

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
        # Methodology metadata that raw.html and the dashboard read so
        # provenance ("classified by what model, on what date, against what
        # keyword list") stays in sync with the actual pipeline.
        "methodology": {
            "llm_model":                 LLM_MODEL,
            "data_source":               "USASpending.gov API + Award Data Archive",
            "data_source_url":           "https://www.usaspending.gov/",
            "data_dictionary_url":       "https://www.usaspending.gov/data-dictionary",
            "bulk_archive_url":          "https://files.usaspending.gov/award_data_archive/",
            "agency_filter":             DHS_AGENCY_NAME,
            "fy_range":                  "FY2022–FY2026",
            "keyword_count":             len(KEYWORDS_SEARCHED),
            # Literal archive ZIPs the pipeline last ingested. Populated by
            # fetch_dhs.py at fetch time; falls back to None if the local
            # checkout doesn't have a fetch_metadata.json yet (e.g. fresh
            # clone before first run).
            "fetch_metadata":            (json.loads(FETCH_METADATA.read_text())
                                          if FETCH_METADATA.exists() else None),
        },
        # Field-level provenance map. Every shipped field on a contract
        # row points at its upstream USASpending column (or "derived" /
        # "llm" / "internal"). raw.html renders this beside each value.
        "field_provenance": FIELD_PROVENANCE,
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

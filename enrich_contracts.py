"""
enrich_contracts.py — Three enrichment passes beyond keyword search.

All USASpending calls are free (no key, no quota).

  --idv          Expand parent IDIQs: pull all sibling task orders of our AI
                 contracts and LLM-classify the ones we haven't seen yet.

  --modifications Re-classify the keyword candidates we called "no" using their
                 full modification history text — AI scope sometimes appears in
                 later mods even when the base award description is generic.

  --subawards    For each AI contract, fetch subaward records and flag any that
                 go to known AI vendors or have AI-adjacent descriptions.

Run:
    python3 enrich_contracts.py --idv
    python3 enrich_contracts.py --modifications
    python3 enrich_contracts.py --subawards
    python3 enrich_contracts.py --idv --modifications --subawards
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

RESULTS_JSON   = Path("web/data/results.json")
CLASSIFIED_CSV = Path("data/dhs_ai_classified.csv")
ENRICHED_JSON  = Path("data/enriched.json")  # accumulates all enrichment findings

AWARD_DETAIL   = "https://api.usaspending.gov/api/v2/awards/{}/"
IDV_AWARDS     = "https://api.usaspending.gov/api/v2/idvs/awards/"
TRANSACTIONS   = "https://api.usaspending.gov/api/v2/transactions/"
SUBAWARDS      = "https://api.usaspending.gov/api/v2/subawards/"

MODEL = "gpt-5.4-mini"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_results() -> dict:
    return json.loads(RESULTS_JSON.read_text())

def load_enriched() -> dict:
    if ENRICHED_JSON.exists():
        return json.loads(ENRICHED_JSON.read_text())
    return {"idv_siblings": [], "mod_reclassified": [], "subaward_signals": []}

def save_enriched(data: dict) -> None:
    ENRICHED_JSON.parent.mkdir(parents=True, exist_ok=True)
    ENRICHED_JSON.write_text(json.dumps(data, indent=2))

def load_classified_csv() -> list[dict]:
    import csv
    rows = []
    with open(CLASSIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows

def llm_classify(description: str, context: str = "") -> dict | None:
    import litellm

    system = """\
You are an expert on federal government contracting. Classify whether this DHS
contract involves AI or machine learning work.

AI/ML work INCLUDES: training or deploying ML models, LLMs, computer vision,
NLP, predictive modeling, AI-powered decision systems, AI research/SBIR.

AI/ML work DOES NOT INCLUDE:
- Robotic Process Automation (RPA) — rule-based scripting, NOT AI. UiPath,
  Automation Anywhere, Blue Prism licenses are NOT AI contracts.
- Generic biometric hardware that is purely enrollment/storage infrastructure
  (e.g., fingerprint card readers, badge scanners, livescan stations) with
  no matching or recognition component.
- General IT operations, data storage, or infrastructure.

IMPORTANT INCLUSIONS — these ARE AI:
- Facial recognition systems (computer vision / ML-based matching)
- Biometric identity matching systems (iris, face, fingerprint matching
  at scale uses ML models)
- Any system that performs automated identity verification or recognition

When in doubt, lean False — we want confirmed AI, not adjacent technology."""

    prompt = f"Description:\n{description[:1200]}"
    if context:
        prompt += f"\n\nAdditional context (modification history):\n{context[:800]}"
    prompt += "\n\nIs this AI/ML work? Respond with is_ai (bool) and explanation (str)."

    from pydantic import BaseModel, Field

    class Result(BaseModel):
        is_ai: bool
        explanation: str = Field(description="One sentence")

    for attempt in range(3):
        try:
            resp = litellm.completion(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                response_format=Result,
                temperature=0.0,
            )
            data = json.loads(resp.choices[0].message.content)
            return Result(**data).model_dump()
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def get_award_detail(session: requests.Session, gid: str) -> dict:
    try:
        r = session.get(AWARD_DETAIL.format(gid), timeout=15)
        return r.json() if r.status_code == 200 else {}
    except requests.exceptions.Timeout:
        return {}
    except Exception:
        return {}


def get_transactions(session: requests.Session, gid: str) -> list[dict]:
    try:
        r = session.post(TRANSACTIONS, json={
            "award_id": gid,
            "fields": ["action_date", "modification_number",
                       "action_type_description", "description",
                       "federal_action_obligation"],
            "sort": "action_date", "order": "asc", "page": 1, "limit": 100,
        }, timeout=45)
        return r.json().get("results", []) if r.status_code == 200 else []
    except requests.exceptions.Timeout:
        print(f"  timeout fetching transactions for {gid[:40]} — skipping")
        return []
    except Exception as e:
        print(f"  error fetching transactions: {e} — skipping")
        return []


# ---------------------------------------------------------------------------
# Phase 2: Parent IDV expansion
# ---------------------------------------------------------------------------

def run_idv(session: requests.Session) -> None:
    print("\n=== IDV expansion ===")
    d = load_results()
    enriched = load_enriched()

    known_award_ids = {c["award_id"] for c in d["contracts"]}
    already_found   = {s["award_id"] for s in enriched["idv_siblings"]}

    # Collect unique parent IDV IDs
    parent_idvs: dict[str, str] = {}  # generated_id → piid
    for i, c in enumerate(d["contracts"]):
        gid = c.get("generated_internal_id")
        if not gid:
            continue
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(d['contracts'])} award details fetched, {len(parent_idvs)} IDVs so far...")
        detail = get_award_detail(session, gid)
        parent = detail.get("parent_award") or {}
        pid = parent.get("generated_unique_award_id")
        if pid and pid not in parent_idvs:
            parent_idvs[pid] = parent.get("piid", pid)
        time.sleep(0.1)

    print(f"Found {len(parent_idvs)} unique parent IDIQs from {len(d['contracts'])} AI contracts")

    new_candidates = []
    for idv_gid, idv_piid in parent_idvs.items():
        page = 1
        while True:
            r = session.post(IDV_AWARDS, json={
                "award_id": idv_gid, "type": "child_awards",
                "limit": 100, "page": page,
            }, timeout=20)
            if r.status_code != 200:
                break
            results = r.json().get("results", [])
            for child in results:
                # Skip task orders with no DHS connection — GSA awards on
                # behalf of other agencies so check both awarding + funding
                awarding = (child.get("awarding_agency") or "").lower()
                funding  = (child.get("funding_agency")  or "").lower()
                if "homeland security" not in awarding and "homeland security" not in funding:
                    continue
                child_gid = child.get("generated_unique_award_id", "")
                child_aid = child_gid.split("_")[3] if child_gid else ""
                if child_aid in known_award_ids or child_gid in already_found:
                    continue
                new_candidates.append({
                    "award_id":              child_aid,
                    "generated_internal_id": child_gid,
                    "description":           child.get("description", ""),
                    "amount":                child.get("obligated_amount", 0),
                    "agency":                child.get("awarding_agency", ""),
                    "parent_idv":            idv_piid,
                })
            if not r.json().get("page_metadata", {}).get("hasNext"):
                break
            page += 1
            time.sleep(0.3)

    print(f"{len(new_candidates)} unseen sibling task orders to classify")

    classified = 0
    for c in new_candidates:
        desc = c["description"]
        if not desc:
            continue
        result = llm_classify(desc)
        classified += 1
        label = "YES" if (result and result["is_ai"]) else "no "
        print(f"  [{label}] [{c['parent_idv']}] {desc[:80]}")
        if result and result["is_ai"]:
            enriched["idv_siblings"].append({**c, **result})
        time.sleep(0.3)

    save_enriched(enriched)
    ai_found = sum(1 for s in enriched["idv_siblings"])
    print(f"\nIDV expansion: {classified} classified, {ai_found} total AI siblings saved")


# ---------------------------------------------------------------------------
# Phase 5: Modification text re-classification
# ---------------------------------------------------------------------------

MOD_PROCESSED_JSON = Path("data/mod_processed.json")

def run_modifications(session: requests.Session) -> None:
    print("\n=== Modification re-classification ===")
    rows = load_classified_csv()
    enriched = load_enriched()

    already_flipped  = {r["award_id"] for r in enriched["mod_reclassified"]}
    already_checked  = set(json.loads(MOD_PROCESSED_JSON.read_text()) if MOD_PROCESSED_JSON.exists() else [])
    no_rows = [
        r for r in rows
        if str(r.get("is_ai", "")).lower() == "false"
        and r.get("generated_internal_id")
        and r.get("Award ID") not in already_flipped
        and r.get("Award ID") not in already_checked
    ]

    print(f"{len(no_rows)} 'no' contracts to re-check ({len(already_checked)} already done, {len(already_flipped)} already flipped)")

    for row in no_rows:
        gid = row["generated_internal_id"]
        aid = row.get("Award ID", "")
        txs = get_transactions(session, gid)
        if not txs:
            continue

        # Concatenate all mod descriptions (skip blanks and SOW-admin-only mods)
        mod_descs = [t["description"] for t in txs if t.get("description")]
        combined_mod_text = " | ".join(mod_descs)

        base_desc = row.get("Description", "")
        result = llm_classify(base_desc, context=combined_mod_text)
        if not result:
            continue

        if result["is_ai"]:
            print(f"  [FLIP] {aid}: {result['explanation'][:80]}")
            enriched["mod_reclassified"].append({
                "award_id":    aid,
                "vendor":      row.get("Recipient Name", ""),
                "agency":      row.get("Awarding Sub Agency", ""),
                "amount":      float(row.get("Award Amount") or 0),
                "description": base_desc,
                "mod_text":    combined_mod_text[:500],
                **result,
            })
            save_enriched(enriched)

        already_checked.add(aid)
        MOD_PROCESSED_JSON.write_text(json.dumps(sorted(already_checked)))
        time.sleep(0.3)

    save_enriched(enriched)
    flipped = len(enriched["mod_reclassified"])
    print(f"\nMod re-classification: {flipped} total contracts flipped to AI")


# ---------------------------------------------------------------------------
# Phase 6: Subawards
# ---------------------------------------------------------------------------

def run_subawards(session: requests.Session) -> None:
    print("\n=== Subaward analysis ===")
    d = load_results()
    enriched = load_enriched()

    # Build known AI vendor set from our classified contracts
    ai_vendors = {c["vendor"].lower() for c in d["contracts"]}

    already = {s["award_id"] for s in enriched["subaward_signals"]}
    signals = []

    for c in d["contracts"]:
        gid = c.get("generated_internal_id")
        if not gid or c["award_id"] in already:
            continue

        page = 1
        contract_signals = []
        while True:
            r = session.post(SUBAWARDS, json={
                "award_id": gid, "limit": 50, "page": page,
            }, timeout=20)
            if r.status_code != 200:
                break
            results = r.json().get("results", [])
            for sub in results:
                desc    = (sub.get("description") or "").lower()
                vendor  = (sub.get("recipient_name") or "").lower()
                amount  = sub.get("amount", 0) or 0
                ai_keywords = ["artificial intelligence", "machine learning",
                               "deep learning", "neural", "nlp", "llm",
                               "computer vision", "predictive", "algorithm",
                               "data science", "ai/ml"]
                keyword_hit = next((kw for kw in ai_keywords if kw in desc), None)
                vendor_hit  = vendor in ai_vendors and vendor != c["vendor"].lower()
                if keyword_hit or vendor_hit:
                    contract_signals.append({
                        "subaward_number": sub.get("subaward_number"),
                        "recipient":       sub.get("recipient_name"),
                        "amount":          amount,
                        "description":     sub.get("description"),
                        "signal":          f"keyword:{keyword_hit}" if keyword_hit else f"known_ai_vendor",
                    })
            if not r.json().get("page_metadata", {}).get("hasNext"):
                break
            page += 1
            time.sleep(0.3)

        if contract_signals:
            print(f"  {c['award_id']} ({c['vendor'][:30]}): {len(contract_signals)} AI subaward signals")
            signals.append({
                "award_id": c["award_id"],
                "vendor":   c["vendor"],
                "signals":  contract_signals,
            })

    enriched["subaward_signals"] = signals
    save_enriched(enriched)
    print(f"\nSubaward analysis: {len(signals)} contracts with AI subaward signals")
    total_subs = sum(len(s["signals"]) for s in signals)
    print(f"  {total_subs} individual AI-signal subaward records")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idv",           action="store_true")
    ap.add_argument("--modifications", action="store_true")
    ap.add_argument("--subawards",     action="store_true")
    args = ap.parse_args()

    if not any([args.idv, args.modifications, args.subawards]):
        ap.print_help()
        return

    if (args.idv or args.modifications) and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set (needed for LLM classification)")

    session = requests.Session()

    if args.idv:
        run_idv(session)
    if args.modifications:
        run_modifications(session)
    if args.subawards:
        run_subawards(session)

    print(f"\nAll done. Results in {ENRICHED_JSON}")


if __name__ == "__main__":
    main()

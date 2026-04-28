"""
run_pipeline.py — Full DHS AI contract analysis pipeline.

Runs all steps in order, idempotent — safe to re-run any time.
Checkpoints at every step mean interruptions pick up where they left off.

Steps:
  1. fetch_dhs.py          — pull/update bulk DHS contracts (R2 or USASpending)
  2. classify_ai.py        — keyword search + LLM classify via USASpending API
  3. classify_ai.py --from-csv  — keyword search over full bulk data
  4. enrich_contracts.py --modifications  — re-classify 'no' contracts w/ mod text
  5. enrich_contracts.py --idv            — expand to sibling task orders
  6. build_web.py          — merge all sources → web/data/results.json

Run:
    python3 run_pipeline.py
    python3 run_pipeline.py --skip-fetch    # skip step 1 (data already fresh)
    python3 run_pipeline.py --skip-enrich   # skip steps 4-5 (LLM-heavy)
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

R2_PREFIX       = "dhs_contracts/pipeline_state/"
STATE_FILES     = [
    "data/classify_checkpoint.json",
    "data/mod_processed.json",
    "data/idv_processed.json",
    "data/enriched.json",
    "data/solicitation_ids.json",
]


def _r2_client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['CF_R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def download_state() -> None:
    if not os.environ.get("CF_R2_ACCOUNT_ID"):
        return
    print("  Downloading pipeline state from R2...")
    s3 = _r2_client()
    bucket = os.environ["CF_R2_BUCKET"]
    for local in STATE_FILES:
        key = R2_PREFIX + Path(local).name
        try:
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, local)
            print(f"    R2 → {local}")
        except Exception:
            pass  # file doesn't exist in R2 yet, that's fine


def upload_state() -> None:
    if not os.environ.get("CF_R2_ACCOUNT_ID"):
        return
    print("  Uploading pipeline state to R2...")
    s3 = _r2_client()
    bucket = os.environ["CF_R2_BUCKET"]
    for local in STATE_FILES:
        if Path(local).exists():
            key = R2_PREFIX + Path(local).name
            s3.upload_file(local, bucket, key)
            print(f"    {local} → R2")


def step(label: str, cmd: str, allow_fail: bool = False) -> bool:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    result = subprocess.run(cmd, shell=True)
    ok = result.returncode == 0
    if not ok:
        if allow_fail:
            print(f"  [warning] {label} failed (continuing)")
        else:
            print(f"  [FAIL] {label} — aborting pipeline")
            sys.exit(1)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch",  action="store_true", help="Skip bulk data download (step 1)")
    ap.add_argument("--skip-enrich", action="store_true", help="Skip enrichment passes (steps 4-5)")
    args = ap.parse_args()

    start = datetime.now(timezone.utc)
    print(f"\nDHS AI Contract Pipeline — {start.strftime('%Y-%m-%d %H:%M UTC')}")

    download_state()

    # Step 1: Update bulk DHS contracts
    if not args.skip_fetch:
        step("1/6  Fetch bulk DHS contracts", "python3 fetch_dhs.py")
    else:
        print("\n  1/6  Fetch — skipped")

    # Step 2: USASpending API keyword search + LLM classify
    step("2/6  Classify via USASpending API", "python3 classify_ai.py")

    # Step 3: Bulk data keyword search + LLM classify (catches API cap misses)
    if Path("data/dhs_contracts.csv").exists():
        step("3/6  Classify via bulk CSV", "python3 classify_ai.py --from-csv data/dhs_contracts.csv")
    else:
        print("\n  3/6  Classify bulk CSV — skipped (data/dhs_contracts.csv not found)")

    # Steps 4-5: Enrichment
    if not args.skip_enrich:
        step("4/6  Modification text re-classification",
             "python3 enrich_contracts.py --modifications", allow_fail=True)
        step("5/6  IDV sibling expansion",
             "python3 enrich_contracts.py --idv", allow_fail=True)
    else:
        print("\n  4/6  Modifications — skipped")
        print("  5/6  IDV expansion — skipped")

    # Step 6: Build web data
    step("6/6  Build web/data/results.json", "python3 build_web.py")

    upload_state()

    elapsed = (datetime.now(timezone.utc) - start).seconds
    print(f"\n{'═' * 60}")
    print(f"  Pipeline complete in {elapsed}s")
    print(f"{'═' * 60}")


if __name__ == "__main__":
    main()

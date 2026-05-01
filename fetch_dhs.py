"""
fetch_dhs.py — Download all DHS contracts from USASpending bulk archives.

No NAICS filter — pulls everything so keyword search can find AI/ML contracts
across all service categories. Description fields (transaction_description,
prime_award_base_transaction_description) are 100% populated and are the
primary signal for AI/ML classification downstream.

Run:
    python3 fetch_dhs.py                      # FY2022–present
    python3 fetch_dhs.py --fy 2026            # single year
    python3 fetch_dhs.py --force              # re-download all
"""

import argparse
import csv
import io
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ARCHIVE_BASE   = "https://files.usaspending.gov/award_data_archive/"
CHECKPOINT_DIR = Path("data/dhs_checkpoints")
OUTPUT_CSV     = Path("data/dhs_contracts.csv")
# Records the literal archive ZIPs we ingested (datestamp + URL list) so
# the dashboard's "Download bulk CSV" provenance link points at the real
# upstream files, not just the archive root index page.
FETCH_METADATA = Path("data/fetch_metadata.json")
AGENCY_CODE    = "070"  # DHS

def _get_latest_datestamp(fallback: str = "20260406") -> str:
    try:
        r = requests.get(ARCHIVE_BASE, timeout=15)
        r.raise_for_status()
        dates = re.findall(r'Contracts_Full_(\d{8})\.zip', r.text)
        if dates:
            latest = max(dates)
            print(f"Auto-detected datestamp: {latest}")
            return latest
    except Exception as exc:
        print(f"Could not auto-detect datestamp ({exc}), using fallback {fallback}")
    return fallback

DATESTAMP = _get_latest_datestamp()

def _current_fy() -> int:
    today = date.today()
    return today.year + 1 if today.month >= 10 else today.year

DEFAULT_YEARS = list(range(_current_fy(), _current_fy() - 5, -1))  # 5 years

# ---------------------------------------------------------------------------
# Columns to keep
# ---------------------------------------------------------------------------

KEEP_COLUMNS = [
    # Identity & key
    "contract_award_unique_key",
    "award_id_piid",
    "modification_number",
    "transaction_number",
    "parent_award_id_piid",

    # Dollars
    "federal_action_obligation",
    "total_dollars_obligated",
    "base_and_exercised_options_value",
    "current_total_value_of_award",
    "base_and_all_options_value",
    "potential_total_value_of_award",

    # Dates
    "action_date",
    "action_date_fiscal_year",
    "period_of_performance_start_date",
    "period_of_performance_current_end_date",
    "period_of_performance_potential_end_date",
    "solicitation_date",

    # Agency (DHS sub-agency breakdown)
    "awarding_agency_code",
    "awarding_agency_name",
    "awarding_sub_agency_name",
    "awarding_office_code",
    "awarding_office_name",

    # Recipient / vendor
    "recipient_uei",
    "recipient_name",
    "recipient_doing_business_as_name",
    "recipient_parent_uei",
    "recipient_parent_name",
    "cage_code",
    "recipient_state_code",
    "recipient_country_code",

    # What they were buying
    "award_type_code",
    "award_type",
    "award_description",
    "naics_code",
    "naics_description",
    "product_or_service_code",
    "product_or_service_code_description",
    "information_technology_commercial_item_category_code",
    "information_technology_commercial_item_category",
    "performance_based_service_acquisition_code",
    "performance_based_service_acquisition",

    # Description text — primary AI signal
    "transaction_description",
    "prime_award_base_transaction_description",

    # Competition & contract type
    "extent_competed_code",
    "extent_competed",
    "type_of_contract_pricing_code",
    "type_of_contract_pricing",
    "solicitation_procedures_code",
    "solicitation_procedures",
    "type_of_set_aside_code",
    "type_of_set_aside",
    "number_of_offers_received",

    # Modification reason
    "action_type_code",
    "action_type_description",

    # Place of performance
    "primary_place_of_performance_state_code",
    "primary_place_of_performance_country_code",

    # Firm characteristics
    "contracting_officers_determination_of_business_size_code",
    "contracting_officers_determination_of_business_size",
    "small_disadvantaged_business",
    "woman_owned_business",
    "veteran_owned_business",
    "service_disabled_veteran_owned_business",
    "c8a_program_participant",
    "historically_underutilized_business_zone_hubzone_firm",

    # Parent vehicle
    "parent_award_agency_name",
    "type_of_idc_code",
    "type_of_idc",

    # Solicitation linkage (for joining back to RFP pipeline)
    "solicitation_identifier",

    # Link
    "usaspending_permalink",
]

# ---------------------------------------------------------------------------
# Download helpers (identical pattern to fetch_bulk.py)
# ---------------------------------------------------------------------------

NOT_FOUND  = "NOT_FOUND"
IP_BLOCKED = "IP_BLOCKED"
FAILED     = "FAILED"


def download_zip(url: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            r = requests.get(url, stream=True, timeout=600)
            if r.status_code == 404:
                return NOT_FOUND
            if r.status_code >= 500:
                return IP_BLOCKED
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            downloaded = 0
            last_print = 0
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    mb = downloaded / 1024 / 1024
                    if mb - last_print >= 50:
                        print(f"{mb:.0f}MB...", end=" ", flush=True)
                        last_print = mb
            tmp.close()
            return tmp.name
        except requests.exceptions.ConnectionError:
            return IP_BLOCKED
        except Exception as exc:
            wait = min(30 * (attempt + 1), 180)
            print(f"\n    retry {attempt+1}/{max_retries} in {wait}s ({exc})...")
            time.sleep(wait)
    return FAILED


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def checkpoint_path(fy: int) -> Path:
    return CHECKPOINT_DIR / f"FY{fy}_{AGENCY_CODE}.csv"

def not_found_path(fy: int) -> Path:
    return CHECKPOINT_DIR / f"FY{fy}_{AGENCY_CODE}.not_found"

def is_done(fy: int) -> bool:
    return checkpoint_path(fy).exists() or not_found_path(fy).exists()


# ---------------------------------------------------------------------------
# R2 sync helpers
# ---------------------------------------------------------------------------

def _r2_client():
    import boto3
    from botocore.config import Config
    account_id = os.environ["CF_R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

R2_PREFIX = "dhs_contracts/"


def download_checkpoints_from_r2() -> None:
    if not os.environ.get("CF_R2_ACCOUNT_ID"):
        return
    print("Downloading checkpoints from R2...")
    s3 = _r2_client()
    bucket = os.environ["CF_R2_BUCKET"]
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=R2_PREFIX + "checkpoints/"):
        for obj in page.get("Contents", []):
            local = CHECKPOINT_DIR / Path(obj["Key"]).name
            if not local.exists():
                s3.download_file(bucket, obj["Key"], str(local))
                print(f"  R2 → {local.name}")
                count += 1
    print(f"  {count} checkpoint(s) downloaded")


def upload_checkpoints_to_r2() -> None:
    if not os.environ.get("CF_R2_ACCOUNT_ID"):
        return
    s3 = _r2_client()
    bucket = os.environ["CF_R2_BUCKET"]
    count = 0
    for f in sorted(CHECKPOINT_DIR.glob("FY*")):
        if f.suffix in {".csv", ".not_found"}:
            key = R2_PREFIX + "checkpoints/" + f.name
            s3.upload_file(str(f), bucket, key)
            count += 1
    print(f"  {count} checkpoint(s) → R2")


def upload_csv_to_r2() -> None:
    if not os.environ.get("CF_R2_ACCOUNT_ID"):
        return
    s3 = _r2_client()
    bucket = os.environ["CF_R2_BUCKET"]
    s3.upload_file(str(OUTPUT_CSV), bucket, R2_PREFIX + OUTPUT_CSV.name)
    print(f"  {OUTPUT_CSV.name} → R2")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Download DHS contracts from USASpending bulk archives")
    parser.add_argument("--fy", nargs="+", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--force", action="store_true", help="Re-download even if checkpoint exists")
    args = parser.parse_args()

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    download_checkpoints_from_r2()

    if args.force:
        for fy in args.fy:
            for p in [checkpoint_path(fy), not_found_path(fy)]:
                if p.exists():
                    print(f"  --force: removing {p.name}")
                    p.unlink()

    already = sum(1 for fy in args.fy if is_done(fy))
    todo    = sum(1 for fy in args.fy if not is_done(fy))
    print(f"Agency: DHS ({AGENCY_CODE})  |  Years: {args.fy}")
    print(f"Already done: {already}  |  To download: {todo}\n")

    ip_blocked = False
    total_kept = 0

    for fy in args.fy:
        if ip_blocked:
            break
        if is_done(fy):
            print(f"FY{fy}: already done, skipping")
            continue

        url = f"{ARCHIVE_BASE}FY{fy}_{AGENCY_CODE}_Contracts_Full_{DATESTAMP}.zip"
        print(f"FY{fy}: downloading {url} ...", end=" ", flush=True)
        resp = download_zip(url)

        if resp is IP_BLOCKED:
            print("IP BLOCKED — stopping.")
            ip_blocked = True
            break
        if resp is FAILED:
            print("FAILED — will retry next run")
            continue
        if resp is NOT_FOUND:
            print("404 — no file for this FY")
            not_found_path(fy).touch()
            continue

        zip_path = resp
        zip_mb = os.path.getsize(zip_path) / 1024 / 1024
        print(f"{zip_mb:.1f} MB  |  scanning...", end=" ", flush=True)

        rows_scanned = rows_kept = 0
        cp = checkpoint_path(fy)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
                if not csv_names:
                    print("no CSV in zip")
                    cp.touch()
                    continue

                with open(cp, "w", newline="", encoding="utf-8") as out_f:
                    writer = csv.DictWriter(out_f, fieldnames=KEEP_COLUMNS, extrasaction="ignore")
                    writer.writeheader()

                    for csv_name in csv_names:
                        with zf.open(csv_name) as raw:
                            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
                            for row in reader:
                                rows_scanned += 1
                                if rows_scanned % 100_000 == 0:
                                    print(f"{rows_scanned // 1000}k...", end=" ", flush=True)
                                kept = {col: row.get(col, "") for col in KEEP_COLUMNS}
                                writer.writerow(kept)
                                rows_kept += 1

        except Exception as exc:
            print(f"\n    ERROR: {exc}")
            if cp.exists():
                cp.unlink()
            os.unlink(zip_path)
            continue

        os.unlink(zip_path)
        total_kept += rows_kept
        print(f"scanned {rows_scanned:,}  →  kept {rows_kept:,} rows")

    upload_checkpoints_to_r2()

    # Merge checkpoints into single CSV
    print(f"\nMerging checkpoints...")
    frames = []
    for cp in sorted(CHECKPOINT_DIR.glob("FY*.csv")):
        if cp.stat().st_size > 0:
            try:
                with open(cp, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        frames.append((cp.name, rows))
            except Exception:
                pass

    if not frames:
        print("No rows found.")
        Path("data/scan_status.txt").write_text("blocked" if ip_blocked else "done")
        return

    total_rows = sum(len(rows) for _, rows in frames)
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=KEEP_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for _, rows in frames:
            writer.writerows(rows)

    print(f"Wrote {total_rows:,} rows to {OUTPUT_CSV}")

    if not ip_blocked:
        upload_csv_to_r2()

    status = "blocked" if ip_blocked else "done"
    Path("data/scan_status.txt").write_text(status)

    # Record fetch metadata so build_web.py can show the actual archive
    # URLs in the dashboard provenance block. Only includes FYs whose
    # checkpoints exist (i.e. ones that actually downloaded — not 404s).
    fy_urls = []
    for fy in sorted(set(args.fy)):
        cp = checkpoint_path(fy)
        if cp.exists() and cp.stat().st_size > 0:
            fy_urls.append({
                "fy": fy,
                "url": f"{ARCHIVE_BASE}FY{fy}_{AGENCY_CODE}_Contracts_Full_{DATESTAMP}.zip",
                "filename": f"FY{fy}_{AGENCY_CODE}_Contracts_Full_{DATESTAMP}.zip",
            })
    FETCH_METADATA.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "datestamp":  DATESTAMP,
        "agency_code": AGENCY_CODE,
        "archive_base": ARCHIVE_BASE,
        "fy_archives": fy_urls,
    }, indent=2))
    print(f"Wrote {FETCH_METADATA} ({len(fy_urls)} FY archives)")

    if ip_blocked:
        print("IP blocked — re-run to continue. Progress saved.")
    else:
        print("Done!")


if __name__ == "__main__":
    main()

"""scripts/check_links.py — HEAD-check every URL the dashboard emits.

Structural URL tests in tests/link_pattern_test.py only verify that
URLs match a regex; they don't verify the URL actually resolves. This
script does the live check.

Run:
    python3 scripts/check_links.py                  # check all
    python3 scripts/check_links.py --sample 20      # 20 random permalinks
    python3 scripts/check_links.py --strict         # exit 1 on any failure

What it checks:
  - Every contract `permalink`           (sampled — there are O(100) of them)
  - methodology.bulk_archive_url
  - methodology.data_source_url
  - methodology.data_dictionary_url
  - methodology.fetch_metadata.fy_archives[].url

USASpending's HEAD endpoint returns 403 for some user-agents; this
script falls back to a small GET that drops the response body.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

RESULTS = Path(__file__).resolve().parents[1] / "web" / "data" / "results.json"
UA      = "dhs-ai-contracts-link-checker/1 (https://github.com/abigailhaddad/dhs-ai-contracts)"
TIMEOUT = 20


def check_one(url: str) -> tuple[str, int, str]:
    """Return (url, status_code, error_msg). status_code 0 means request
    failed before getting a response."""
    headers = {"User-Agent": UA}
    try:
        # HEAD first; some hosts (USASpending CDN) reject HEAD or return
        # misleading codes, so fall back to a short GET.
        r = requests.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (403, 405) or r.status_code >= 500:
            r = requests.get(url, headers=headers, timeout=TIMEOUT,
                             allow_redirects=True, stream=True)
            r.close()
        return (url, r.status_code, "")
    except requests.RequestException as exc:
        return (url, 0, str(exc).split("\n")[0][:160])


def collect_urls(d: dict, sample: int) -> list[tuple[str, str]]:
    """Return [(category, url), ...]. Permalinks are sampled when there
    are many; the others are checked in full."""
    out: list[tuple[str, str]] = []
    m = d.get("methodology") or {}
    for key in ("data_source_url", "data_dictionary_url", "bulk_archive_url"):
        if m.get(key):
            out.append((f"methodology.{key}", m[key]))
    fm = m.get("fetch_metadata") or {}
    for a in fm.get("fy_archives", []):
        url = a.get("url", "")
        if url:
            out.append((f"fetch_metadata.fy_archives[FY{a.get('fy')}]", url))
    permalinks = [c["permalink"] for c in d.get("contracts", []) if c.get("permalink")]
    if sample > 0 and len(permalinks) > sample:
        random.seed(42)
        permalinks = random.sample(permalinks, sample)
    for url in permalinks:
        out.append(("contracts[].permalink", url))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=20,
                   help="Number of permalinks to check (default 20). 0 = all.")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any URL fails — useful for CI.")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel HTTP workers (default 8).")
    args = p.parse_args()

    if not RESULTS.exists():
        print(f"ERROR: {RESULTS} not present. Run `python build_web.py` first.")
        return 2

    d = json.loads(RESULTS.read_text())
    urls = collect_urls(d, args.sample)
    print(f"Checking {len(urls)} URLs with {args.workers} workers...\n")

    fails: list[tuple[str, str, int, str]] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_one, u): (cat, u) for cat, u in urls}
        for fut in as_completed(futures):
            cat, u = futures[fut]
            url, status, err = fut.result()
            ok = 200 <= status < 400
            sym = "✓" if ok else "✗"
            print(f"  {sym} [{status or '---'}] {cat:50}  {url}")
            if not ok:
                fails.append((cat, url, status, err))

    elapsed = time.time() - started
    print(f"\n{len(urls) - len(fails)}/{len(urls)} OK in {elapsed:.1f}s")
    if fails:
        print(f"\nFAILURES ({len(fails)}):")
        for cat, url, status, err in fails:
            print(f"  [{status or '---'}] {cat}")
            print(f"    {url}")
            if err:
                print(f"    {err}")
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

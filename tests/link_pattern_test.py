"""URL pattern tests for every link the dashboard generates.

Background: across this project family we have repeatedly shipped URLs
that look right but don't actually work — sam.gov/entity/<uei>/general
404s for lapsed registrations, usaspending.gov/recipient/<uei> needs a
UUID hash, sam.gov/opp/<solicitation_id>/view wants a notice UUID not
a solicitation identifier. Each one was caught only after a user clicked
it.

This test enforces a catalog of URL patterns against web/data/results.json.
Every URL the site emits must match exactly one pattern. New URL fields
must be added to the catalog or the test fails.

Structural check only — for liveness, run scripts/check_links.py separately.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WEB_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
WEB_HTML = Path(__file__).resolve().parents[1] / "web"

# Every URL field that ships in a contract record. Add the regex here when
# you add a new URL field to results.json — otherwise this test will
# fail with "unknown URL field." That's the whole point.
CONTRACT_URL_PATTERNS = {
    # USASpending per-action page. The {gid} we use to build this MUST start
    # with CONT_AWD_ or CONT_IDV_ — bare award_id like "70SBUR21F00000194"
    # silently 404s on usaspending.gov even though it looks like a valid
    # award page. The pattern enforces that prefix.
    "permalink": re.compile(r"^https://www\.usaspending\.gov/award/CONT_(AWD|IDV)_[A-Z0-9_-]+/?$"),
    # raw_url is a relative URL into our own site (raw.html?piid=...).
    "raw_url":   re.compile(r"^raw\.html\?piid=[A-Za-z0-9._%~()-]+$"),
}

# IDV-sibling rows nested inside a contract. They don't carry their own
# URL fields right now, but if any are added later they should appear in
# the catalog above (path with [] for list elements) or be added here.
CONTRACT_NESTED_URL_PATTERNS: dict[str, re.Pattern] = {}

# Methodology block at top level — every URL field there too.
METHODOLOGY_URL_PATTERNS = {
    "data_source_url":  re.compile(r"^https://www\.usaspending\.gov/?$"),
    "bulk_archive_url": re.compile(r"^https://files\.usaspending\.gov/award_data_archive/?$"),
}


def _load(name: str):
    p = WEB_DATA / name
    if not p.exists():
        pytest.fail(
            f"{p} not present — run `python build_web.py` first so these "
            f"tests can validate the on-disk snapshot."
        )
    return json.loads(p.read_text())


def test_every_contract_url_field_matches_a_pattern():
    """Every URL value in results.json contracts must match the catalog."""
    d = _load("results.json")
    contracts = d.get("contracts", [])
    assert contracts, "results.json contracts list is empty"

    for field, pattern in CONTRACT_URL_PATTERNS.items():
        values = [c.get(field) for c in contracts if c.get(field)]
        assert values, (
            f"Field {field!r} present in catalog but missing from every contract. "
            f"Either drop it from CONTRACT_URL_PATTERNS or fix build_web.py to emit it."
        )
        bad = [v for v in values[:50] if not pattern.match(v)]
        assert not bad, (
            f"contracts[].{field} has values that don't match {pattern.pattern!r}. "
            f"First offender: {bad[0]!r}. Fix the URL builder in build_web.py "
            f"or update CONTRACT_URL_PATTERNS."
        )


def test_methodology_block_urls_match_catalog():
    d = _load("results.json")
    m = d.get("methodology") or {}
    for field, pattern in METHODOLOGY_URL_PATTERNS.items():
        v = m.get(field)
        assert v, f"methodology.{field} missing — required for raw.html provenance block"
        assert pattern.match(v), (
            f"methodology.{field}={v!r} doesn't match {pattern.pattern!r}"
        )


def test_no_unknown_url_fields_in_contracts():
    """Every value that LOOKS like a URL or relative .html link in a
    contract record must be registered in the catalog. New URL fields
    should be added to CONTRACT_URL_PATTERNS so they get pattern-checked."""
    d = _load("results.json")
    contracts = d.get("contracts", [])
    if not contracts:
        return
    url_like = re.compile(r"^(https?://|raw\.html\?)")
    known = set(CONTRACT_URL_PATTERNS.keys())
    sample = contracts[0]
    found_url_fields = {k for k, v in sample.items()
                        if isinstance(v, str) and url_like.match(v)}
    unknown = found_url_fields - known
    assert not unknown, (
        f"contracts[0] has URL-like fields not in the catalog: {sorted(unknown)}. "
        f"Add them to CONTRACT_URL_PATTERNS in tests/link_pattern_test.py with "
        f"a regex that captures the expected shape."
    )


def test_no_permalink_falls_back_to_bare_award_id():
    """Specific regression guard: build_web.make_permalink uses
    `gid or aid`. When the pipeline drops `generated_internal_id` for a
    contract (it's happened — see commits 1b01730 and 0e74c9b) the
    permalink falls back to the bare Award ID and the resulting URL
    silently 404s on usaspending.gov. Catch this at build time."""
    d = _load("results.json")
    bad = []
    for c in d.get("contracts", []):
        p = c.get("permalink") or ""
        if "CONT_AWD_" not in p and "CONT_IDV_" not in p:
            bad.append((c.get("award_id"), p))
    assert not bad, (
        f"{len(bad)} contracts have permalinks lacking CONT_AWD_/CONT_IDV_ "
        f"prefix — the gid fallback to bare award_id silently 404s. First few: "
        f"{bad[:3]}. Fix build_web.make_permalink or the upstream pipeline that "
        f"failed to generate a generated_internal_id."
    )


def test_no_uei_only_vendor_urls():
    """Two URL families that look right but reliably 404 given just a UEI:
        - sam.gov/entity/<uei>/general — fails for lapsed SAM registrations
        - usaspending.gov/recipient/<uei> — needs a UUID hash, not the UEI
    We don't emit these from this project, but if someone tries to add
    them later this test catches it."""
    d = _load("results.json")
    contracts = d.get("contracts", [])
    if not contracts:
        return
    sample = contracts[0]
    for forbidden in ("sam_url", "recipient_url", "vendor_profile_url"):
        assert forbidden not in sample, (
            f"contracts[].{forbidden} is forbidden — no UEI-only URL pattern "
            f"reliably resolves. Drop it; users follow `permalink` instead."
        )


def test_raw_html_takes_only_piid_param():
    """raw.html is keyed on award_id (piid). If the page starts accepting
    a different param shape we want the test to remind us to update
    CONTRACT_URL_PATTERNS — and to update build_web.make_raw_link()."""
    raw = (WEB_HTML / "raw.html").read_text()
    # The page must read the 'piid' query param somewhere.
    assert re.search(r"\.get\(['\"]piid['\"]\)", raw), (
        "raw.html no longer reads ?piid= — update CONTRACT_URL_PATTERNS['raw_url'] "
        "and build_web.make_raw_link() to match the new param name."
    )


def test_index_html_inline_urls_match_known_shapes():
    """Hardcoded URLs in index.html's inline <script> and HTML must match
    a known-good shape. New external links should be added to this list."""
    src = (WEB_HTML / "index.html").read_text()
    # Pull every absolute URL out of the source and assert each matches one
    # of the patterns below.
    urls = re.findall(r"https://[^\s\"'<>)]+", src)
    allowed = [
        re.compile(r"^https://www\.usaspending\.gov/?(?:\?|$|award/|api/|data-dictionary)"),
        re.compile(r"^https://api\.usaspending\.gov/api/"),
        re.compile(r"^https://files\.usaspending\.gov/award_data_archive/"),
        re.compile(r"^https://github\.com/abigailhaddad/dhs-ai-contracts/?$"),
        re.compile(r"^https://fonts\.(googleapis|gstatic)\.com($|/)"),
        re.compile(r"^https://cdn\.(jsdelivr\.net|datatables\.net)/"),
        re.compile(r"^https://code\.jquery\.com/"),
    ]
    bad = [u for u in urls if not any(p.match(u) for p in allowed)]
    assert not bad, (
        f"index.html has URLs not in the allow-list: {bad[:5]}. "
        f"Either fix the URL or add a pattern to test_index_html_inline_urls_match_known_shapes."
    )

"""Shape checks for web/data/results.json as it exists on disk.

Catches stale snapshots and silent build_web regressions: if a frontend
field disappears or a new classification source ships without provenance
metadata, these tests fail before the data hits production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WEB_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
RESULTS  = WEB_DATA / "results.json"

REQUIRED_TOP_LEVEL = {"generated_at", "summary", "by_agency", "contracts",
                      "keywords_searched", "methodology"}

REQUIRED_SUMMARY_KEYS = {
    "total_ai_contracts", "total_ai_dollars", "total_candidates",
    "not_ai_filtered_out", "sub_agencies_with_ai",
    "from_keyword_search", "from_modification_text", "from_idv_expansion",
}

REQUIRED_CONTRACT_KEYS = {
    "award_id", "vendor", "agency", "amount", "description",
    "keyword", "explanation", "generated_internal_id",
    "permalink", "raw_url", "source",
}

REQUIRED_METHODOLOGY_KEYS = {
    "llm_model", "data_source", "data_source_url", "data_dictionary_url",
    "bulk_archive_url", "agency_filter", "fy_range", "keyword_count",
    "fetch_metadata",
}

# Allowed values for field_provenance entries' "source" field. New
# values must be added to raw.html's CSS (.src-tag.<kind>) AND the
# index.html source-badge CSS — the test below catches drift.
ALLOWED_PROVENANCE_SOURCES = {"usaspending_csv", "derived", "llm", "internal"}

# Sources the dashboard advertises in stat cards. If a new source is added
# (e.g. "rfp_text"), update this set and add per-source provenance copy
# to raw.html before shipping — otherwise users see "(unknown source)".
KNOWN_SOURCES = {"keyword_search", "modification_text", "idv_expansion"}


def _load():
    if not RESULTS.exists():
        pytest.fail(
            f"{RESULTS} not present. Run `python build_web.py` to generate "
            f"the snapshot these tests validate."
        )
    return json.loads(RESULTS.read_text())


def test_top_level_keys_present():
    d = _load()
    missing = REQUIRED_TOP_LEVEL - set(d.keys())
    assert not missing, f"results.json missing top-level keys: {missing}"


def test_summary_keys_present_and_numeric():
    d = _load()
    s = d["summary"]
    missing = REQUIRED_SUMMARY_KEYS - set(s.keys())
    assert not missing, f"summary missing keys: {missing}"
    # The dashboard does s.total_ai_dollars/1e6 — non-numeric crashes the
    # toFixed call without a meaningful error.
    for k in ("total_ai_dollars", "total_ai_contracts"):
        assert isinstance(s[k], (int, float)), f"summary.{k} must be numeric, got {type(s[k]).__name__}"


def test_summary_counts_match_contracts_list():
    """The stat cards on the homepage display from_keyword_search +
    from_modification_text + from_idv_expansion — they must add to the
    actual contract count or the user sees inconsistent numbers."""
    d = _load()
    s = d["summary"]
    by_source: dict[str, int] = {}
    for c in d["contracts"]:
        src = c.get("source", "")
        by_source[src] = by_source.get(src, 0) + 1
    assert by_source.get("keyword_search", 0) == s.get("from_keyword_search", -1), (
        "from_keyword_search disagrees with contracts list"
    )
    assert by_source.get("modification_text", 0) == s.get("from_modification_text", -1), (
        "from_modification_text disagrees with contracts list"
    )
    assert by_source.get("idv_expansion", 0) == s.get("from_idv_expansion", -1), (
        "from_idv_expansion disagrees with contracts list"
    )
    assert sum(by_source.values()) == s["total_ai_contracts"], (
        "total_ai_contracts disagrees with contracts list"
    )


def test_every_contract_has_required_fields():
    d = _load()
    contracts = d["contracts"]
    assert contracts, "contracts list is empty — frontend renders an empty table"
    for i, c in enumerate(contracts):
        missing = REQUIRED_CONTRACT_KEYS - set(c.keys())
        assert not missing, (
            f"contracts[{i}] (award_id={c.get('award_id')!r}) missing keys: {missing}"
        )


def test_every_contract_has_known_source():
    d = _load()
    bad = [(c.get("award_id"), c.get("source")) for c in d["contracts"]
           if c.get("source") not in KNOWN_SOURCES]
    assert not bad, (
        f"{len(bad)} contracts have unknown source values: {bad[:3]}. "
        f"Either add the new source to KNOWN_SOURCES + add per-source "
        f"provenance copy to raw.html provenanceFor(), or fix build_web.py."
    )


def test_modification_contracts_carry_mod_text():
    """raw.html highlights the modification text that flipped the verdict.
    If mod_text is missing the user sees an empty highlight box and has
    no way to verify the re-classification."""
    d = _load()
    bad = [c.get("award_id") for c in d["contracts"]
           if c.get("source") == "modification_text" and not c.get("mod_text")]
    assert not bad, (
        f"{len(bad)} modification_text contracts missing mod_text: {bad[:3]}. "
        f"raw.html cannot show the trigger without it. Fix build_web.py."
    )


def test_idv_expansion_contracts_carry_parent_and_siblings():
    """raw.html shows the sibling AI awards under the parent IDV — the
    evidence for why we expanded to this child task order. Missing siblings
    means the user sees a bare claim with no audit trail."""
    d = _load()
    for c in d["contracts"]:
        if c.get("source") != "idv_expansion":
            continue
        aid = c.get("award_id")
        assert c.get("idv_parent"), (
            f"idv_expansion contract {aid} missing idv_parent — raw.html "
            f"cannot show the parent vehicle that justified inclusion."
        )
        sibs = c.get("idv_siblings")
        assert isinstance(sibs, list), (
            f"idv_expansion contract {aid} idv_siblings is not a list "
            f"(got {type(sibs).__name__})"
        )
        # We don't assert non-empty: it's possible (rare) that a parent
        # IDV has only this one expansion. But every sibling row that DOES
        # exist must have the fields raw.html reads.
        for s in sibs:
            for k in ("award_id", "amount", "description",
                      "generated_internal_id", "source"):
                assert k in s, (
                    f"idv_siblings under {aid} missing key {k!r}: {s}"
                )


def test_methodology_block_present_and_complete():
    """raw.html's provenance block reads from methodology — missing fields
    silently fall back to hardcoded defaults that drift from the pipeline."""
    d = _load()
    m = d.get("methodology") or {}
    missing = REQUIRED_METHODOLOGY_KEYS - set(m.keys())
    assert not missing, f"methodology block missing keys: {missing}"
    assert m["llm_model"], "methodology.llm_model is empty"
    assert isinstance(m["keyword_count"], int) and m["keyword_count"] > 0, (
        "methodology.keyword_count must be a positive integer"
    )


def test_keywords_searched_matches_methodology_count():
    d = _load()
    m = d.get("methodology") or {}
    assert m.get("keyword_count") == len(d.get("keywords_searched", [])), (
        f"methodology.keyword_count={m.get('keyword_count')} disagrees with "
        f"len(keywords_searched)={len(d.get('keywords_searched', []))}"
    )


def test_by_agency_dollars_match_contract_total():
    """The agency bar chart must sum to the headline total dollar figure
    or the user sees one number on the homepage and a different total
    when they sum the chart bars."""
    d = _load()
    chart_total = sum(a.get("dollars", 0.0) for a in d.get("by_agency", []))
    headline = d["summary"]["total_ai_dollars"]
    # Allow tiny float tolerance.
    assert abs(chart_total - headline) < 1.0, (
        f"by_agency total ${chart_total:,.0f} != summary.total_ai_dollars "
        f"${headline:,.0f} — build_web aggregation drift."
    )


def test_field_provenance_block_is_complete():
    """Every key that appears on a contract row must have an entry in
    results.json.field_provenance — that's how raw.html shows users
    'this value came from upstream column X.' If build_web ships a new
    field without registering provenance for it, raw.html silently shows
    no source tag and the user has no audit trail."""
    d = _load()
    fp = d.get("field_provenance") or {}
    assert fp, "field_provenance block missing or empty in results.json"
    # Collect every key seen across contracts (some keys are source-specific,
    # e.g. mod_text only on modification_text rows, idv_parent only on
    # idv_expansion rows).
    seen_keys: set[str] = set()
    for c in d["contracts"]:
        seen_keys.update(c.keys())
    undocumented = seen_keys - set(fp.keys())
    assert not undocumented, (
        f"Contract fields shipped without field_provenance entries: "
        f"{sorted(undocumented)}. Add them to FIELD_PROVENANCE in build_web.py "
        f"with source/column/note so raw.html can show users where each "
        f"value came from."
    )


def test_field_provenance_entries_are_well_formed():
    d = _load()
    fp = d.get("field_provenance") or {}
    for field, row in fp.items():
        assert isinstance(row, dict), f"field_provenance[{field}] not a dict"
        for required in ("source", "column", "note"):
            assert row.get(required), (
                f"field_provenance[{field}] missing {required!r}. "
                f"Every entry needs source / column / note for raw.html to render."
            )
        assert row["source"] in ALLOWED_PROVENANCE_SOURCES, (
            f"field_provenance[{field}].source={row['source']!r} not in "
            f"{ALLOWED_PROVENANCE_SOURCES}. Add the new kind to raw.html .src-tag "
            f"CSS and to ALLOWED_PROVENANCE_SOURCES in this test."
        )


def test_methodology_fetch_metadata_matches_archive_url_pattern():
    """If fetch_metadata is present, every fy_archives URL must point at a
    real-looking USASpending bulk ZIP (FY{n}_070_Contracts_Full_<datestamp>.zip).
    Catches a hypothetical regression where fetch_dhs.py writes a malformed
    URL into the metadata file."""
    import re as _re
    d = _load()
    fm = (d.get("methodology") or {}).get("fetch_metadata")
    if fm is None:
        # Fresh checkout, no fetch yet — that's allowed; the dashboard
        # falls back to the archive root URL.
        return
    archives = fm.get("fy_archives") or []
    pat = _re.compile(
        r"^https://files\.usaspending\.gov/award_data_archive/"
        r"FY\d{4}_070_Contracts_Full_\d{8}\.zip$"
    )
    for a in archives:
        assert pat.match(a.get("url", "")), (
            f"fetch_metadata.fy_archives URL doesn't match the bulk ZIP "
            f"pattern: {a.get('url')!r}. Fix fetch_dhs.py."
        )


def test_every_contract_award_id_unique():
    """raw.html looks up contracts by award_id. Duplicate award_ids would
    silently cause the wrong row to render, with no error visible to the
    user. (build_web uses award_id as a dict key, so any collision is
    already a data bug — but assert it explicitly here.)"""
    d = _load()
    aids = [c.get("award_id") for c in d["contracts"]]
    assert len(aids) == len(set(aids)), (
        f"Duplicate award_ids in contracts list — raw.html will render "
        f"the wrong row. Counts: {len(aids)} total, {len(set(aids))} unique."
    )

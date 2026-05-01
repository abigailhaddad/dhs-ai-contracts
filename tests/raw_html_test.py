"""Structural checks on web/raw.html.

raw.html is the provenance viewer — every claim on the dashboard's
homepage links here, and every classification source the pipeline ships
needs a corresponding render path in this page. These tests catch the
class of bug where:
  - Pipeline gains a new source (e.g. "rfp_text") but raw.html
    falls through to "(unknown source)".
  - raw.html stops reading a field that build_web still emits (or vice
    versa) and a user sees blank provenance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WEB_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
WEB_HTML = Path(__file__).resolve().parents[1] / "web"
RAW_HTML = WEB_HTML / "raw.html"
RESULTS  = WEB_DATA / "results.json"

# Sources the pipeline ships, mirrored from build_web.py. raw.html must
# have a render path for each one. If you add a source, update both
# files AND this list.
EXPECTED_SOURCES = ["keyword_search", "modification_text", "idv_expansion"]


def _read_raw():
    if not RAW_HTML.exists():
        pytest.fail(f"{RAW_HTML} not present")
    return RAW_HTML.read_text()


def _load_results():
    if not RESULTS.exists():
        pytest.fail(f"{RESULTS} not present — run `python build_web.py`")
    return json.loads(RESULTS.read_text())


def test_raw_html_handles_every_source_we_ship():
    """raw.html's provenanceFor() switches on contract.source. Every
    source value in our actual data must map to a non-fallback branch."""
    src = _read_raw()
    for source in EXPECTED_SOURCES:
        # Each branch in provenanceFor() does a quoted comparison with
        # the source name. Missing → falls through to "(unknown source)".
        assert f"'{source}'" in src, (
            f"raw.html provenanceFor() has no branch for source={source!r}. "
            f"Users hitting this contract see the fallback '(unknown source)' "
            f"copy instead of meaningful provenance. Add a branch."
        )


def test_raw_html_reads_methodology_fields():
    """raw.html populates the provenance block from results.json.methodology.
    These reads must exist or the block silently falls back to defaults."""
    src = _read_raw()
    for field in ("data_source", "data_source_url", "bulk_archive_url",
                  "agency_filter", "fy_range", "llm_model"):
        assert f"m.{field}" in src, (
            f"raw.html no longer reads methodology.{field}. Either drop it "
            f"from build_web.py's methodology block or restore the read."
        )


def test_raw_html_renders_field_provenance():
    """raw.html must read d.field_provenance and render src-tag rows. If
    that wiring is removed, users see values with no upstream-column hint."""
    src = _read_raw()
    assert "field_provenance" in src, (
        "raw.html no longer reads d.field_provenance — users lose the "
        "per-value column-source tags."
    )
    assert "renderSrcTag" in src, (
        "raw.html no longer calls renderSrcTag — provenance tags won't render."
    )


def test_raw_html_handles_idv_siblings_payload():
    """The IDV expansion render path reads idv_parent + idv_siblings.
    If build_web.py renames either, raw.html breaks silently."""
    src = _read_raw()
    assert "idv_parent" in src, "raw.html no longer reads idv_parent"
    assert "idv_siblings" in src, "raw.html no longer reads idv_siblings"


def test_raw_html_handles_mod_text_payload():
    src = _read_raw()
    assert "mod_text" in src, "raw.html no longer reads mod_text"


def test_raw_html_loads_results_json_not_some_other_file():
    """Sanity: the page reads from data/results.json (the same file the
    dashboard reads). If it ever fetches a separate raw_data.json that
    we don't generate, every page will 404."""
    src = _read_raw()
    fetches = re.findall(r"fetch\(['\"]([^'\"]+)['\"]", src)
    assert "data/results.json" in fetches, (
        f"raw.html doesn't fetch data/results.json. Found fetches: {fetches}"
    )


def test_every_contract_resolves_in_raw_html_logic():
    """For every contract in our shipped data, simulate the raw.html
    lookup: results.contracts.find(c.award_id === piid). If any contract's
    award_id wouldn't be findable (empty / non-string), raw.html shows
    'No contract found with award_id …' even though the dashboard listed it."""
    d = _load_results()
    for i, c in enumerate(d.get("contracts", [])):
        aid = c.get("award_id")
        assert isinstance(aid, str) and aid, (
            f"contracts[{i}] has unusable award_id {aid!r} — raw.html "
            f"cannot look this contract up by ?piid="
        )


def test_raw_html_handles_aggregate_flags():
    """raw.html?flag=summary, ?flag=agency, ?flag=source back the homepage
    stat cards and the agency chart bars. Each branch must exist in the
    page or those clicks fall through to '(unknown flag)'."""
    src = _read_raw()
    for flag in ("summary", "agency", "source"):
        assert f"'{flag}'" in src, (
            f"raw.html no longer handles ?flag={flag} — the corresponding "
            f"homepage entry point will fall through to an error. Restore "
            f"the renderAggregatePage branch."
        )


def test_index_html_stat_cards_link_to_raw_aggregate():
    """Every clickable stat card on the homepage must point at a real
    raw.html?flag=... URL. If the link disappears or is malformed, users
    can't drill from the headline number to the underlying contracts."""
    src = (WEB_HTML / "index.html").read_text()
    expected = [
        "raw.html?flag=summary",
        "raw.html?flag=source&source=keyword_search",
        "raw.html?flag=source&source=modification_text",
        "raw.html?flag=source&source=idv_expansion",
    ]
    for url in expected:
        assert url in src, (
            f"index.html no longer links to {url!r} — homepage stat card "
            f"loses its drill-down. Restore the link."
        )


def test_index_html_agency_chart_has_bar_click_handler():
    """The agency bar chart should be clickable to drill into per-agency
    contracts. Without this, the chart is a dead-end visualization."""
    src = (WEB_HTML / "index.html").read_text()
    assert "raw.html?flag=agency&agency=" in src, (
        "index.html agency chart no longer wires clicks to "
        "raw.html?flag=agency&agency=... — bars are no longer clickable. "
        "Restore the onClick handler in the Chart options."
    )


def test_review_html_exists_and_reads_results_json():
    """The classification review tool reads the same results.json the
    dashboard does. If it stops loading or links somewhere else, the
    spot-check workflow breaks silently."""
    review = WEB_HTML / "review.html"
    assert review.exists(), f"{review} not present"
    src = review.read_text()
    assert "fetch('data/results.json')" in src or 'fetch("data/results.json")' in src, (
        "review.html no longer fetches data/results.json — review tool "
        "won't render any contracts."
    )
    # Methodology section must link to it.
    idx = (WEB_HTML / "index.html").read_text()
    assert "review.html" in idx, (
        "index.html no longer links to review.html — users have no "
        "discoverable path to the spot-check tool."
    )


def test_index_html_links_to_raw_html_from_modal_and_table():
    """Two surface areas in index.html must link to raw.html:
       1. The Source badge in the contracts table (per-row).
       2. The contract detail modal (per-contract deep-link).
    If either link disappears, users lose the provenance entry point.
    """
    src = (WEB_HTML / "index.html").read_text()
    # Badge link in renderTable: an <a> whose href references c.raw_url
    # and which contains a <span class="source-badge"> child. The regex
    # is loose so styling tweaks don't break the test — only the
    # structural "raw_url anchor wraps a source-badge span" relationship
    # is enforced.
    assert re.search(
        r"<a[^>]*c\.raw_url[^>]*>\s*<span[^>]*source-badge", src,
    ), (
        "index.html: source badge no longer wraps in an <a href={c.raw_url}>. "
        "Re-add the link or users have no per-row provenance entry point."
    )
    # Modal "View raw data" link
    assert "View raw data + classification provenance" in src, (
        "index.html: modal no longer shows the 'View raw data + "
        "classification provenance →' link. Re-add it."
    )

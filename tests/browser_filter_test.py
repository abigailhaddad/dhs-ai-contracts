"""Browser-driven filter tests for the DHS AI Contracts dashboard.

Catches the class of bug where a multiselect option exists in the dropdown
but selecting it produces zero visible rows. Every option in every
multiselect filter (agency, source, keyword) must yield at least one row
when applied — by construction the options come from contract values, so
this is true if and only if the filter wiring is correct.

Also smoke-tests the text + range filters and the FilterManager's
"Clear All" reset.

Run with:
    pytest tests/browser_filter_test.py -v

Skips automatically when:
    - playwright is not installed
    - chromium browser is not installed
    - port collision (parallel runs use the same port — change SERVER_PORT)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import Page, sync_playwright  # noqa: E402

# conftest.py is auto-discovered by pytest but must be imported explicitly
# as a module here since we use the helpers as plain functions (not
# fixtures). pytest adds the tests/ dir to sys.path during collection.
import sys
sys.path.insert(0, str(Path(__file__).parent))
from conftest import port_in_use, start_quiet_server, WEB_DIR  # noqa: E402

SERVER_PORT = 18866
INDEX_HTML  = WEB_DIR / "index.html"
RESULTS     = WEB_DIR / "data" / "results.json"


@pytest.fixture(scope="session")
def fixture_server():
    if not INDEX_HTML.exists():
        pytest.skip("web/index.html not found")
    if not RESULTS.exists():
        pytest.skip("web/data/results.json not found — run build_web.py first")
    if port_in_use(SERVER_PORT):
        pytest.skip(f"Port {SERVER_PORT} already in use")
    server = start_quiet_server(WEB_DIR, SERVER_PORT)
    yield f"http://127.0.0.1:{SERVER_PORT}"
    server.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium not available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, fixture_server) -> Page:
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(fixture_server + "/index.html")
    page.wait_for_selector("#contractTable tbody tr", timeout=15_000)
    yield page
    ctx.close()


def _visible_rows(page: Page) -> int:
    """Number of rows DataTables currently has loaded (= rows matching
    the active filter, not the count of <tr>s in the DOM — DataTables
    paginates the DOM to pageLength=25). The dashboard's filter chain
    rebuilds tbody to ONLY the post-filter set then re-instantiates
    DataTables, so DataTable.rows().count() is the true match count."""
    return page.evaluate("""() => {
        if (!window.jQuery || !$.fn.DataTable.isDataTable('#contractTable')) {
            return document.querySelectorAll('#contractTable tbody tr').length;
        }
        return $('#contractTable').DataTable().rows().count();
    }""")


# index.html ships a static .filter-popover inside #detailModal (the
# contract-detail modal). That static element is always in the DOM, so a
# plain `.filter-popover` selector matches it AND any dynamic modal the
# FilterManager spins up. We scope to `.filter-modal:not(#detailModal)`
# to avoid grabbing the static one. createModal() appends a fresh
# .filter-modal directly under <body> for each filter dialog.
DYN_MODAL    = ".filter-modal:not(#detailModal)"
DYN_POPOVER  = f"{DYN_MODAL} .filter-popover"


def _open_filter_dialog(page: Page, column_name: str):
    """Open the multiselect dialog for a given column (e.g. 'Sub-Agency').
    The FilterManager toolbar exposes '+ Add Filter' → column picker →
    multiselect dialog. We click through that flow."""
    page.locator(".add-filter-btn").click()
    page.wait_for_selector(DYN_POPOVER)
    # Pick the column by name
    page.locator(f"{DYN_POPOVER} .filter-option:has-text('{column_name}') input").click()
    page.wait_for_selector(f"{DYN_POPOVER} .filter-title:has-text('Filter:')")


def _apply_multiselect(page: Page, value: str):
    """Inside an open multiselect dialog, check exactly one option by
    its `value=` attribute and click Apply."""
    safe = value.replace('"', '\\"')
    page.locator(f'{DYN_POPOVER} input[type="checkbox"][value="{safe}"]').check()
    page.locator(f"{DYN_POPOVER} .btn-apply").click()
    # Modal removes itself from DOM on apply; wait for any dynamic modal
    # to be gone (the static #detailModal stays).
    page.wait_for_selector(DYN_MODAL, state="detached")


def _clear_all_filters(page: Page):
    btn = page.locator(".clear-filters-btn")
    if btn.is_visible():
        btn.click()
        # FilterManager.clearAll() runs onFilterChange → renderTable
        page.wait_for_function(
            "() => document.querySelector('.clear-filters-btn').style.display === 'none'"
        )


# ── tests ─────────────────────────────────────────────────────────────────────

def test_dashboard_loads_with_rows(page: Page):
    """Smoke test: the table populates from results.json."""
    assert _visible_rows(page) > 0, "contract table is empty after load"


@pytest.mark.parametrize("column,field", [
    ("Sub-Agency", "agency"),
    ("Source",     "source"),
    ("Keyword",    "keyword"),
])
def test_every_multiselect_option_yields_rows(page: Page, column: str, field: str):
    """Every option in every multiselect filter must produce ≥1 row when
    applied. Options are built from the contract data itself, so a zero
    result means the filter wiring lost the row."""
    options = json.loads(RESULTS.read_text())
    contracts = options["contracts"]
    values = sorted({(c.get(field) or "") for c in contracts})
    values = [v for v in values if v]  # drop empty
    assert values, f"no values found for field {field!r}"

    # Sample up to N options to keep the test runtime reasonable. The
    # parametrize covers each multiselect; within each, we walk every
    # value to guarantee none of them silently filters to zero.
    failures = []
    for v in values:
        _open_filter_dialog(page, column)
        _apply_multiselect(page, v)
        rows = _visible_rows(page)
        # Cross-check against the expected count from the data — catches
        # cases where the filter applies but maps to the wrong subset.
        expected = sum(1 for c in contracts if c.get(field) == v)
        if rows != expected:
            failures.append((v, rows, expected))
        _clear_all_filters(page)
    assert not failures, (
        f"{len(failures)}/{len(values)} options on {column} produced wrong row count. "
        f"First few: {failures[:3]} (option, got, expected)"
    )


def test_vendor_text_filter_returns_results(page: Page):
    """Pick a vendor from the data, type a substring of it into the
    Vendor text filter, assert ≥1 row matches."""
    contracts = json.loads(RESULTS.read_text())["contracts"]
    sample_vendor = next((c["vendor"] for c in contracts if c.get("vendor")), None)
    assert sample_vendor, "no vendor in dataset"
    needle = sample_vendor.split()[0][:6]  # first 6 chars of first word

    page.locator(".add-filter-btn").click()
    page.wait_for_selector(DYN_POPOVER)
    page.locator(f"{DYN_POPOVER} .filter-option:has-text('Vendor') input").click()
    page.wait_for_selector(f"{DYN_POPOVER} .filter-title:has-text('Filter:')")
    page.locator(f"{DYN_POPOVER} input[type='text']").fill(needle)
    page.locator(f"{DYN_POPOVER} .btn-apply").click()
    page.wait_for_selector(DYN_MODAL, state="detached")

    rows = _visible_rows(page)
    assert rows >= 1, f"vendor text filter on {needle!r} returned 0 rows"


def test_amount_range_filter_returns_results(page: Page):
    """Apply a generous Amount range that must include some contracts."""
    page.locator(".add-filter-btn").click()
    page.wait_for_selector(DYN_POPOVER)
    page.locator(f"{DYN_POPOVER} .filter-option:has-text('Amount') input").click()
    page.wait_for_selector(f"{DYN_POPOVER} .filter-title:has-text('Filter:')")
    page.locator(f"{DYN_POPOVER} .filter-range-min").fill("0")
    page.locator(f"{DYN_POPOVER} .filter-range-max").fill("999999")
    page.locator(f"{DYN_POPOVER} .btn-apply").click()
    page.wait_for_selector(DYN_MODAL, state="detached")

    rows = _visible_rows(page)
    assert rows >= 1, "amount range 0–$999B returned 0 rows"


def test_clear_all_resets_to_full_dataset(page: Page):
    """Apply a filter, then Clear All — table should restore to full
    contract count (or DataTables' page size, whichever is smaller)."""
    contracts = json.loads(RESULTS.read_text())["contracts"]

    # Get total count before any filter
    before = _visible_rows(page)
    assert before > 0

    # Apply a filter to one source value
    _open_filter_dialog(page, "Source")
    _apply_multiselect(page, "idv_expansion")
    after_filter = _visible_rows(page)
    assert after_filter < before, "filter didn't reduce row count"

    # Clear and verify reset
    _clear_all_filters(page)
    after_clear = _visible_rows(page)
    assert after_clear == before, (
        f"Clear All didn't restore full dataset: {before} before, "
        f"{after_clear} after clear (filter showed {after_filter})"
    )


def test_url_filter_round_trips(browser, fixture_server):
    """Loading the page with ?source=idv_expansion in the URL must:
       1. show the filter chip
       2. actually apply the filter to the table (not just paint chips)
    Catches the regression class where shared links look right but show
    every row anyway."""
    contracts = json.loads(RESULTS.read_text())["contracts"]
    expected = sum(1 for c in contracts if c.get("source") == "idv_expansion")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(fixture_server + "/index.html?source=idv_expansion")
    page.wait_for_selector("#contractTable tbody tr", timeout=15_000)
    page.wait_for_function(
        f"() => $('#contractTable').DataTable().rows().count() === {expected}",
        timeout=5_000,
    )
    chips = page.evaluate(
        "() => Array.from(document.querySelectorAll('.filter-chip-value')).map(e => e.textContent)"
    )
    assert any("idv" in c.lower() for c in chips), (
        f"URL-loaded filter set the chip wrong: {chips}"
    )
    ctx.close()


def test_url_round_trip_writes_back_after_apply(page: Page):
    """When a filter is applied via the UI, the URL must encode it so
    'Copy Link' produces a shareable URL."""
    _open_filter_dialog(page, "Source")
    _apply_multiselect(page, "modification_text")
    url = page.evaluate("() => window.location.search")
    assert "source=modification_text" in url, (
        f"After applying a multiselect filter, URL lacks the param: {url!r}"
    )


def test_multiselect_search_narrows_options(page: Page):
    """The keyword multiselect has 39 options. Typing in the in-modal
    search box must hide non-matching options. If this regresses, picking
    an option from a long list becomes painful."""
    _open_filter_dialog(page, "Keyword")
    # Count visible (non-hidden) options before search
    before = page.evaluate(f"""() => {{
        return Array.from(document.querySelectorAll('{DYN_POPOVER} .filter-option'))
            .filter(el => el.style.display !== 'none').length;
    }}""")
    assert before > 5, f"expected >5 keyword options, saw {before}"
    # Type a substring that matches only a few keywords
    page.locator(f"{DYN_POPOVER} .filter-options-search").fill("biometric")
    page.wait_for_function(f"""() => {{
        return Array.from(document.querySelectorAll('{DYN_POPOVER} .filter-option'))
            .filter(el => el.style.display !== 'none').length < {before};
    }}""", timeout=2_000)
    after = page.evaluate(f"""() => {{
        return Array.from(document.querySelectorAll('{DYN_POPOVER} .filter-option'))
            .filter(el => el.style.display !== 'none').length;
    }}""")
    assert 1 <= after < before, (
        f"keyword search 'biometric' should narrow {before} options to a "
        f"few, got {after}"
    )
    # Cancel the dialog to leave the page clean
    page.keyboard.press("Escape")
    page.wait_for_selector(DYN_MODAL, state="detached")


def test_loading_overlay_disappears_after_load(browser, fixture_server):
    """The loading overlay should be gone (or .hidden) within a couple
    seconds of loading the page. If it sticks, users see a wash of
    background color over the dashboard forever."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(fixture_server + "/index.html")
    page.wait_for_function(
        "() => !document.getElementById('loadingOverlay') || "
        "document.getElementById('loadingOverlay').classList.contains('hidden')",
        timeout=5_000,
    )
    ctx.close()


def test_zero_match_filter_shows_empty_state(page: Page):
    """A filter combination that matches no contracts must show the
    explicit empty-state block, not just a blank table."""
    # Pick a vendor substring guaranteed not to exist
    page.locator(".add-filter-btn").click()
    page.wait_for_selector(DYN_POPOVER)
    page.locator(f"{DYN_POPOVER} .filter-option:has-text('Vendor') input").click()
    page.wait_for_selector(f"{DYN_POPOVER} .filter-title:has-text('Filter:')")
    page.locator(f"{DYN_POPOVER} input[type='text']").fill("ZZZ_NO_VENDOR_MATCHES_THIS_XYZ")
    page.locator(f"{DYN_POPOVER} .btn-apply").click()
    page.wait_for_selector(DYN_MODAL, state="detached")
    page.wait_for_function(
        "() => document.getElementById('emptyState')?.style.display === 'block'",
        timeout=3_000,
    )
    table_visible = page.evaluate(
        "() => document.getElementById('tableWrap')?.style.display !== 'none'"
    )
    assert not table_visible, (
        "Empty-state visible AND table still showing — the empty path "
        "should swap the table out, not display both."
    )


def test_source_badges_link_to_raw_html(page: Page):
    """Every source badge in the table must wrap in an <a href=raw.html?...>
    so clicking jumps to the per-contract provenance view."""
    bad = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('#contractTable tbody tr').forEach(tr => {
            const badge = tr.querySelector('.source-badge');
            const anchor = badge ? badge.closest('a') : null;
            if (!anchor || !anchor.href.includes('raw.html?piid=')) {
                out.push(tr.querySelector('td')?.innerText || '?');
            }
        });
        return out;
    }""")
    assert not bad, f"{len(bad)} table rows have source badges not wrapped in raw.html link: {bad[:3]}"

# Testing

Three layers of checks, run in order of cheapness:

| Layer | Command | Runs in CI | What it catches |
|---|---|---|---|
| Static / shape | `pytest tests/ --ignore=tests/browser_filter_test.py` | Yes — `weekly.yml` | Missing fields in `results.json`, unknown classification sources, malformed URL patterns, missing `field_provenance` entries, missing modal/raw-HTML wiring |
| Browser (Playwright) | `pytest tests/browser_filter_test.py` | No (run locally before merge — or wire chromium into CI) | Filter UI bugs (every multiselect option yields >0 rows, URL round-trip, empty state, sticky headers), loading overlay regression |
| Live URLs | `python scripts/check_links.py --strict --sample 20` | Yes — `weekly.yml` | A `permalink` that 404s, a stale archive datestamp in `fetch_metadata.json`, a typo in `data_dictionary_url` |
| Human review | `web/review.html` | No (manual) | LLM classifications that the regex/structure can't catch — false positives on borderline contracts, miscategorized work |

## `pytest tests/` — what each file covers

**`tests/web_data_shape_test.py`** — assertions about the on-disk `web/data/results.json`:

- Top-level keys (`summary`, `contracts`, `methodology`, `field_provenance`, `keywords_searched`, `by_agency`).
- Every `contracts[]` row carries the keys raw.html and the dashboard read.
- `summary.from_*` counts reconcile with `len([c for c in contracts if c.source == ...])`.
- `summary.total_ai_dollars` reconciles with `sum(by_agency[].dollars)`.
- Every `award_id` is unique.
- Every `modification_text` row has `mod_text`; every `idv_expansion` row has `idv_parent` + `idv_siblings`.
- Every `methodology.*` field is populated; `fetch_metadata.fy_archives[].url` matches the bulk-archive ZIP pattern.
- Every contract field is registered in `field_provenance` with `source` ∈ `{usaspending_csv, derived, llm, internal}`.

**`tests/link_pattern_test.py`** — every URL the dashboard emits matches a regex catalog:

- `permalink` must be `https://www.usaspending.gov/award/CONT_(AWD|IDV)_…/` — catches the `gid` fallback to a bare PIID that silently 404s.
- `raw_url` must be `raw.html?piid=…`.
- Methodology URLs (`data_source_url`, `bulk_archive_url`, `data_dictionary_url`) match their patterns.
- No UEI-only vendor URLs (`sam.gov/entity/<uei>/general`, `usaspending.gov/recipient/<uei>`) — both reliably 404.
- Hardcoded URLs in `index.html` are in an explicit allow-list.

**`tests/raw_html_test.py`** — structural checks on `raw.html` and `index.html`:

- `raw.html` has a `provenanceFor()` branch for every value in `KNOWN_SOURCES`.
- `raw.html` reads every `methodology.*` field used by the page.
- `raw.html` reads `mod_text`, `idv_parent`, `idv_siblings`, and `field_provenance`.
- `raw.html` handles `?flag=summary`, `?flag=agency`, `?flag=source`.
- `index.html` wires every clickable surface (source-badge, modal "View raw data", stat-card drill-down, agency-bar onClick) to the right `raw.html?…` URL.
- `review.html` exists and reads `data/results.json`.

## `tests/browser_filter_test.py` — Playwright

Drives a real chromium browser against `web/index.html` to catch bugs the static tests can't see — the filter UI looks correct in code review but produces zero rows because of a stale DataTables cache, a stat-card link points at the wrong slug, etc.

Coverage:

- **Every multiselect option yields the right row count.** For `Sub-Agency`, `Source`, and `Keyword` filters, opens the dialog, applies each option, and asserts `DataTable.rows().count()` matches the expected count from `results.json`. This is the test that caught the `DataTable.destroy()` reorder bug — every filter chip applied but the table still showed all 166 rows.
- **Vendor text filter** returns >=1 row when given a substring of a known vendor.
- **Amount range filter** returns >=1 row when given an inclusive range.
- **Clear All** restores the full dataset.
- **URL round-trip:** loading `index.html?source=idv_expansion` shows the chip AND filters the table; applying via UI writes the param into the URL.
- **Multiselect in-modal search** narrows the option list when typing.
- **Loading overlay** disappears (or gains `.hidden`) within 5s of page load.
- **Zero-match empty state** swaps the table for the empty-state block when filters return 0 contracts.
- **Source badges** in every row are wrapped in an `<a href="raw.html?piid=...">`.

Run:

```bash
pytest tests/browser_filter_test.py -v          # ~18s
pytest tests/browser_filter_test.py -k url      # subset
```

Requirements:

```bash
pip install pytest-playwright
playwright install chromium
```

## `scripts/check_links.py`

```bash
python3 scripts/check_links.py                 # default: 20 random permalinks + every methodology URL
python3 scripts/check_links.py --sample 0      # all permalinks (~150 HEAD requests)
python3 scripts/check_links.py --strict        # exit 1 on any failure (CI mode)
python3 scripts/check_links.py --workers 16    # parallelism
```

What it does:

1. Loads `web/data/results.json`.
2. Collects URLs: every contract `permalink`, `methodology.data_source_url`, `methodology.data_dictionary_url`, `methodology.bulk_archive_url`, `methodology.fetch_metadata.fy_archives[].url`.
3. HEADs each URL (falls back to streaming GET when the host rejects HEAD).
4. Prints `✓` / `✗` per URL with status code, then a failure summary.

Use this when:

- A weekly run committed but USASpending rotated to a new archive datestamp (the `fetch_metadata` URLs go from 200 → 404). `weekly.yml` runs this with `--strict`, so the auto-commit is blocked when this happens; you'll see the failure in the GH Actions log.
- You changed `make_permalink()` in `build_web.py` and want to confirm every emitted URL still resolves.

## `web/review.html` — manual spot-check

A browser-only tool for walking every shipped contract and marking the LLM's verdict as `agree` / `disagree` / `skip`, with optional notes. Reviews are stored in `localStorage` and exportable as CSV. No network writes.

Use this when:

- You've changed the LLM model or prompt (`classify_ai.py`'s `MODEL` or `SYSTEM_PROMPT`) and want to spot-check the verdict shift.
- An external reviewer wants to audit the dataset before publication.
- You're investigating a single sub-agency's results and want to log per-row dispositions while you read.

Workflow: filter to a slice (source path or sub-agency), walk rows, mark + note, then click **Export CSV**. Drop the export into `data/classification_reviews/` and reference it in commit messages or PR descriptions.

## When to run what

| Situation | Static | Live | Human |
|---|---|---|---|
| Editing `build_web.py` field shape | ✓ | — | — |
| Editing `raw.html` / `index.html` | ✓ | — | — |
| Editing `fetch_dhs.py` archive logic | ✓ | ✓ | — |
| Editing `classify_ai.py` model or prompt | ✓ | — | ✓ (sample slice) |
| Pre-merge from a branch | ✓ | ✓ (`--sample 5`) | optional |
| Pre-publication / share with external reviewer | ✓ | ✓ (`--sample 0`) | ✓ (full pass) |
| Weekly cron (automatic) | ✓ | ✓ (`--sample 20`) | — |

## Failure modes worth remembering

- **Stale archive datestamp.** `fetch_dhs.py` falls back to a hardcoded datestamp when the archive index isn't reachable. If that fallback drifts behind USASpending's monthly rotation, every `fy_archives[].url` 404s. `scripts/check_links.py --strict` catches this; the fix is to update the fallback in `fetch_dhs.py` and re-run `fetch_dhs.py` (or just re-run, the auto-detect usually works).
- **`bulk_keyword_scan` placeholder leaking.** Old `dhs_ai_classified.csv` rows can store `bulk_keyword_scan` as the matched keyword. `build_web.py` re-derives the literal keyword from the description, falling back to a one-time scan of `data/dhs_contracts.csv` when the truncated description doesn't contain any keyword. If `data/dhs_contracts.csv` is missing on a fresh checkout, those rows ship the placeholder and raw.html's highlighter no-ops gracefully.
- **`generated_internal_id` empty.** If a contract has no `generated_internal_id`, `make_permalink()` falls back to the bare `award_id`, which 404s on USASpending. `tests/link_pattern_test.py::test_no_permalink_falls_back_to_bare_award_id` is the regression guard; the fix is upstream in whichever pipeline step produces the row.

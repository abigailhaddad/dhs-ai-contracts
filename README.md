# DHS AI Contracts

Open analysis of DHS contract spending on AI/ML work, FY2022–FY2026.

**Dashboard:** https://dhs-ai-contracts.vercel.app
**User-facing methodology:** the dashboard's Methodology section is the single source of truth — it documents what each variable is, where it comes from, what it joins to, and how AI classification works. This README is for developers running the pipeline.

## Pipeline

```
fetch_dhs.py          → data/dhs_contracts.csv         (USASpending bulk archive, FY22–FY26 DHS)
classify_ai.py        → data/dhs_ai_classified.csv     (keyword scan + LLM verdict)
enrich_contracts.py   → data/enriched.json             (modification-text re-classification + IDV sibling expansion)
build_web.py          → web/data/results.json          (dashboard payload)
run_pipeline.py       — orchestrates all of the above; the weekly GH Actions cron calls this.
```

Provenance flows through to the dashboard so every claim links back: `web/data/results.json.methodology` carries the LLM model + literal archive ZIPs ingested + per-field upstream column map (`field_provenance`); `web/raw.html?piid=…` renders all of it for one contract.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # add OPENAI_API_KEY (and CF_R2_* if you want R2 sync)

python3 run_pipeline.py
```

Subset runs:

```bash
python3 fetch_dhs.py --fy 2026                              # one fiscal year
python3 classify_ai.py --from-csv data/dhs_contracts.csv    # bulk-CSV path (no API throttling)
python3 classify_ai.py --dry-run                            # no LLM calls
python3 build_web.py                                        # rebuild dashboard JSON only
python3 run_pipeline.py --skip-fetch --skip-enrich          # cheapest: classify + rebuild
```

## Tests + link checks

```bash
pytest tests/                          # 32 tests: shape, URL patterns, raw-html structure
python3 scripts/check_links.py         # HEAD-checks emitted URLs (sample 20 permalinks by default)
python3 scripts/check_links.py --strict --sample 0   # CI mode: exit 1 on any failure, all permalinks
```

Both run automatically in `.github/workflows/weekly.yml` between the pipeline and the auto-commit, so a stale archive datestamp or a malformed URL blocks the dashboard update instead of silently shipping.

## Adding a new dashboard field

1. Add the field to whatever pipeline step populates it (`classify_ai.py`, `enrich_contracts.py`, etc.).
2. Forward it to the contract record in `build_web.py`.
3. **Register provenance** in `FIELD_PROVENANCE` at the top of `build_web.py` — `tests/web_data_shape_test.py::test_field_provenance_block_is_complete` will fail until you do, by design.
4. If the field is a URL, add a regex to `CONTRACT_URL_PATTERNS` in `tests/link_pattern_test.py`.

## Adding a new classification source

1. Implement the new path in `classify_ai.py` / `enrich_contracts.py`.
2. Add the source label (e.g. `rfp_text`) to `KNOWN_SOURCES` in `tests/web_data_shape_test.py` AND `EXPECTED_SOURCES` in `tests/raw_html_test.py`.
3. Add a render branch in `web/raw.html`'s `provenanceFor()` so users see meaningful provenance instead of "(unknown source)".
4. Add a row to the methodology table in `web/index.html` describing the new path.

## Files

```
run_pipeline.py            — top-level orchestrator
fetch_dhs.py               — bulk archive download (FY22–FY26 DHS, agency code 070)
classify_ai.py             — keyword scan + GPT-5.4-mini classification
enrich_contracts.py        — modification-text + IDV-sibling passes
build_web.py               — results.json builder + field_provenance map
explore_keywords.py        — keyword-coverage probe (no API key needed)

web/index.html             — dashboard (single-source methodology table)
web/raw.html               — per-contract / per-aggregate provenance viewer
web/data/results.json      — dashboard payload (committed)

scripts/check_links.py     — live URL checker
tests/                     — pytest suite (shape, URL patterns, raw-html structure)
.github/workflows/
  weekly.yml               — GH Actions: pipeline + tests + link check + auto-commit
```

## Data

All source data is public-domain from [USASpending.gov](https://www.usaspending.gov/). Classification uses the OpenAI API (gpt-5.4-mini).

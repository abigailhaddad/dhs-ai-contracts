# DHS AI Contracts

Open analysis of DHS contract spending on AI/ML work, FY2022–FY2026.

**Dashboard:** https://dhs-ai-contracts.vercel.app

**User-facing methodology:** the dashboard's Methodology tab is the single source of truth — it documents every variable, the upstream USASpending column it lives in, the join key, and how AI classification works. This README is intentionally short to avoid drift; it's for developers running the pipeline.

## What this is

Prime contract awards at the **Department of Homeland Security** (agency code 070) FY2022–FY2026, pulled from the [USASpending Award Data Archive](https://files.usaspending.gov/award_data_archive/) and classified for AI/ML scope by GPT-5.4-mini. Sub-agencies include CBP, ICE, TSA, USCIS, FEMA, Coast Guard, Secret Service, and others. Grants and IDV vehicle parents are excluded; child task orders under IDV vehicles are included. Output is a static HTML site (`web/index.html`) plus generated JSON (`web/data/results.json`).

## How it runs

GitHub Actions on `abigailhaddad/dhs-ai-contracts`:

| Workflow | Cadence | What it does |
|---|---|---|
| `weekly.yml` | Mondays 08:00 UTC | Run `run_pipeline.py` (fetch → classify → enrich → build), run pytest, live-check a sample of emitted URLs, then auto-commit `web/data/results.json` to `main`. The push triggers Vercel to rebuild the static site at `dhs-ai-contracts.vercel.app`. |

`workflow_dispatch` exposes two flags for manual reruns: `skip_fetch` (data already fresh) and `skip_enrich` (skip the LLM-heavy modification + IDV passes).

**State persistence between runs.** Pipeline state files (`classify_checkpoint.json`, `mod_processed.json`, `idv_processed.json`, `enriched.json`, `solicitation_ids.json`) sync to Cloudflare R2 at the start and end of every run (`run_pipeline.py:54-80`). The bulk DHS CSV (~250 MB, too big to commit) also lives in R2 and `fetch_dhs.py` pulls it down before scanning. That's how weekly runs stay cheap — the LLM only re-classifies new keyword hits, not the whole archive.

## Pipeline

`run_pipeline.py` runs six steps in order. Each step is idempotent and checkpoints, so interruptions resume cleanly:

```
1. fetch_dhs.py                          → data/dhs_contracts.csv     (USASpending bulk archive, FY22–FY26 DHS)
2. classify_ai.py                        → data/dhs_ai_classified.csv (USASpending API keyword search + LLM verdict)
3. classify_ai.py --from-csv             → (same)                     (bulk-CSV keyword scan; catches API cap misses)
4. enrich_contracts.py --modifications   → data/enriched.json         (re-classify "no" contracts using combined modification text)
5. enrich_contracts.py --idv             → data/enriched.json         (expand to sibling task orders under any IDV with a confirmed AI child)
6. build_web.py                          → web/data/results.json      (dashboard payload + field_provenance map)
```

Steps 2 and 3 hit the same classifier with different inputs: step 2 walks the USASpending API (capped at 10K rows per query, but lets us pull modification text), step 3 scans the full bulk CSV (no cap, no modification text). Together they catch what either pass alone would miss.

Provenance flows through to the dashboard so every claim links back: `web/data/results.json.methodology` carries the LLM model + literal archive ZIPs ingested + per-field upstream column map (`field_provenance`); `web/raw.html?piid=…` renders all of it for one contract.

## Where the raw source data lives

Every `raw.html` provenance block links back to the literal upstream file — same URLs the pipeline ingests, no re-hosting:

| Classification source path | Upstream input |
|---|---|
| `keyword search` | [USASpending Award Data Archive](https://files.usaspending.gov/award_data_archive/) — monthly per-FY DHS ZIPs (FY22–FY26). The exact ZIP filenames ingested are recorded in `methodology.fetch_metadata` and shown on `raw.html`. |
| `keyword search` (API path) | [USASpending Search API](https://api.usaspending.gov/api/v2/search/spending_by_award/) filtered to DHS (toptier code `070`) by FY. Catches modification-level descriptions the bulk archive collapses. |
| `modification text` | [USASpending modifications](https://api.usaspending.gov/) for each PIID, joined to a single combined text blob and re-fed to the LLM. |
| `idv expansion` | All sibling task orders under the same `parent_award_id_piid` as a confirmed AI child, pulled from the same bulk archive and LLM-classified. |

Pipeline state and the bulk CSV are mirrored to **Cloudflare R2** under `s3://<bucket>/dhs_contracts/` (`pipeline_state/` for checkpoints, `bulk/` for the CSV). R2 is private — only the GH Actions runner needs it.

## Run locally

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
pytest tests/                          # 47 tests: shape, URL patterns, raw-html structure, provenance integrity
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
run_pipeline.py            — top-level orchestrator (R2 state sync + 6-step pipeline)
fetch_dhs.py               — bulk archive download (FY22–FY26 DHS, agency code 070), R2-mirrored
classify_ai.py             — keyword scan + GPT-5.4-mini classification (API + bulk-CSV paths)
enrich_contracts.py        — modification-text + IDV-sibling passes
build_web.py               — results.json builder + field_provenance map
explore_keywords.py        — keyword-coverage probe (no API key needed)

web/index.html             — dashboard (Dashboard / Methodology tabs)
web/raw.html               — per-contract / per-aggregate provenance viewer
web/data/results.json      — dashboard payload (committed by weekly cron)

scripts/check_links.py     — live URL checker
tests/                     — pytest suite (47 tests)
.github/workflows/
  weekly.yml               — pipeline + tests + link check + auto-commit (Vercel rebuilds on push)
vercel.json                — static-site config (web/ output, no Python build)
```

## Data

All source data is public-domain from [USASpending.gov](https://www.usaspending.gov/). Classification uses the OpenAI API (gpt-5.4-mini).

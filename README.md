# DHS AI Contracts

An open analysis of AI and machine learning contract spending at the Department of Homeland Security, FY2022–FY2026.

**[View the dashboard](https://dhs-ai-contracts.vercel.app)** | Data from [USASpending.gov](https://www.usaspending.gov/)

## What this is

This project identifies DHS contracts that involve AI or machine learning work. It does this in two steps:

1. **Keyword search** — contract descriptions are searched against a broad list of AI-related terms (artificial intelligence, machine learning, LLM, computer vision, biometric, etc.)
2. **LLM classification** — each keyword match is reviewed by GPT-5.4-mini, which determines whether the contract is actually for AI/ML work and provides a one-sentence explanation

The result is a filtered, human-readable dataset of DHS AI contracts with dollar amounts, vendors, sub-agencies, and LLM-generated explanations.

## Pipeline

```
explore_keywords.py   — probe keyword hit counts via USASpending API (no auth needed)
classify_ai.py        — fetch candidates + LLM classify → data/dhs_ai_classified.csv
build_web.py          — generate web/data/results.json from classified data
fetch_dhs.py          — (optional) bulk download all DHS contracts from USASpending archives
```

### Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Explore keyword coverage (no API key needed)
python3 explore_keywords.py

# Classify AI contracts (needs OPENAI_API_KEY in .env)
python3 classify_ai.py

# Build dashboard data
python3 build_web.py
```

### Full bulk download (optional)

`fetch_dhs.py` downloads all DHS contracts from USASpending bulk archives (no NAICS filter).
Useful for catching AI contracts whose descriptions don't surface in the API keyword search.
Requires R2 credentials in `.env` for cloud storage.

```bash
python3 fetch_dhs.py          # FY2022–present
python3 classify_ai.py --from-csv data/dhs_contracts.csv
```

## Methodology

**Keywords searched:** 40 terms covering core AI/ML vocabulary (artificial intelligence, machine learning, deep learning, LLM, computer vision, facial recognition, etc.), automation-adjacent terms (robotic process automation, decision support), and DHS-specific signals (biometric, screening at speed). All terms are included in the keyword list regardless of whether they returned results — zero-hit terms demonstrate search coverage.

**LLM classification:** GPT-5.4-mini reviews each candidate contract with its description, vendor, NAICS/PSC codes, and sub-agency. The model is instructed to lean toward *true* for borderline cases. Contracts where the API call failed are excluded.

**Caveats:**
- Misses AI work embedded in larger IT contracts without explicit AI terminology in the description
- Dollar amounts are total obligated value and may include unexercised options
- Deduplication is by USASpending Award ID — the same base contract may appear across multiple fiscal years if re-awarded

## Files

```
explore_keywords.py        — USASpending API keyword probe
classify_ai.py             — LLM classification pipeline
build_web.py               — web data builder
fetch_dhs.py               — bulk archive downloader
web/index.html             — dashboard
web/data/results.json      — dashboard data (committed)
.github/workflows/
  fetch_dhs.yml            — GitHub Actions: bulk download
```

## Data

All source data is from [USASpending.gov](https://www.usaspending.gov/) (public domain).
Classification was performed using the OpenAI API.

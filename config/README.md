# Configuration — Amazon HealthSignals

This directory contains **all operational configuration** for HealthSignals. Adding new states, diseases, or data sources requires only editing/adding JSON files here — no code changes needed.

---

## Directory Structure

```
config/
├── system.json              # Global: S3 buckets, DynamoDB tables, Bedrock models, delivery
├── data_sources/
│   ├── delphi.json          # CMU Delphi Epidata API settings
│   ├── cdc_wastewater.json  # CDC NWSS Socrata API settings
│   └── cdc_nssp.json        # CDC NSSP ED Visit Socrata API settings
├── states/
│   ├── texas.json           # Texas: metros, counties, contacts, overrides
│   ├── _template.json       # ← Copy this to add a new state
│   └── (your_state.json)
├── diseases/
│   ├── influenza.json       # Flu: thresholds, signals, prompts
│   ├── rsv.json             # RSV: thresholds, signals, prompts
│   ├── covid.json           # COVID-19: thresholds, signals, prompts
│   ├── _template.json       # ← Copy this to add a new disease
│   └── (your_disease.json)
└── README.md                # This file
```

---

## How to Add a New State

1. **Copy the template:**
   ```bash
   cp config/states/_template.json config/states/florida.json
   ```

2. **Fill in required fields:**
   - `state_key`: lowercase identifier (e.g., `"florida"`)
   - `state_name`: full name (e.g., `"Florida"`)
   - `state_abbreviation`: 2-letter code (e.g., `"FL"`)
   - `cdc_geography_name`: must match CDC NSSP dataset exactly (e.g., `"Florida"`)
   - `sentinel_metros`: 2-4 major metros with MSA FIPS codes and county FIPS
   - `subscribing_counties`: rural counties that will receive alerts

3. **Set `enabled: true`** when ready to activate.

4. **Seed calibration data:**
   ```bash
   python scripts/seed_calibration_data.py --state florida
   ```

5. **Upload to S3** (or redeploy via CDK):
   ```bash
   aws s3 cp config/states/florida.json s3://${CONFIG_BUCKET}/config/states/florida.json
   ```

6. **No code changes needed.** The next Lambda invocation will pick up the new state automatically.

### Finding MSA FIPS Codes
- Census Bureau: https://www.census.gov/programs-surveys/metro-micro.html
- Download list: https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/

### Finding County FIPS Codes
- Census ANSI codes: https://www.census.gov/library/reference/code-lists/ansi.html

---

## How to Add a New Disease

1. **Copy the template:**
   ```bash
   cp config/diseases/_template.json config/diseases/mpox.json
   ```

2. **Fill in required fields:**
   - `disease_key`: lowercase identifier
   - `detection.threshold_pct_ed_visits`: what % of ED visits signals an outbreak
   - `data_sources.delphi`: Delphi signal name (check their docs for availability)
   - `data_sources.cdc_wastewater.socrata_dataset_id`: Socrata 4×4 ID from data.cdc.gov
   - `data_sources.cdc_nssp.pathogen_name`: exact name in NSSP dataset
   - `severity_classification`: define LOW/MODERATE/HIGH/CRITICAL thresholds
   - `prompt_hints`: guide the AI on what to recommend/avoid

3. **Set `enabled: true`** when ready.

4. **Seed calibration data** (requires ≥2 seasons of historical data):
   ```bash
   python scripts/seed_calibration_data.py --disease mpox
   ```

5. **Upload and done.** No code changes required.

### Prerequisites for Adding a Disease
- The disease must follow **geographic propagation** (metro → rural pattern)
- At least one data source must provide MSA-level or state-level data
- You need ≥2 historical seasons for calibration (otherwise predictions use defaults)
- Diseases that DON'T fit this model: foodborne, sexually transmitted, vector-borne

---

## How to Add a New Data Source

This is the **one scenario that requires code**:

1. Create `config/data_sources/your_source.json` with API settings
2. Write a new Lambda fetcher at `lambdas/ingestion/your_source_fetcher/handler.py`
3. Add CDK resources in `cdk/stacks/ingestion_stack.py`
4. Reference the new source in disease configs under `data_sources.your_source`

The existing fetcher pattern is designed to be copied. See `delphi_fetcher/handler.py` as the simplest example.

---

## How Config Loading Works

```
Lambda cold start
    ↓
Check CONFIG_BUCKET env var
    ↓
┌─── CONFIG_BUCKET set? ───┐
│ YES                       │ NO
│ Load from S3              │ Load from local config/ directory
│ s3://{bucket}/{prefix}/   │ (for pytest and local development)
└───────────────────────────┘
    ↓
Cache in memory (reused across warm invocations)
    ↓
Validate required fields
    ↓
Return config dict
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CONFIG_BUCKET` | No | `""` (use local) | S3 bucket with config files |
| `CONFIG_PREFIX` | No | `"config/"` | S3 key prefix |
| `CONFIG_LOCAL_PATH` | No | `../../config` | Local filesystem path (dev/test) |

---

## Schema Reference

### State Config (required fields)

| Field | Type | Description |
|-------|------|-------------|
| `state_key` | string | Lowercase identifier |
| `state_name` | string | Full state name |
| `state_abbreviation` | string | 2-letter code |
| `cdc_geography_name` | string | Exact match for CDC NSSP dataset |
| `enabled` | boolean | Whether to monitor this state |
| `sentinel_metros` | object | MSA FIPS → metro details |
| `subscribing_counties` | array | Counties receiving alerts |

### Disease Config (required fields)

| Field | Type | Description |
|-------|------|-------------|
| `disease_key` | string | Lowercase identifier |
| `display_name` | string | Human-readable name |
| `enabled` | boolean | Whether to monitor this disease |
| `detection.threshold_pct_ed_visits` | float | % ED visits to trigger detection |
| `detection.require_rising_trend` | boolean | Must trend be rising? |
| `data_sources` | object | Signal IDs per data source |
| `severity_classification` | object | LOW/MODERATE/HIGH/CRITICAL definitions |

---

## Validation

Configs are validated at load time. Missing required fields produce clear error messages:

```
ConfigLoadError: State config 'florida' missing required fields: ['sentinel_metros']
```

To test config validity locally:
```bash
python -c "
import sys; sys.path.insert(0, 'lambdas')
from shared.config_loader import list_active_states, list_active_diseases
print('States:', [s['state_key'] for s in list_active_states()])
print('Diseases:', [d['disease_key'] for d in list_active_diseases()])
"
```

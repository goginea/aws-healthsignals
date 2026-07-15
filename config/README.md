# Configuration — Amazon HealthSignals

This directory contains all operational configuration for HealthSignals. Adding new states, diseases, or data sources requires only editing/adding JSON files here — no code changes needed.

---

## Directory Structure

```
config/
├── system.json                  # Global: S3 buckets, DynamoDB tables, Bedrock models, delivery
├── alert_categories.json        # Shared: subscriber opt-in categories for plugin modules
├── disease_thresholds.json      # Detection threshold overrides
├── metros.json                  # Metro area reference data
├── subscription_settings.json   # Subscription limits and defaults
├── counties_sample.json         # Sample county data
│
├── data_sources/
│   ├── delphi.json              # CMU Delphi Epidata API settings
│   ├── cdc_wastewater.json      # CDC NWSS Socrata API settings
│   ├── cdc_nssp.json            # CDC NSSP ED Visit API settings
│   └── openfda_shortages.json   # [Plugin] openFDA Drug Shortages API
│
├── states/
│   ├── texas.json               # Texas: metros, counties, contacts, overrides
│   ├── _template.json           # Copy this to add a new state
│   └── ...
│
├── diseases/
│   ├── influenza.json           # Flu: thresholds, signals
│   ├── rsv.json                 # RSV: thresholds, signals
│   ├── covid.json               # COVID-19: thresholds, signals
│   ├── _template.json           # Copy this to add a new disease
│   └── ...
│
└── shortage_monitoring/         # [Plugin] Drug Shortage module config
    ├── therapeutic_categories.json   # Monitored drug categories + disease mappings
    └── _template_therapeutic_category.json  # Template for new categories
```

---

## Core Config Files

### system.json

Global system configuration: DynamoDB table names, S3 bucket patterns, Bedrock model IDs, delivery settings (SES sender, SMS limits). Used by all Lambdas.

### alert_categories.json

Defines categories available for subscriber opt-in. Plugin modules register their categories here. Used by the `update_preferences` Lambda to validate subscriber category selections. When no plugins are enabled, this file can be empty (`{"categories": []}`).

### data_sources/

API endpoint configuration for each data source. The `config_loader` reads these to determine URLs, rate limits, pagination settings, and retry behavior.

### states/

One file per monitored state. Contains sentinel metro definitions, subscribing counties, contacts, and optional disease-specific threshold overrides.

### diseases/

One file per monitored disease. Contains detection thresholds, data source signal mappings, and severity classification rules.

---

## Plugin Config Files

### shortage_monitoring/ (Drug Shortage Module)

Only used when `enable_drug_shortage: true` in `cdk/cdk.json`.

- `therapeutic_categories.json` — Defines which drug categories to monitor, their priority levels, disease relationships, and FDA product name matching patterns
- `_template_therapeutic_category.json` — Template for adding new categories

---

## How to Add a New State

```bash
cp config/states/_template.json config/states/florida.json
# Edit with sentinel metros and subscribing counties
aws s3 cp config/states/florida.json s3://${CONFIG_BUCKET}/config/states/florida.json
python scripts/seed_calibration_data.py --state florida --seasons 3
```

No code changes needed.

---

## How to Add a New Disease

```bash
cp config/diseases/_template.json config/diseases/mpox.json
# Edit with thresholds, data source signals, severity rules
aws s3 cp config/diseases/mpox.json s3://${CONFIG_BUCKET}/config/diseases/mpox.json
```

Prerequisite: the disease must follow geographic propagation (metro-to-rural) and have at least one MSA-level data source.

---

## How to Add a New Data Source

This requires code: write a new fetcher Lambda at `lambdas/ingestion/your_source_fetcher/handler.py` and add CDK resources. The config file at `config/data_sources/your_source.json` defines the API connection parameters. See `delphi_fetcher/handler.py` as a reference implementation.

---

## Config Loading

Configs are loaded by `lambdas/shared/config_loader.py`:

- **Production**: reads from S3 using `CONFIG_BUCKET` and `CONFIG_PREFIX` env vars
- **Development/test**: reads from local `config/` directory when `CONFIG_BUCKET` is empty
- **Caching**: configs are cached in Lambda memory after first load; force refresh with `{"_refresh_config": true}`

---

## Validation

Configs are validated at load time. Test locally:

```bash
python -c "
import sys; sys.path.insert(0, 'lambdas')
from shared.config_loader import list_active_states, list_active_diseases
print('States:', [s['state_key'] for s in list_active_states()])
print('Diseases:', [d['disease_key'] for d in list_active_diseases()])
"
```

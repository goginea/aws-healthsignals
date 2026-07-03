# Configuration Guide — Amazon HealthSignals

## Overview

HealthSignals is **fully config-driven**. All operational parameters — which states to monitor, which diseases to track, detection thresholds, data source endpoints, delivery preferences — live in JSON config files under `config/`.

**Key principle:** Adding a new state or disease NEVER requires code changes. You only modify or add config files.

---

## Quick Reference

| I want to... | Action |
|---|---|
| Add a new state | Copy `config/states/_template.json` → fill in → set `enabled: true` |
| Add a new disease | Copy `config/diseases/_template.json` → fill in → set `enabled: true` |
| Change a detection threshold | Edit `config/diseases/{disease}.json` → `detection.threshold_pct_ed_visits` |
| Add a county to monitoring | Edit `config/states/{state}.json` → add to `subscribing_counties` array |
| Override threshold for one state | Edit `config/states/{state}.json` → `disease_overrides.{disease}.threshold_override` |
| Change Bedrock model | Edit `config/system.json` → `bedrock.routine_model_id` |
| Change Bedrock models | Edit `config/system.json` → update `routine_model_id` and `high_severity_model_id`, then redeploy |
| Add a new data source | Create `config/data_sources/{name}.json` + write a new fetcher Lambda |
| Disable a state temporarily | Set `enabled: false` in that state's config |
| Change alert delivery channel | Edit county's `delivery_preferences.channels` in state config |
| Adjust quiet hours | Edit county's `delivery_preferences.quiet_hours` in state config |

---

## Config Architecture

```
                    ┌─────────────────────────────────────┐
                    │         config/system.json           │
                    │  (Global: tables, models, delivery)  │
                    └─────────────┬───────────────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
          ▼                       ▼                       ▼
┌─────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  config/states/ │   │ config/diseases/ │   │config/data_sources│
│                 │   │                  │   │                   │
│ texas.json      │   │ influenza.json   │   │ delphi.json       │
│ florida.json    │   │ rsv.json         │   │ cdc_wastewater.json│
│ ohio.json       │   │ covid.json       │   │ cdc_nssp.json     │
│ ...             │   │ mpox.json        │   │ ...               │
└─────────────────┘   └──────────────────┘   └──────────────────┘
        │                       │                       │
        │    ┌──────────────────┼───────────────────┐   │
        │    │                  │                   │   │
        ▼    ▼                  ▼                   ▼   ▼
┌─────────────────────────────────────────────────────────────┐
│              lambdas/shared/config_loader.py                  │
│                                                              │
│  get_system_config()    list_active_states()                │
│  get_state_config()     list_active_diseases()              │
│  get_disease_config()   get_detection_threshold()           │
│  get_data_source_config()  get_subscribing_counties()       │
└─────────────────────────────────────────────────────────────┘
        │
        ▼ (used by all Lambda handlers)
┌──────────────────────────────────────────────────────────────┐
│  delphi_fetcher  │  cdc_wastewater  │  cdc_respiratory       │
│  leader_detection│  geographic_affinity│  timing_estimation   │
│  alert_dispatcher│  feedback_collector │                      │
└──────────────────────────────────────────────────────────────┘
```

---

## Deployment Modes

### Production (S3-backed)
```
CONFIG_BUCKET=healthsignals-config-123456789012-us-east-1
CONFIG_PREFIX=config/
```
Configs are loaded from S3 at Lambda cold start. Cache persists for warm invocations.

### Local Development
```
CONFIG_BUCKET=        (empty or unset)
CONFIG_LOCAL_PATH=./config
```
Configs are loaded from local filesystem. No AWS credentials needed.

### Testing
```python
# In pytest, the config loader auto-detects local config/ directory
from shared.config_loader import get_system_config, list_active_states
```

---

## Adding a Complete New State (Example: Florida)

### Step 1: Create the config file

```bash
cp config/states/_template.json config/states/florida.json
```

### Step 2: Research sentinel metros

For Florida, good sentinels would be:
- Miami-Fort Lauderdale (MSA 33100) — largest, international gateway
- Orlando (MSA 36740) — central Florida hub
- Tampa-St. Petersburg (MSA 45300) — Gulf coast
- Jacksonville (MSA 27260) — northeast Florida

### Step 3: Fill in the config

```json
{
  "state_key": "florida",
  "state_name": "Florida",
  "state_abbreviation": "FL",
  "cdc_geography_name": "Florida",
  "enabled": true,
  "sentinel_metros": {
    "33100": {
      "name": "Miami-Fort Lauderdale-Pompano Beach",
      "short_name": "Miami",
      "msa_fips": "33100",
      "county_fips": ["12086", "12011", "12099"],
      "county_names": ["Miami-Dade", "Broward", "Palm Beach"]
    }
    // ... more metros
  },
  "subscribing_counties": [
    // ... rural counties
  ]
}
```

### Step 4: Seed calibration data

```bash
python scripts/seed_calibration_data.py --state florida --seasons 3
```

### Step 5: Deploy

```bash
aws s3 cp config/states/florida.json s3://${CONFIG_BUCKET}/config/states/florida.json
```

Next Lambda execution will automatically include Florida in all ingestion, prediction, and alerting.

---

## Config Caching & Refresh

- Configs are cached in Lambda memory after first load (cold start)
- Warm invocations reuse cached configs (~100ms faster)
- To force refresh after S3 update: invoke Lambda with `{"_refresh_config": true}`
- Cache auto-expires if Lambda instance is recycled (~15-45 minutes of inactivity)

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "ConfigLoadError: missing required fields" | Incomplete config JSON | Check fields against `_template.json` |
| New state not appearing | `enabled: false` in config | Set to `true` |
| Alerts not sent to new county | Missing from `subscribing_counties` | Add county entry with contacts |
| Threshold not working | State override conflicting | Check `disease_overrides` in state config |
| Delphi signal not found | Wrong signal name | Check Delphi docs for available signals |
| Wastewater data empty | Wrong Socrata dataset ID | Verify 4×4 ID at data.cdc.gov |

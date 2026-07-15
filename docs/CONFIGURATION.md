# Configuration Guide — Amazon HealthSignals

## Overview

HealthSignals is **config-driven**. All operational parameters — states, diseases, thresholds, data sources, delivery preferences — live in JSON config files under `config/`. Adding a new state or disease requires no code changes.

---

## Quick Reference

| I want to...                     | Action                                                                               |
| -------------------------------- | ------------------------------------------------------------------------------------ |
| Add a new state                  | Copy `config/states/_template.json`, fill in, set `enabled: true`                    |
| Add a new disease                | Copy `config/diseases/_template.json`, fill in, set `enabled: true`                  |
| Change a detection threshold     | Edit `config/diseases/{disease}.json` > `detection.threshold_pct_ed_visits`          |
| Add a county to monitoring       | Edit `config/states/{state}.json` > add to `subscribing_counties`                    |
| Override threshold for one state | Edit `config/states/{state}.json` > `disease_overrides.{disease}.threshold_override` |
| Change Bedrock model             | Edit `config/system.json` > `bedrock.routine_model_id`, then redeploy                |
| Add a new data source            | Create `config/data_sources/{name}.json` + write a new fetcher Lambda                |
| Disable a state temporarily      | Set `enabled: false` in that state's config                                          |
| Enable/disable a plugin module   | Edit `cdk/cdk.json` > set `enable_{module_name}` context flag                        |
| Add alert categories for plugins | Edit `config/alert_categories.json`                                                  |

---

## Config Architecture

```
config/
├── system.json                    # Global: tables, models, delivery settings
├── alert_categories.json          # Shared: plugin alert categories for subscriber opt-in
├── data_sources/
│   ├── delphi.json                # CMU Delphi Epidata API settings
│   ├── cdc_wastewater.json        # CDC NWSS Socrata API settings
│   ├── cdc_nssp.json              # CDC NSSP ED Visit API settings
│   └── openfda_shortages.json     # [Plugin] openFDA Drug Shortages API
├── states/
│   ├── texas.json                 # Texas: metros, counties, contacts, overrides
│   ├── _template.json             # Copy this to add a new state
│   └── ...
├── diseases/
│   ├── influenza.json             # Flu: thresholds, signals
│   ├── rsv.json                   # RSV: thresholds, signals
│   ├── covid.json                 # COVID-19: thresholds, signals
│   ├── _template.json             # Copy this to add a new disease
│   └── ...
└── shortage_monitoring/           # [Plugin] Drug Shortage module config
    ├── therapeutic_categories.json
    └── _template_therapeutic_category.json
```

All configs are loaded by `lambdas/shared/config_loader.py`, which reads from S3 in production and local filesystem during development/testing.

---

## Plugin Module Configuration

Plugin modules (e.g., Drug Shortage Intelligence) are controlled via feature flags in `cdk/cdk.json`:

```json
{
  "context": {
    "enable_drug_shortage": true
  }
}
```

When a plugin is enabled:

- Its CDK stack is deployed with dedicated resources (Lambdas, DynamoDB, Step Functions)
- Its dispatch plugin is loaded by the alert dispatcher via `DISPATCH_PLUGINS` env var
- Its GSIs are added to the subscriptions table
- Its config files in `config/` are read by its own Lambdas

When disabled, the core system has no awareness of the plugin.

---

## Alert Categories (config/alert_categories.json)

Defines categories that subscribers can opt into for plugin module alerts. Each plugin registers its categories here.

```json
{
  "categories": [
    {
      "category_key": "antivirals",
      "display_name": "Antivirals",
      "module": "drug_shortage",
      "relevant_diseases": ["influenza", "covid"],
      "priority_level": "HIGH"
    }
  ]
}
```

Subscribers opt in via the update_preferences API with `alert_categories: ["antivirals"]`.

---

## Deployment Modes

| Mode       | Config Source                    | Use Case              |
| ---------- | -------------------------------- | --------------------- |
| Production | S3 (`CONFIG_BUCKET` env var)     | Deployed Lambdas      |
| Local dev  | Filesystem (`config/` directory) | pytest, local testing |

---

## Adding a New State (Example: Florida)

1. Copy template: `cp config/states/_template.json config/states/florida.json`
2. Fill in `state_key`, `state_name`, `sentinel_metros`, `subscribing_counties`
3. Set `enabled: true`
4. Seed calibration: `python scripts/seed_calibration_data.py --state florida --seasons 3`
5. Upload: `aws s3 cp config/states/florida.json s3://${CONFIG_BUCKET}/config/states/florida.json`

No code changes needed. The next Lambda execution picks up the new state automatically.

---

## Adding a New Disease

1. Copy template: `cp config/diseases/_template.json config/diseases/mpox.json`
2. Fill in `disease_key`, `detection.threshold_pct_ed_visits`, `data_sources`, `severity_classification`
3. Set `enabled: true`
4. Seed calibration (requires 2+ historical seasons)
5. Upload to S3

Prerequisites: the disease must follow geographic propagation (metro-to-rural pattern) and at least one data source must provide MSA-level data.

---

## Config Caching

- Configs are cached in Lambda memory after first load (cold start)
- Warm invocations reuse cached configs
- To force refresh: invoke Lambda with `{"_refresh_config": true}`
- Cache auto-expires when Lambda instance is recycled (~15-45 minutes of inactivity)

---

## Troubleshooting

| Symptom                                    | Cause                               | Fix                            |
| ------------------------------------------ | ----------------------------------- | ------------------------------ |
| `ConfigLoadError: missing required fields` | Incomplete JSON                     | Check against `_template.json` |
| New state not appearing                    | `enabled: false`                    | Set to `true`                  |
| Alerts not sent to new county              | Missing from `subscribing_counties` | Add county entry               |
| Config not refreshing                      | Warm instance cache                 | Force cold start or wait       |

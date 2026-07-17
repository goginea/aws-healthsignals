# Forecast Provider Plugin — Configuration Guide

## Overview

The Forecast Provider Plugin extends HealthSignals with external forecast data from CDC FluSight, RSV Hub, and custom user models. It enriches the core prediction pipeline with validated ensemble forecasts without replacing the internal historic-lookup engine.

**Key capabilities:**

1. **CDC FluSight Integration** — Weekly influenza hospitalization forecasts from 30+ models
2. **RSV Hub Integration** — Weekly RSV hospitalization forecasts from ensemble models
3. **Custom Model Support** — Register your own forecasting API via config
4. **Multi-Source Aggregation** — Weighted mean with conflict detection across providers
5. **Core Enrichment** — Bedrock situation briefs include external forecast context when available

The core pipeline continues to function identically without this plugin. When enabled, the timing_estimation Lambda optionally reads external forecasts to enrich alerts.

---

## Architecture

```
EventBridge (weekly schedule)
         │
    ┌────┴────┐
    ▼         ▼
FluSight   RSV Hub        Custom Model
Fetcher    Fetcher         Fetcher
    │         │               │
    └────┬────┘               │
         ▼                    ▼
    DynamoDB: healthsignals-forecast-state
         │
         ▼
  Forecast Aggregator
         │
         ├── Weighted mean + conflict detection
         ├── Write aggregated result to DynamoDB
         └── Emit EventBridge: healthsignals.forecast.updated
         
Core Pipeline (timing_estimation):
         │
         ├── Reads aggregated forecast from DynamoDB (if FORECAST_STATE_TABLE env var set)
         ├── Passes external_forecast to Step Functions
         └── Bedrock includes EXTERNAL FORECAST CONTEXT in situation brief
```

---

## Enabling the Module

In `cdk/cdk.json`:

```json
{
  "context": {
    "enable_forecast_providers": true
  }
}
```

Then deploy:

```bash
cdk deploy --all
```

This creates the `HealthSignals-ForecastProviders` stack and adds `FORECAST_STATE_TABLE` env var to the timing_estimation Lambda.

---

## Configuration Files

| File | Purpose |
|------|---------|
| `config/forecast_providers/_schema.json` | Field definitions and Standard Forecast Contract |
| `config/forecast_providers/cdc_flusight.json` | FluSight provider config |
| `config/forecast_providers/cdc_rsv_hub.json` | RSV Hub provider config |
| `config/forecast_providers/{custom}.json` | User-created custom providers |
| `config/system.json` > `forecast_providers` | Global settings (aggregation method, conflict threshold) |

---

## Adding a Custom Model Provider

Create `config/forecast_providers/your_model.json`:

```json
{
  "provider_name": "tx_dshs_seir_model",
  "display_name": "Texas DSHS Internal SEIR Forecast",
  "enabled": true,
  "source_type": "api_endpoint",
  "endpoint_url": "https://models.dshs.texas.gov/api/v1/forecast",
  "auth_type": "api_key",
  "auth_secret_arn": "arn:aws:secretsmanager:us-east-1:123:secret:dshs-api-key",
  "diseases": ["influenza", "rsv"],
  "poll_schedule": "weekly",
  "trust_weight": 0.8,
  "timeout_seconds": 30,
  "fallback_on_error": true
}
```

Upload to S3 and the custom model fetcher will invoke it on the next scheduled run.

### Authentication Options

| Method | Config Value | Behavior |
|--------|-------------|----------|
| None | `"auth_type": "none"` | No auth header |
| API Key | `"auth_type": "api_key"` | X-API-Key header from Secrets Manager |
| Bearer Token | `"auth_type": "bearer"` | Authorization: Bearer from Secrets Manager |

### Custom Model API Contract

Your endpoint receives a POST request:

```json
{
  "request_type": "forecast",
  "disease": "influenza",
  "geo_level": "state",
  "geo_value": "TX",
  "request_date": "2026-11-15",
  "horizons_requested": [1, 2, 3, 4],
  "current_signals": {
    "ed_visit_pct": 3.2,
    "ed_visit_trend": "rising"
  }
}
```

And must return the Standard Forecast Contract (see below).

---

## Standard Forecast Contract

All providers must output this JSON format:

```json
{
  "provider": "cdc_flusight",
  "disease": "influenza",
  "geo_level": "state",
  "geo_value": "TX",
  "forecast_date": "2026-11-15",
  "target": "hospitalizations",
  "predictions": [
    {
      "horizon_weeks": 1,
      "point_estimate": 1200,
      "quantiles": {"0.025": 800, "0.25": 1000, "0.75": 1400, "0.975": 1800}
    },
    {
      "horizon_weeks": 2,
      "point_estimate": 1500
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | Yes | Identifier for attribution |
| `disease` | Yes | Disease key |
| `geo_level` | Yes | "state" or "national" |
| `geo_value` | Yes | State abbreviation or "US" |
| `forecast_date` | Yes | When forecast was generated |
| `target` | Yes | "hospitalizations", "ed_visits", or "cases" |
| `predictions[]` | Yes | Array of horizon forecasts |
| `predictions[].horizon_weeks` | Yes | Weeks ahead (1-4) |
| `predictions[].point_estimate` | Yes | Central value |
| `predictions[].quantiles` | No | Uncertainty intervals |

---

## CDK Resources (HealthSignals-ForecastProviders Stack)

| Resource | Type | Purpose |
|----------|------|---------|
| `healthsignals-forecast-state` | DynamoDB | Forecast storage (PK: geo_key, SK: disease_week, TTL 8 weeks) |
| `healthsignals-flusight-forecast-fetcher` | Lambda | Weekly FluSight CSV fetch from GitHub |
| `healthsignals-rsv-hub-forecast-fetcher` | Lambda | Weekly RSV Hub parquet fetch from GitHub |
| `healthsignals-custom-model-fetcher` | Lambda | On-demand custom API calls |
| `healthsignals-forecast-aggregator` | Lambda | Multi-source weighted aggregation |
| 3 EventBridge rules | Schedule | Wednesday 10/11/12 AM UTC (staggered) |
| 2 CloudWatch alarms | Monitoring | Fetcher errors, no data in 14 days |
| CloudWatch dashboard | Monitoring | Forecasts written, conflicts detected |

---

## Aggregation Logic

For each disease + state + week:
1. Collect all provider forecasts from DynamoDB
2. Compute weighted mean of point estimates (using `trust_weight`)
3. Blend quantiles via weighted average per level
4. Detect conflicts: providers disagree by >50% on magnitude
5. Write aggregated result with `provider: "_aggregated"`
6. Emit EventBridge event

### Trust Weights

| Provider | Weight | Rationale |
|----------|--------|-----------|
| CDC FluSight Ensemble | 1.0 | 30+ models, 7+ seasons validated |
| CDC RSV Hub | 1.0 | Same rigor |
| Internal historic calibration | 1.0 (county timing) / 0.6 (state magnitude) | Unique county-level spatial resolution |
| User custom model | 0.7 (configurable) | Per-provider in config |

### Conflict Detection

| Scenario | Action |
|----------|--------|
| Providers agree (within 50%) | Report consensus |
| Disagree on magnitude (>50%) | Flag conflict, Bedrock reports range |
| One provider missing | Skip, weight others proportionally |
| All external unavailable | Core historic lookup only |

---

## Core Integration

When the plugin is enabled:
- `timing_estimation` Lambda reads `FORECAST_STATE_TABLE` env var
- Queries DynamoDB for the aggregated forecast matching state + disease + week
- Returns `external_forecast` field in its response
- Pipeline coordinator forwards it to Step Functions
- Bedrock SituationBrief prompt includes external forecast context if present

When disabled or no data available:
- `external_forecast` is `null` throughout
- Bedrock omits the external forecast section
- Core generates alerts using historic lookup alone

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| No external forecast in alerts | Plugin not deployed or no data in DDB | Check FORECAST_STATE_TABLE env var on timing_estimation Lambda |
| FluSight fetch fails | GitHub CSV URL changed or rate limited | Check CloudWatch logs, verify latest file pattern |
| RSV Hub fetch fails | Parquet format changed or pandas missing | Verify Lambda has pandas/pyarrow in layer |
| Custom model timeout | Provider API slow | Increase timeout_seconds in provider config |
| Conflict detected every week | Providers fundamentally disagree | Review trust_weight settings, consider disabling one |
| Aggregator finds no data | Fetchers haven't run yet | Trigger fetchers manually or wait for next Wednesday |

---

## Data Sources

| Source | Repository | Update | Format | License |
|--------|-----------|--------|--------|---------|
| CDC FluSight | cdcepi/FluSight-forecast-hub | Weekly (Wed) | CSV | MIT |
| US RSV Hub | HopkinsIDD/rsv-forecast-hub | Weekly | Parquet | MIT |

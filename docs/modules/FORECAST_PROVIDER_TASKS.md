# Forecast Provider Plugin — Implementation Task List

**Module name:** `forecast_provider`
**Feature flag:** `enable_forecast_providers`
**Branch:** `feature/forecast-provider`
**Estimated effort:** 4-5 days
**Design doc:** Forecast Provider Plugin Design (July 2026)

---

## Prerequisites

- Core HealthSignals deployed with all 7 stacks operational
- Bedrock model access enabled (Claude Sonnet 4.5)
- `shared/geo_utils.py` available for state normalization (already in place)

---

## Task Group 1: Configuration Files

- [ ] **1.1** Create `config/forecast_providers/_schema.json` — JSON Schema defining the provider config structure (for validation)
- [ ] **1.2** Create `config/forecast_providers/cdc_flusight.json` — Built-in provider config: GitHub CSV source, cdcepi/FluSight-forecast-hub, weekly poll, trust_weight 1.0, diseases: [influenza]
- [ ] **1.3** Create `config/forecast_providers/cdc_rsv_hub.json` — Built-in provider config: GitHub CSV source, HopkinsIDD/rsv-forecast-hub, weekly poll, trust_weight 1.0, diseases: [rsv]
- [ ] **1.4** Add `forecast_providers` section to `config/system.json` — enabled flag, aggregation_method, conflict_threshold_pct, forecast_state_table name, max_providers

---

## Task Group 2: DynamoDB Forecast State Table

- [ ] **2.1** Define DynamoDB table `healthsignals-forecast-state` in CDK stack
  - PK: `geo_key` (String) — normalized state key (e.g., "texas")
  - SK: `disease_week` (String) — e.g., "influenza_2026-W45"
  - Attributes: provider, point_estimate, quantiles, forecast_date, metadata, aggregated_result, ttl
  - TTL attribute for auto-cleanup of stale forecasts (8 weeks)
  - On-demand billing, RemovalPolicy.RETAIN

---

## Task Group 3: FluSight Ingestion Lambda

- [ ] **3.1** Create `lambdas/ingestion/flusight_forecast_fetcher/handler.py`
  - Fetch latest ensemble CSV from GitHub (cdcepi/FluSight-forecast-hub)
  - Parse quantile predictions from CSV format
  - Normalize to Standard Forecast Contract JSON
  - Use `shared/geo_utils.normalize_state_name()` to convert state abbreviations (TX → texas)
  - Write to DynamoDB forecast-state table
  - Store raw CSV to S3: `raw/forecasts/flusight/{date}/ensemble.csv`
  - Emit CloudWatch metrics (records fetched, errors)
- [ ] **3.2** Create `lambdas/ingestion/flusight_forecast_fetcher/__init__.py`

---

## Task Group 4: RSV Hub Ingestion Lambda

- [ ] **4.1** Create `lambdas/ingestion/rsv_hub_forecast_fetcher/handler.py`
  - Same pattern as FluSight but for HopkinsIDD/rsv-forecast-hub
  - Parse RSV-specific ensemble CSV
  - Normalize to Standard Forecast Contract
  - Use `shared/geo_utils` for state normalization
  - Write to DynamoDB + S3
- [ ] **4.2** Create `lambdas/ingestion/rsv_hub_forecast_fetcher/__init__.py`

---

## Task Group 5: Custom Model Fetcher Lambda

- [ ] **5.1** Create `lambdas/ingestion/custom_model_fetcher/handler.py`
  - Read provider config from S3 (`config/forecast_providers/{provider}.json`)
  - Build request body per Section 4.2 of design doc (include current_signals)
  - Call external API endpoint via HTTP POST
  - Support auth options: none, api_key (X-API-Key header), bearer (Authorization header), iam_sigv4
  - Read auth credentials from Secrets Manager when needed
  - Validate response against Standard Forecast Contract
  - Normalize geo_value using `shared/geo_utils`
  - Write to DynamoDB + S3
  - Error handling: timeout → skip, invalid response → skip, HTTP error → retry once then skip
- [ ] **5.2** Create `lambdas/ingestion/custom_model_fetcher/__init__.py`

---

## Task Group 6: Forecast Aggregator Lambda

- [ ] **6.1** Create `lambdas/prediction/forecast_aggregator/handler.py`
  - For each disease + state + week: collect all provider forecasts from DynamoDB
  - Normalize targets (do not mix units — hospitalizations vs. ED visits vs. cases)
  - Compute weighted mean of point estimates using trust_weight
  - Blend quantiles (weighted average per quantile level)
  - Detect conflicts: providers disagree by >50% on magnitude or direction
  - Write aggregated result back to DynamoDB (same table, separate record or attribute)
  - Emit EventBridge event: `healthsignals.forecast.updated` with aggregated forecast
  - Emit CloudWatch metrics
- [ ] **6.2** Create `lambdas/prediction/forecast_aggregator/__init__.py`

---

## Task Group 7: Core Timing Estimation Enhancement (MINIMAL CORE CHANGE)

This is the one core modification required. Gated by env var — zero impact when plugin is not deployed.

- [ ] **7.1** Modify `lambdas/prediction/timing_estimation/handler.py`
  - Add optional env var: `FORECAST_STATE_TABLE` (empty when plugin not deployed)
  - If env var is set: query DynamoDB for latest aggregated forecast matching state + disease + current week
  - Append `external_forecast` dict to the Lambda output (or `null` if no data / env var unset)
  - If query fails: log warning, set `external_forecast: null`, continue without error
- [ ] **7.2** Update `cdk/stacks/prediction_stack.py` — do NOT add env var or IAM here (plugin stack does it)
  - Note: The ForecastProviderStack will add FORECAST_STATE_TABLE env var and DynamoDB read IAM to the existing timing_estimation Lambda via cross-stack reference or direct IAM policy

---

## Task Group 8: Step Functions Prompt Enhancement

- [ ] **8.1** Modify `stepfunctions/alert_generation.asl.json` — SituationBrief state
  - Add `$.external_forecast` to the States.Format parameters
  - Update system prompt to include: "If external_forecast context is provided and not null, include an EXTERNAL FORECAST CONTEXT section. If null or absent, omit this section entirely."
  - Bedrock naturally handles null fields — no Choice state needed

---

## Task Group 9: CDK Stack (ForecastProviderStack)

- [ ] **9.1** Create `cdk/stacks/forecast_provider_stack.py`
  - DynamoDB forecast-state table (from Task 2.1)
  - FluSight fetcher Lambda + EventBridge weekly schedule (Wednesday)
  - RSV Hub fetcher Lambda + EventBridge weekly schedule
  - Custom model fetcher Lambda (invoked per-provider on schedule)
  - Forecast aggregator Lambda (triggered after ingestion completes)
  - Own Bedrock IAM role (not needed for this stack — no SFN)
  - CloudWatch alarms: ingestion failures, aggregator errors, no data in 14 days
  - CloudWatch dashboard: forecasts fetched, providers active, conflicts detected
- [ ] **9.2** Grant cross-stack permissions
  - ForecastProviderStack adds `FORECAST_STATE_TABLE` env var to existing timing_estimation Lambda
  - ForecastProviderStack adds DynamoDB read IAM policy to timing_estimation Lambda role
  - This is the mechanism by which the plugin "enables itself" in core without core code knowing about it
- [ ] **9.3** S3 permissions for storing raw forecast archives
- [ ] **9.4** Secrets Manager read permission for custom model fetcher (auth credentials)
- [ ] **9.5** EventBridge PutEvents permission for publishing forecast.updated events

---

## Task Group 10: app.py Registration

- [ ] **10.1** Add `enable_forecast_providers` feature flag to `cdk/cdk.json` (default: false — this plugin is opt-in since it modifies core behavior)
- [ ] **10.2** Register in `cdk/app.py`
  - Read feature flag
  - Conditional import and instantiation of ForecastProviderStack
  - Pass data_bucket_name, ops_topic_arn, timing_estimation_function_name
  - Add dependency on prediction stack (needs Lambda ARN for cross-stack env var update)
  - No dispatch plugin needed (this module enriches core predictions, doesn't dispatch its own alerts)

---

## Task Group 11: Standard Forecast Contract Validation

- [ ] **11.1** Create `lambdas/shared/forecast_contract.py` — shared validation utility
  - `validate_forecast(data: dict) -> bool` — checks required fields per Section 4.3
  - `normalize_forecast_geo(data: dict) -> dict` — normalizes geo_value using geo_utils
  - Used by all ingestion Lambdas (FluSight, RSV Hub, custom model)

---

## Task Group 12: Unit Tests

- [ ] **12.1** Test FluSight fetcher — mock GitHub CSV, verify parsing and DynamoDB write
- [ ] **12.2** Test RSV Hub fetcher — mock CSV, verify normalization
- [ ] **12.3** Test custom model fetcher — mock API responses (success, timeout, invalid, auth variants)
- [ ] **12.4** Test forecast aggregator — weighted mean, conflict detection, quantile blending
- [ ] **12.5** Test forecast contract validation — valid/invalid schemas
- [ ] **12.6** Test timing_estimation enhancement — with/without FORECAST_STATE_TABLE env var
- [ ] **12.7** Test geo normalization in forecast context (TX → texas via geo_utils)

---

## Task Group 13: Integration Tests

- [ ] **13.1** Test against real FluSight GitHub repo — fetch latest CSV, verify parsing
- [ ] **13.2** Test against real RSV Hub GitHub repo — fetch latest CSV, verify parsing
- [ ] **13.3** End-to-end: ingest → aggregate → verify timing_estimation output includes external_forecast

---

## Task Group 14: Documentation

- [ ] **14.1** Create `docs/modules/FORECAST_PROVIDER_CONFIGURATION.md` — full configuration guide (similar to DRUG_SHORTAGE_CONFIGURATION.md)
- [ ] **14.2** Update `docs/DATA_SOURCES.md` — add FluSight and RSV Hub sections
- [ ] **14.3** Update `docs/ADDING_MODULES.md` — note that some plugins may require minimal core changes (forecast enrichment pattern)

---

## Implementation Order (Recommended)

```
1.  Task Group 1      (Config files — foundation)
2.  Task Group 2      (DynamoDB table schema)
3.  Task Group 11     (Standard contract validation — used by all fetchers)
4.  Task Group 3      (FluSight fetcher)
5.  Task Group 4      (RSV Hub fetcher)
6.  Task Group 5      (Custom model fetcher)
7.  Task Group 6      (Forecast aggregator)
8.  Task Group 7      (Core timing_estimation enhancement — minimal)
9.  Task Group 8      (Step Functions prompt update)
10. Task Group 9      (CDK stack — wire everything)
11. Task Group 10     (app.py registration)
12. Task Group 12     (Unit tests)
13. Task Group 13     (Integration tests)
14. Task Group 14     (Documentation)
```

---

## Key Design Notes

### One Core Modification (Acknowledged)

This plugin requires modifying the core `timing_estimation` Lambda to optionally read from the forecast-state DynamoDB table. This is:
- **Gated by env var** — when `FORECAST_STATE_TABLE` is empty/unset, the Lambda behaves identically to today
- **Failure-tolerant** — if the DynamoDB query fails, the Lambda logs a warning and continues with `external_forecast: null`
- **Applied by the plugin stack** — the ForecastProviderStack adds the env var and IAM policy to the existing Lambda (the core CDK code doesn't change)

### State Normalization via `shared/geo_utils`

FluSight CSVs use "TX", RSV Hub uses "Texas" or "TX". All ingestion Lambdas normalize using `shared.geo_utils.normalize_state_name()` before writing to DynamoDB. The forecast-state table stores our internal key format ("texas").

### EventBridge Usage

The plugin emits `healthsignals.forecast.updated` events. These are informational — the core does NOT subscribe to them. Instead, the core's timing_estimation Lambda reads forecast data from DynamoDB (pull model). The events are available for future consumers (dashboards, external integrations).

### Trust Weights

| Provider | Default Weight | Notes |
|----------|---------------|-------|
| CDC FluSight Ensemble | 1.0 | State-level magnitude/trajectory only |
| CDC RSV Hub | 1.0 | Same as FluSight |
| Internal historic calibration | 1.0 for county timing, 0.6 for state magnitude | County timing offset is unique contribution |
| User custom model | 0.7 (configurable) | Per-provider in config |

Note: Internal calibration weight of 0.6 applies only to state-level magnitude comparison. For county-specific timing (lag weeks, severity multiplier), internal calibration remains the sole source at weight 1.0, since no external hub provides county-level data.

### Feature Flag Default

Default: `false` — Unlike other plugins, this one modifies core Lambda behavior (adds DynamoDB read). It should be explicitly opted into rather than enabled by default.

---

*Total: 14 task groups, ~35 tasks*
*Estimated: 4-5 days for implementation + testing*

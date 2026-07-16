# CDC Outbreak Alerts Module — Configuration Guide

## Overview

The CDC Outbreak Alerts Module is an optional plugin that extends HealthSignals with real-time outbreak alerting from CDC public data. It monitors the CDC Outbreaks RSS feed daily, uses Bedrock to extract structured information from linked investigation pages, and alerts subscribing counties in affected states.

**Key capabilities:**

1. **Real-time Outbreak Detection** — Monitors CDC Outbreaks RSS feed for new and updated outbreak investigations
2. **AI-powered Data Extraction** — Uses Bedrock to extract affected states, case counts, source food, and severity from CDC pages
3. **State-level Alerting** — Alerts all subscribing counties in affected states with AI-generated situation briefs
4. **Update Tracking** — Re-alerts when outbreaks expand to new states, highlighting geographic spread

All outputs cite CDC as the source and direct recipients to cdc.gov for latest updates.

---

## Architecture

The module operates independently from the core disease surveillance pipeline:

```
CDC Outbreaks RSS Feed (daily poll)
         │
         ▼
  CDC Outbreak Fetcher Lambda
         │
         ├── Parse RSS XML → detect NEW/UPDATED items
         ├── Fetch linked CDC investigation page
         ├── Invoke Bedrock → extract structured data
         ├── Store to S3 + update DynamoDB state
         │
         ▼
  Outbreak Processor Lambda
         │
         ├── Normalize state names (shared geo_utils)
         ├── Resolve affected states → subscribing counties
         ├── Identify newly added states (for updates)
         │
         ▼
  Step Functions (own state machine)
         │
         ├── Bedrock: Severity classification
         ├── Bedrock: Situation brief generation
         │
         ▼
  Alert Dispatcher (via outbreak_dispatch plugin)
```

**Key design decisions:**
- No leader detection — the CDC notification IS the detection
- No timing estimation — the outbreak is already happening
- No geographic affinity — affected states are explicitly listed by CDC
- Bedrock extracts data from CDC pages (not fragile HTML regex scraping)

---

## Enabling the Module

In `cdk/cdk.json`:

```json
{
  "context": {
    "enable_cdc_outbreak_alerts": true
  }
}
```

Then deploy:

```bash
cdk deploy --all
```

This creates the `HealthSignals-CDCOutbreaks` stack and registers the `outbreak_dispatch` plugin with the alert dispatcher.

---

## Configuration Files

| File | Purpose |
|------|---------|
| `config/data_sources/cdc_outbreaks_rss.json` | RSS feed URL, poll frequency, retry settings, Bedrock model config |
| `config/alert_categories.json` | Foodborne outbreak categories for subscriber opt-in |

---

## Schema: cdc_outbreaks_rss.json

```json
{
  "source_name": "cdc_outbreaks_rss",
  "display_name": "CDC Outbreaks RSS Feed",
  "enabled": true,
  "api": {
    "base_url": "https://tools.cdc.gov/api/v2/resources/media/285676.rss",
    "auth_type": "none",
    "timeout_seconds": 30,
    "retry_max_attempts": 3,
    "retry_backoff_seconds": 10
  },
  "poll_frequency": {
    "schedule": "daily",
    "cron": "0 8 * * ? *",
    "description": "Daily at 8 AM UTC"
  },
  "extraction": {
    "method": "bedrock",
    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
  },
  "s3_storage": {
    "prefix_pattern": "raw/cdc-outbreaks/{date}/{outbreak_id}.json"
  }
}
```

| Field | Description |
|-------|-------------|
| `api.base_url` | CDC Outbreaks RSS feed URL (standard RSS 2.0) |
| `api.auth_type` | Always `"none"` — public feed |
| `api.timeout_seconds` | HTTP request timeout |
| `poll_frequency.cron` | EventBridge cron expression for daily polling |
| `extraction.method` | `"bedrock"` — uses AI for page content extraction |
| `extraction.model_id` | Bedrock inference profile for extraction |
| `s3_storage.prefix_pattern` | S3 key pattern for storing extracted outbreak data |

---

## Alert Categories

The module registers these categories in `config/alert_categories.json`:

| Category Key | Display Name | Description |
|-------------|--------------|-------------|
| `foodborne_outbreak` | Foodborne Outbreaks (All) | All CDC foodborne outbreak alerts |
| `salmonella` | Salmonella Outbreaks | Salmonella-specific alerts |
| `ecoli` | E. coli Outbreaks | E. coli-specific alerts |
| `listeria` | Listeria Outbreaks | Listeria-specific alerts |
| `cyclospora` | Cyclospora Outbreaks | Cyclospora-specific alerts |

---

## CDK Resources (HealthSignals-CDCOutbreaks Stack)

| Resource | Type | Purpose |
|----------|------|---------|
| `healthsignals-cdc-outbreak-state` | DynamoDB | Tracks known outbreaks (PK: outbreak_id, TTL) |
| `healthsignals-cdc-outbreak-fetcher` | Lambda | Daily RSS polling + Bedrock extraction |
| `healthsignals-outbreak-processor` | Lambda | State normalization + county resolution + SFN start |
| `healthsignals-outbreak-alert-generation` | Step Functions | Severity classification + brief generation + dispatch |
| Daily EventBridge rule | Schedule | Triggers fetcher at 8 AM UTC |
| 2 CloudWatch alarms | Monitoring | Fetcher errors, no data in 7 days |
| CloudWatch dashboard | Monitoring | Outbreaks detected, Lambda errors |

---

## Bedrock Extraction

The fetcher Lambda passes CDC page text content to Bedrock with a structured extraction prompt. Bedrock returns a JSON object with:

```json
{
  "disease_name": "Cyclosporiasis",
  "affected_states": ["Michigan", "Ohio", "West Virginia", "Kentucky"],
  "case_count": 400,
  "hospitalizations": null,
  "deaths": null,
  "source_food": "Unknown",
  "onset_date": "2026-06-22",
  "status": "active",
  "summary": "Large multistate outbreak in 4 midwestern states."
}
```

Extraction rules enforced via prompt:
- Only extract explicitly stated information
- Use full state names
- Return `null` for fields not mentioned
- Never hallucinate or infer unstated data

---

## Severity Classification

The Step Functions state machine classifies severity using explicit grounding rules:

| Severity | Criteria |
|----------|----------|
| LOW | <50 cases, 1-2 states, no hospitalizations |
| MODERATE | 50-500 cases, 2-5 states, some hospitalizations |
| HIGH | 500-1000 cases, 5+ states, significant hospitalizations |
| CRITICAL | >1000 cases, 10+ states, deaths or rapid spread |

---

## State Name Normalization

The module uses `lambdas/shared/geo_utils.py` for normalizing state names extracted by Bedrock:

- Full names: "North Carolina" → "north carolina"
- Postal codes: "NC" → "north carolina"
- Abbreviations: "N. Carolina", "W. Virginia" → normalized form

Unrecognized names are logged and skipped (no crash).

---

## Subscription Model

Subscribers opt in per-state using the existing subscription infrastructure. When CDC reports an outbreak affecting a state, all active verified subscribers in that state receive an alert.

No new DynamoDB GSI is needed — the module reuses the existing `state-index` on the subscriptions table.

---

## Change Detection & Updates

The DynamoDB state table tracks each outbreak by `outbreak_id` (generated from title). Detection logic:

- **NEW**: RSS item not in DynamoDB → full processing + alert
- **UPDATED**: RSS item exists but `pubDate` changed → re-fetch page, re-alert ALL states, highlight newly added states in the brief
- **UNCHANGED**: Same `pubDate` → skip

When an outbreak updates (new states added), the generated brief begins with "UPDATE:" and explicitly notes which states are newly affected.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| No outbreaks detected | RSS feed empty or unreachable | Check CDC RSS URL, verify Lambda can reach internet |
| Bedrock extraction returns null fields | CDC page layout changed significantly | Check CloudWatch logs for extraction prompt/response |
| States not matching | Bedrock extracted abbreviation not in lookup | Add variant to `lambdas/shared/geo_utils.py` STATE_LOOKUP |
| No alerts delivered | No subscribers in affected state | Create subscription for a county in that state |
| Duplicate alerts | DynamoDB state lost | Check healthsignals-cdc-outbreak-state table |
| Lambda timeout | CDC page very large or slow | Increase Lambda timeout, check network |

---

## Phase 2 Roadmap

| Enhancement | Description | Status |
|-------------|-------------|--------|
| County-level extraction | Extract specific counties from CDC pages (when mentioned) | Planned |
| HAN integration | Monitor CDC Health Alert Network for urgent notices | Pending (HAN feed currently stale) |
| Multi-source correlation | Cross-reference with FDA recalls for food-linked outbreaks | Research |
| Historical trend analysis | Track outbreak patterns over time for seasonal preparedness | Planned |
| Real-time push | Replace daily polling with webhook/push if CDC provides one | Pending CDC API support |

# Amazon HealthSignals — Bedrock Agent Blueprint

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-orange.svg)](https://docs.aws.amazon.com/cdk/v2/guide/home.html)
[![Python](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![Bedrock](https://img.shields.io/badge/Amazon%20Bedrock-Claude-purple.svg)](https://aws.amazon.com/bedrock/)

**Predictive disease surveillance for rural counties using metropolitan sentinel signals and generative AI.**

---

## Problem

Rural health departments (2,000+ counties, <50K population) lack resources for real-time disease surveillance. They discover outbreaks 3–6 weeks after metropolitan areas — too late to prepare. Meanwhile, cities generate rich syndromic data (ED visits, wastewater, lab confirmations) that _already predicts_ what rural counties will face.

## How It Works

**Core Pipeline (Disease Surveillance):**

1. **Monitor** — Weekly ingestion from CMU Delphi Epidata API + CDC NWSS wastewater + CDC NSSP respiratory data for sentinel metros
2. **Predict** — When a metro crosses threshold, historical lag tables estimate when and how severely each subscribed rural county will be affected
3. **Generate** — Step Functions orchestrates 4 Bedrock InvokeModel calls to produce situation briefs, severity classifications, preparation checklists, and SMS/email alerts
4. **Deliver** — Alerts reach rural health officers via SMS/email

**CDC Outbreak Alerts (Plugin):**

- Daily RSS polling from CDC Outbreaks feed
- Bedrock extracts structured data (disease, states, case counts) from unstructured CDC pages
- Step Functions generates severity classification + situation brief
- State-based alerting to all subscribers in affected states

**Drug Shortage Intelligence (Plugin):**

- Weekly openFDA Drug Shortages API polling
- Change detection (NEW, WORSENING, RESOLVED)
- Combined enrichment with disease outbreak context
- Dedicated Step Functions alert generation

**Forecast Providers (Plugin):**

- CDC FluSight and RSV Hub ensemble forecast ingestion
- Custom model provider support
- Weighted aggregation across multiple forecast sources
- Enriches core predictions with external context

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                            Amazon HealthSignals                                 │
├───────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌──────────────┐     ┌──────────────────┐     ┌──────────────────────────┐   │
│  │  EventBridge │────▶│  SQS Queues      │────▶│  Lambda Ingestion Fleet  │   │
│  │  Schedules   │     │  (per module     │     │  ┌────────┐ ┌────────┐  │   │
│  │  ┌────────┐  │     │   + DLQ each)    │     │  │Delphi  │ │CDC NWSS│  │   │
│  │  │Weekly  │  │     └──────────────────┘     │  │Fetcher │ │Fetcher │  │   │
│  │  │Mon 6AM │  │                              │  └────────┘ └────────┘  │   │
│  │  ├────────┤  │     All fetchers use SQS     │  ┌────────┐ ┌────────┐  │   │
│  │  │Daily   │  │     for retry + DLQ:         │  │CDC Resp│ │CDC RSS │  │   │
│  │  │8AM UTC │  │     • Core: 3 queues + DLQ   │  │Fetcher │ │Fetcher │  │   │
│  │  ├────────┤  │     • CDC: 1 queue + DLQ     │  └────────┘ └────────┘  │   │
│  │  │Weekly  │  │     • Shortage: 1 queue + DLQ│  ┌────────┐ ┌────────┐  │   │
│  │  │Wed 10AM│  │     • Forecast: 2 queues     │  │openFDA │ │FluSight│  │   │
│  │  └────────┘  │       + shared DLQ           │  │Shortage│ │/RSVHub │  │   │
│  └──────────────┘                              │  └───┬────┘ └───┬────┘  │   │
│                                                 └─────┼───────────┼───────┘   │
│                                                       │           │            │
│                                                       ▼           ▼            │
│                                  ┌───────────────────────────────────┐         │
│                                  │          S3 Data Lake              │         │
│                                  │  (versioned, time-partitioned)     │         │
│                                  └───────────────┬───────────────────┘         │
│                                                  │                             │
│                          ┌───────────────────────┼────────────────────┐        │
│                          │ S3 Event              │ Lambda Invoke       │        │
│                          ▼                       ▼                    ▼        │
│  ┌─────────────────────────────┐  ┌──────────────────┐  ┌──────────────┐     │
│  │  Pipeline Coordinator       │  │Outbreak Processor │  │  Shortage    │     │
│  │  ┌──────────┐ ┌──────────┐ │  │(state-based fan  │  │  Enrichment  │     │
│  │  │Leader    │ │Geographic│ │  │ out to SFN)      │  │  Lambda      │     │
│  │  │Detection │ │Affinity  │ │  └────────┬─────────┘  └──────┬───────┘     │
│  │  └──────────┘ └──────────┘ │           │                    │             │
│  │  ┌──────────────────────┐  │           │                    │             │
│  │  │ Timing Estimation    │  │           │                    │             │
│  │  └──────────┬───────────┘  │           │                    │             │
│  └─────────────┼──────────────┘           │                    │             │
│                │                           │                    │             │
│                ▼                           ▼                    ▼             │
│  ┌───────────────────────────────────────────────────────────────────┐       │
│  │              Step Functions Workflows (3 state machines)            │       │
│  │                                                                     │       │
│  │  Core Alert Generation:        CDC Outbreak Generation:            │       │
│  │  ┌──────────┐ ┌──────────┐   ┌──────────┐ ┌──────────┐          │       │
│  │  │Situation │ │Severity  │   │Severity  │ │Outbreak  │          │       │
│  │  │Brief     │ │Classify  │   │Classify  │ │Brief     │          │       │
│  │  └──────────┘ └──────────┘   └──────────┘ └──────────┘          │       │
│  │  ┌──────────┐ ┌──────────┐   ┌──────────────────────┐           │       │
│  │  │Checklist │ │Comms     │   │Dispatch Alert        │           │       │
│  │  │(4.5/5)   │ │Drafting  │   └──────────────────────┘           │       │
│  │  └──────────┘ └──────────┘                                       │       │
│  │                                                                     │       │
│  │  Shortage Alert Generation:                                        │       │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────────┐              │       │
│  │  │Severity  │ │Shortage  │ │Dispatch Alert        │              │       │
│  │  │Classify  │ │Brief     │ └──────────────────────┘              │       │
│  │  └──────────┘ └──────────┘                                        │       │
│  └───────────────────────────────────────────────────────────────────┘       │
│                              │                                                │
│                              ▼                                                │
│  ┌───────────────────────────────────────────────────────────────────┐       │
│  │           Alert Dispatcher (Plugin Registry)                       │       │
│  │  Core: disease_outbreak  │ Plugins: cdc_outbreak, shortage        │       │
│  │  Query subscriptions → Filter active/verified → SES + SNS        │       │
│  └───────────────────────────────────────────────────────────────────┘       │
│                                                                               │
│  ┌──────────────────────────────────────┐                                    │
│  │  Subscription API (API Gateway)       │  County self-service:             │
│  │  POST /subscribe    GET /verify       │  subscribe, verify, unsubscribe,  │
│  │  POST /unsubscribe  PUT /preferences  │  update preferences, check status │
│  │  GET  /status                         │                                    │
│  └──────────────────────────────────────┘                                    │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Config-Driven Scaling

HealthSignals is **fully config-driven**. No code changes needed to add states or diseases:

| To do this...                    | Action                                              | Code change?   |
| -------------------------------- | --------------------------------------------------- | -------------- |
| Add a new state (e.g., Florida)  | Create `config/states/florida.json`                 | No             |
| Add a new disease (e.g., Mpox)   | Create `config/diseases/mpox.json`                  | No             |
| Change flu threshold             | Edit one number in `config/diseases/influenza.json` | No             |
| Add 50 more counties             | Append to array in state config                     | No             |
| Override threshold for one state | Add `disease_overrides` in state config             | No             |
| Add a new data source            | Add config JSON + new fetcher Lambda                | Yes (one file) |

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full config reference.

## Data Sources

| Source                     | Endpoint                                          | Data                                        | Frequency        |
| -------------------------- | ------------------------------------------------- | ------------------------------------------- | ---------------- |
| **CMU Delphi Epidata**     | `api.delphi.cmu.edu/epidata/covidcast/`           | County-level % ED visits (flu, RSV, COVID)  | Weekly (Mon)     |
| **CDC NWSS Wastewater**    | `data.cdc.gov/resource/{id}.json`                 | Wastewater viral activity (Flu, RSV, COVID) | Weekly (Mon)     |
| **CDC NSSP Respiratory**   | `data.cdc.gov/resource/rdmq-nq56.json`            | State-level % ED visits                     | Weekly (Mon)     |
| **CDC Outbreaks RSS**      | `tools.cdc.gov/api/v2/resources/media/285676.rss` | Foodborne/parasitic outbreak investigations | Daily (8 AM UTC) |
| **openFDA Drug Shortages** | `api.fda.gov/drug/shortages.json`                 | Drug shortage status and details            | Weekly (Mon)     |
| **CDC FluSight Ensemble**  | FluSight Hub API                                  | National/state flu forecasts                | Weekly (Wed)     |
| **RSV Hub Ensemble**       | RSV Forecast Hub API                              | National/state RSV forecasts                | Weekly (Wed)     |

All public, no PHI, no HIPAA, no data sharing agreements. See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

> **Note:** NSSP signals on the Delphi API do NOT support `geo_type=msa`. We use the **primary county FIPS** of each metro as a proxy for metro-level surveillance.

## Quickstart

**Prerequisites:**

- AWS account with [Bedrock model access](https://console.aws.amazon.com/bedrock/home#/modelaccess) enabled for Claude Sonnet 4.5
- AWS CDK v2, Python 3.11+, Node.js 20+
- AWS CLI configured with credentials

```bash
# 1. Clone
git clone https://github.com/goginea/aws-healthsignals.git
cd aws-healthsignals

# 2. Deploy infrastructure (up to 10 CDK stacks — takes ~5 minutes first time)
cd cdk && pip install -r requirements.txt
npx aws-cdk bootstrap aws://ACCOUNT_ID/us-east-1
npx aws-cdk deploy --all --require-approval never
cd ..

# 3. Upload config to S3 (MUST do before any Lambda invocation)
aws s3 sync config/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/config/

# 4. Upload Knowledge Base documents to S3
aws s3 sync bedrock/knowledge_bases/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/

# 5. Seed historical calibration data (3 seasons)
python scripts/seed_calibration_data.py --seasons 3

# 6. Test: invoke the Delphi fetcher
aws lambda invoke --function-name healthsignals-delphi-fetcher --payload '{}' /dev/stdout

# 7. (Optional) Subscribe a test county
curl -X POST https://API_ID.execute-api.us-east-1.amazonaws.com/prod/subscription/subscribe \
  -d '{"county_fips":"48143","county_name":"Erath County","state":"texas","contact_name":"Test","contact_email":"you@example.com","diseases":["influenza","rsv","covid"]}'
```

> **Note:** Replace `ACCOUNT_ID` with your AWS account ID. The system monitors weekly and alerts only when thresholds are crossed. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deployment guide.

## CDK Stacks

### Core (Always Deployed)

| Stack                         | Purpose                                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------------------------- |
| `HealthSignals-Ingestion`     | S3 bucket, SQS queues + DLQ, 3 core fetcher Lambdas, EventBridge weekly schedule                  |
| `HealthSignals-Prediction`    | DynamoDB tables (configs, alerts, calibration), 3 prediction Lambdas                              |
| `HealthSignals-Generation`    | Step Functions state machine (4-step Bedrock workflow), Bedrock IAM                               |
| `HealthSignals-Orchestration` | Pipeline coordinator Lambda, pipeline_runs DynamoDB, S3 event trigger                             |
| `HealthSignals-Delivery`      | SES/SNS, alert dispatcher (plugin registry), feedback collector + recalibrator, feedback DynamoDB |
| `HealthSignals-Subscription`  | API Gateway, 5 subscription Lambdas, subscriptions DynamoDB + GSIs, Secrets Manager               |
| `HealthSignals-Monitoring`    | CloudWatch dashboards, X-Ray, alarms, ops SNS topic                                               |

### Plugin Modules (Feature-Flagged)

| Stack                             | Feature Flag                 | Purpose                                                                                                                 |
| --------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `HealthSignals-CDCOutbreaks`      | `enable_cdc_outbreak_alerts` | CDC RSS fetcher, outbreak processor, Bedrock extraction, outbreak SFN, DynamoDB state table, daily EventBridge schedule |
| `HealthSignals-DrugShortage`      | `enable_drug_shortage`       | openFDA fetcher, shortage change detector, shortage enrichment, shortage SFN, shortage alerts DynamoDB                  |
| `HealthSignals-ForecastProviders` | `enable_forecast_providers`  | FluSight/RSV Hub/custom model fetchers, forecast aggregator, forecast state DynamoDB                                    |

All plugins are enabled by default in `cdk.json`. Disable with `"enable_<module>": false`.

## Knowledge Bases (Pre-populated)

### CDC Guidelines (Precision Retrieval) — 7 documents

- Influenza preparedness (stockpiling, staffing surge, school closure framework)
- RSV guidance (immunoprophylaxis timing, pediatric capacity thresholds)
- COVID-19 current guidance (antivirals, wastewater interpretation, variants)
- Activity level definitions (ARI metric, percentile methodology)
- CERC communication principles (be first/right/credible, uncertainty language)
- Rural health resources (HPSA, mutual aid, Critical Access Hospital limits)

### Communication Templates (Variety Retrieval) — 33 templates

- Severity-graded email alerts (LOW through CRITICAL + ALL-CLEAR)
- SMS templates (≤160 characters)
- Public announcement templates (press release, Facebook, school letters)
- Partner notification templates (state escalation, hospital, EMS, pharmacy)
- Follow-up templates (weekly update, feedback request, season wrap-up)
- Subscription lifecycle templates (verification, welcome, unsubscribe, pause)

### Shortage Guidance — 3 documents

- FDA shortage protocols
- Inventory management strategies
- Therapeutic substitution framework

## Subscription System

Counties self-service subscribe via API — no AWS console access needed:

```
POST /subscribe  → Creates subscription (pending verification)
GET  /verify     → Confirms email (activates subscription)
POST /unsubscribe → Soft-deletes (one-click from email link)
PUT  /preferences → Update diseases, channels, pause/resume
GET  /status     → Check subscription health
```

Double opt-in. Signed unsubscribe tokens in every alert. Pause for off-season.
See [docs/SUBSCRIPTION_SCHEMA.md](docs/SUBSCRIPTION_SCHEMA.md).

## Cost Estimate (Single Deployment, 100 Counties, All Modules Enabled)

This is a single shared deployment — one AWS account serves all subscribed counties. No per-county infrastructure duplication. Estimates based on current [AWS pricing](https://aws.amazon.com/pricing/) (us-east-1), **excluding free tier**. Assumes peak-month usage with all modules active.

Workload assumptions:

- **Core pipeline**: Weekly ingestion (3 sources × 4 weeks), threshold crossings trigger ~100 county alerts/month (4 Bedrock calls each)
- **CDC Outbreak Alerts**: Daily RSS polling (30 fetches/month), ~4 new/updated outbreaks processed, each alerting 1–3 monitored states
- **Drug Shortage**: Weekly openFDA polling (4 fetches/month), ~5 shortage alerts generated
- **Forecast Providers**: Weekly FluSight + RSV Hub + custom model ingestion (12 fetches/month), weekly aggregation

| Component                       | Usage Basis                                                                                                                                                   | Monthly              |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| **Bedrock (Claude Sonnet 4.5)** | Core: 100 counties × 4 calls = 400. CDC: 4 outbreaks × 3 states × 2 calls + 4 extractions = 28. Shortage: 5 × 2 = 10. Total ~440 calls, avg ~3.5K tokens each | $80–150              |
| **Lambda**                      | ~15 functions, ~12K invocations/month (daily CDC + weekly core + weekly shortage + weekly forecast), avg 512MB/3s. $0.20/1M requests + $0.0000166667/GB-s     | $4–8                 |
| **Step Functions**              | ~110 executions × 4–5 transitions = ~500 transitions. $0.000025/transition                                                                                    | <$1                  |
| **DynamoDB (on-demand)**        | 6 tables, ~30K reads ($0.25/1M RRU) + 8K writes ($1.25/1M WRU), <2GB storage ($0.25/GB)                                                                       | $2–4                 |
| **S3**                          | <5GB storage ($0.023/GB), ~5K PUTs ($0.005/1K) + 20K GETs ($0.0004/1K)                                                                                        | <$1                  |
| **SES**                         | ~500 emails/month (100 county alerts + CDC + shortage + lifecycle) @ $0.10/1,000                                                                              | <$1                  |
| **SNS SMS**                     | ~250 SMS/month @ $0.00645/message (US transactional)                                                                                                          | $1–2                 |
| **EventBridge + SQS**           | ~500 events ($1/1M) + ~500 messages                                                                                                                           | <$1                  |
| **API Gateway**                 | Subscription API, ~1K requests/month @ $3.50/1M                                                                                                               | <$1                  |
| **CloudWatch**                  | Logs (~$0.50/GB ingested, ~2GB/month), 3 dashboards ($3/each), 10 alarms ($0.10/each)                                                                         | $10–15               |
| **Total**                       |                                                                                                                                                               | **$100–185/month**   |
| **Per county**                  |                                                                                                                                                               | **$1.00–1.85/month** |

> **Notes:**
>
> - Bedrock is the primary cost driver (~70% of total).
> - SMS costs scale with subscribers opting into SMS. Budget ~$0.65/subscriber/month for SMS-enabled counties.
> - Per-county cost decreases as you add counties since infrastructure costs are fixed.

## Project Structure

```
aws-healthsignals/
├── README.md
├── LICENSE                          # Apache 2.0
├── CONTRIBUTING.md
├── architecture/
│   └── architecture.md             # Detailed architecture + decision rationale
├── cdk/                            # AWS CDK infrastructure (up to 10 stacks)
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       ├── ingestion_stack.py      # S3 + SQS + DLQ + Lambdas + EventBridge
│       ├── prediction_stack.py     # DynamoDB + prediction Lambdas
│       ├── generation_stack.py     # Step Functions + Bedrock IAM
│       ├── orchestration_stack.py  # Pipeline coordinator + S3 trigger
│       ├── delivery_stack.py       # SES + SNS + dispatcher Lambda
│       ├── subscription_stack.py   # API Gateway + subscription Lambdas
│       ├── monitoring_stack.py     # CloudWatch + X-Ray + alarms
│       ├── cdc_outbreak_alerts_stack.py   # [Plugin] CDC outbreak pipeline
│       ├── shortage_stack.py              # [Plugin] Drug shortage pipeline
│       └── forecast_provider_stack.py     # [Plugin] External forecast providers
├── config/                         # All operational config (JSON, uploaded to S3)
│   ├── system.json                 # Global settings (tables, models, delivery)
│   ├── data_sources/               # API endpoints + settings
│   ├── states/                     # Per-state metros, counties, overrides
│   ├── diseases/                   # Per-disease thresholds, signals
│   ├── forecast_providers/         # FluSight, RSV Hub, custom model configs
│   ├── shortage_monitoring/        # Drug shortage monitoring config
│   └── subscription_settings.json
├── lambdas/
│   ├── shared/                     # Shared utilities (also deployed as Lambda layer)
│   │   ├── config_loader.py        # S3 config loading + caching
│   │   ├── token_utils.py          # HMAC token generation/validation
│   │   ├── geo_utils.py            # State name normalization
│   │   └── forecast_contract.py    # Forecast provider interface
│   ├── ingestion/                  # 8 data source fetchers
│   │   ├── delphi_fetcher/         # CMU Delphi Epidata (core)
│   │   ├── cdc_wastewater_fetcher/ # CDC NWSS wastewater (core)
│   │   ├── cdc_respiratory_fetcher/# CDC NSSP respiratory (core)
│   │   ├── cdc_outbreak_fetcher/   # CDC Outbreaks RSS + Bedrock extraction (plugin)
│   │   ├── openfda_shortage_fetcher/   # openFDA drug shortages (plugin)
│   │   ├── flusight_forecast_fetcher/  # CDC FluSight ensemble (plugin)
│   │   ├── rsv_hub_forecast_fetcher/   # RSV Hub ensemble (plugin)
│   │   └── custom_model_fetcher/       # Custom forecast model (plugin)
│   ├── prediction/                 # 5 prediction/analysis functions
│   │   ├── leader_detection/       # Metro threshold crossing (core)
│   │   ├── geographic_affinity/    # County-metro mapping (core)
│   │   ├── timing_estimation/      # Lag + severity projection (core)
│   │   ├── shortage_change_detector/   # NEW/WORSENING/RESOLVED (plugin)
│   │   └── forecast_aggregator/        # Multi-provider aggregation (plugin)
│   ├── orchestration/              # Pipeline coordination
│   │   ├── pipeline_coordinator/   # S3 trigger → prediction → SFN (core)
│   │   ├── outbreak_processor/     # CDC outbreak → state fan-out → SFN (plugin)
│   │   └── shortage_enrichment/    # Disease+shortage combined signal (plugin)
│   ├── delivery/                   # Alert delivery
│   │   ├── alert_dispatcher/       # Plugin registry: disease_outbreak, cdc_outbreak, shortage
│   │   ├── feedback_collector/     # Officer feedback collection
│   │   └── feedback_recalibrator/  # Adjust predictions from feedback
│   └── subscription/              # 5 subscription API handlers
│       ├── subscribe/
│       ├── verify/
│       ├── unsubscribe/
│       ├── update_preferences/
│       └── status/
├── layers/
│   └── shared/                     # Lambda layer (shared utilities)
│       └── python/shared/          # Packaged as /opt/python/shared/ at runtime
├── stepfunctions/
│   ├── alert_generation.asl.json           # Core: 4-step Bedrock workflow
│   ├── outbreak_alert_generation.asl.json  # CDC: severity + brief + dispatch
│   └── shortage_alert_generation.asl.json  # Shortage: severity + brief + dispatch
├── bedrock/
│   ├── prompts/                    # System prompts (6)
│   ├── guardrails/                 # Denied topics + word filters
│   └── knowledge_bases/            # Pre-populated reference docs
│       ├── cdc_guidelines/         # 7 CDC reference documents
│       ├── communication_templates/ # 33 templates (7 categories)
│       └── shortage_guidance/      # 3 FDA/clinical shortage docs
├── tests/
│   ├── unit/                       # pytest unit tests (~260 tests)
│   ├── integration/                # Live API connectivity tests
│   └── data/                       # Mock API responses
├── docs/
│   ├── DEPLOYMENT.md               # Full deployment guide
│   ├── TEARDOWN.md                 # Stack teardown guide
│   ├── CONFIGURATION.md            # Config reference
│   ├── DATA_SOURCES.md             # Data source details
│   ├── SUBSCRIPTION_SCHEMA.md      # DynamoDB subscription schema
│   ├── ADDING_MODULES.md           # How to add new plugin modules
│   └── modules/                    # Per-module configuration guides
│       ├── CDC_OUTBREAK_CONFIGURATION.md
│       ├── DRUG_SHORTAGE_CONFIGURATION.md
│       └── FORECAST_PROVIDER_CONFIGURATION.md
└── scripts/
    ├── seed_calibration_data.py    # Bootstrap 3 seasons of historical data
    ├── validate_predictions.py     # Validate prediction accuracy
    ├── validate_shortage_config.py # Validate shortage module config
    └── test_end_to_end.sh          # End-to-end integration test
```

## Methodology

The prediction engine uses established epidemiological methods — no algorithmic novelty is claimed:

| Component            | Method                                 | Reference                       |
| -------------------- | -------------------------------------- | ------------------------------- |
| Leader detection     | Threshold crossing + cross-correlation | Standard syndromic surveillance |
| Timing estimation    | Historical lag median ± stdev          | Viboud et al. (2006)            |
| Severity projection  | Peak ratio across seasons              | Pei & Shaman (2018)             |
| Forecast aggregation | Weighted mean + conflict detection     | FluSight ensemble methodology   |
| GenAI role           | Interpretation + communication ONLY    | Not prediction                  |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Areas where help is needed:

- Epidemiologists: Validate threshold parameters
- Public health practitioners: Review communication templates
- Cloud engineers: Improve CDK constructs, add observability
- Data engineers: Add new data source integrations

## License

[Apache 2.0](LICENSE)

---

_Built by [Avinash Gogineni](https://github.com/goginea) — AWS Technical Account Manager_

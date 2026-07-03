# Amazon HealthSignals — Bedrock Agent Blueprint

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-orange.svg)](https://docs.aws.amazon.com/cdk/v2/guide/home.html)
[![Python](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![Bedrock](https://img.shields.io/badge/Amazon%20Bedrock-Claude-purple.svg)](https://aws.amazon.com/bedrock/)

**Predictive disease surveillance for rural counties using metropolitan sentinel signals and generative AI.**

---

## Problem

Rural health departments (2,000+ counties, <50K population) lack resources for real-time disease surveillance. They discover outbreaks 3–6 weeks after metropolitan areas — too late to prepare. Meanwhile, cities generate rich syndromic data (ED visits, wastewater, lab confirmations) that *already predicts* what rural counties will face.

## How It Works

1. **Monitor** — Weekly ingestion from CMU Delphi Epidata API + CDC NWSS wastewater + CDC NSSP respiratory data for sentinel metros
2. **Predict** — When a metro crosses threshold, historical lag tables estimate when and how severely each subscribed rural county will be affected
3. **Generate** — Step Functions orchestrates 4 Bedrock InvokeModel calls to produce situation briefs, severity classifications, preparation checklists, and SMS/email alerts
4. **Deliver** — Alerts reach rural health officers via SMS/email (≤5 min/week engagement)

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                            Amazon HealthSignals                                 │
├───────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌──────────────┐     ┌──────────────────┐     ┌──────────────────────────┐   │
│  │  EventBridge │────▶│  SQS Queues      │────▶│  Lambda Ingestion Fleet  │   │
│  │  (weekly)    │     │  (3 queues + DLQ)│     │  ┌────────┐ ┌────────┐  │   │
│  └──────────────┘     └──────────────────┘     │  │Delphi  │ │CDC NWSS│  │   │
│                                                 │  │Fetcher │ │Fetcher │  │   │
│                                                 │  └───┬────┘ └───┬────┘  │   │
│                                                 │  ┌───────────┐  │       │   │
│                                                 │  │CDC Resp.  │──┘       │   │
│                                                 │  │Fetcher    │          │   │
│                                                 │  └───┬───────┘          │   │
│                                                 └──────┼──────────────────┘   │
│                                                        │                       │
│                                                        ▼                       │
│                                  ┌───────────────────────────────────┐         │
│                                  │          S3 Data Lake              │         │
│                                  │  (versioned, time-partitioned)     │         │
│                                  └───────────────┬───────────────────┘         │
│                                                  │                             │
│                                                  │ S3 Event                    │
│                                                  ▼                             │
│  ┌───────────────────────────────────────────────────────────────────┐         │
│  │              Pipeline Coordinator (Orchestration Lambda)           │         │
│  │  ┌───────────────┐  ┌──────────────┐  ┌────────────────────┐    │         │
│  │  │Leader         │─▶│Geographic    │─▶│Timing Estimation   │    │         │
│  │  │Detection      │  │Affinity      │  │(lag + severity)    │    │         │
│  │  └───────────────┘  └──────────────┘  └─────────┬──────────┘    │         │
│  └──────────────────────────────────────────────────┼────────────────┘         │
│                                                     │                          │
│                              ┌───────────────────────┘                         │
│                              ▼  (one execution per affected county)            │
│  ┌───────────────────────────────────────────────────────────────────┐         │
│  │                    Step Functions Workflow                          │         │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  ┌──────────┐  │         │
│  │  │ Situation   │─▶│ Severity    │─▶│Checklist │─▶│  Comms   │  │         │
│  │  │ Brief       │  │ Classify    │  │Generate  │  │ Drafting │  │         │
│  │  │(Sonnet 4.5) │  │(Sonnet 4.5) │  │(Sonnet   │  │(Sonnet   │  │         │
│  │  │             │  │             │  │ 4.5/5)   │  │ 4.5/5)   │  │         │
│  │  └─────────────┘  └─────────────┘  └──────────┘  └──────────┘  │         │
│  │                                                                   │         │
│  │  Knowledge Bases: CDC Guidelines │ Communication Templates        │         │
│  │  Guardrails: Block clinical/diagnostic language                   │         │
│  └───────────────────────────────────────────────────────────────────┘         │
│                              │                                                  │
│                              ▼                                                  │
│  ┌───────────────────────────────────────────────────────────────────┐         │
│  │                    Alert Dispatcher                                 │         │
│  │  Query subscriptions → Filter active/verified → SES email + SNS   │         │
│  │  (includes unsubscribe link, confidence disclaimer)                │         │
│  └───────────────────────────────────────────────────────────────────┘         │
│                                                                                 │
│  ┌──────────────────────────────────────┐                                      │
│  │  Subscription API (API Gateway)       │  County self-service:               │
│  │  POST /subscribe    GET /verify       │  subscribe, verify, unsubscribe,    │
│  │  POST /unsubscribe  PUT /preferences  │  update preferences, check status   │
│  │  GET  /status                         │                                      │
│  └──────────────────────────────────────┘                                      │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Config-Driven Scaling

HealthSignals is **fully config-driven**. No code changes needed to add states or diseases:

| To do this... | Action | Code change? |
|---|---|---|
| Add a new state (e.g., Florida) | Create `config/states/florida.json` | ❌ No |
| Add a new disease (e.g., Mpox) | Create `config/diseases/mpox.json` | ❌ No |
| Change flu threshold | Edit one number in `config/diseases/influenza.json` | ❌ No |
| Add 50 more counties | Append to array in state config | ❌ No |
| Override threshold for one state | Add `disease_overrides` in state config | ❌ No |
| Add a new data source | Add config JSON + new fetcher Lambda | ✅ One file |

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full config reference.

## Data Sources

| Source | Endpoint | Data | Auth |
|--------|----------|------|------|
| **CMU Delphi Epidata** | `api.delphi.cmu.edu/epidata/covidcast/` | County-level % ED visits (flu, RSV, COVID) — `geo_type=county`, `time_type=week` (epiweek YYYYWW) | None |
| **CDC NWSS Wastewater** | `data.cdc.gov/resource/{id}.json` | Wastewater viral activity (Flu: `ymmh-divb`, RSV: `45cq-cw4i`, COVID: `2ew6-ywp6`) | None |
| **CDC NSSP Respiratory** | `data.cdc.gov/resource/rdmq-nq56.json` | State-level % ED visits | None |

All public, no PHI, no HIPAA, no data sharing agreements. See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

> **Note:** NSSP signals on the Delphi API do NOT support `geo_type=msa`. We use the **primary county FIPS** of each metro (Harris=48201 for Houston, Dallas=48113 for DFW, Travis=48453 for Austin, Bexar=48029 for San Antonio) as a proxy for metro-level surveillance.

## Quickstart

**Prerequisites:**
- AWS account with [Bedrock model access](https://console.aws.amazon.com/bedrock/home#/modelaccess) enabled for Claude Sonnet 4.5 and Sonnet 5
- AWS CDK v2, Python 3.11+, Node.js 20+
- AWS CLI configured with credentials

```bash
# 1. Clone
git clone https://github.com/goginea/aws-healthsignals.git
cd aws-healthsignals

# 2. Deploy infrastructure (7 CDK stacks — takes ~5 minutes first time)
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

# 6. Grant Bedrock cross-region inference profile permissions
#    (Required until CDK is updated — see docs/DEPLOYMENT.md for details)
aws iam put-role-policy --role-name <SFN_ROLE_NAME> \
  --policy-name BedrockAccess \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel"],"Resource":["*"]}]}'

# 7. Test: invoke the Delphi fetcher
aws lambda invoke --function-name healthsignals-delphi-fetcher --payload '{}' /dev/stdout

# 8. (Optional) Subscribe a test county
curl -X POST https://API_ID.execute-api.us-east-1.amazonaws.com/prod/subscribe \
  -d '{"county_fips":"48143","county_name":"Erath County","state":"texas","contact_name":"Test","contact_email":"you@example.com","diseases":["influenza","rsv","covid"]}'
```

> **Note:** Replace `ACCOUNT_ID` with your AWS account ID (e.g., `767900122304`).
> The system monitors weekly and alerts only when flu season thresholds are crossed (typically Oct–Feb).
> See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deployment guide with troubleshooting.

## CDK Stacks

| Stack | Purpose |
|-------|---------|
| `HealthSignals-Ingestion` | S3 bucket, SQS queues + DLQ, 3 fetcher Lambdas, EventBridge schedule |
| `HealthSignals-Prediction` | DynamoDB tables (configs, alerts, calibration), 3 prediction Lambdas |
| `HealthSignals-Generation` | Step Functions state machine, Bedrock IAM, Guardrails |
| `HealthSignals-Delivery` | SES/SNS configuration, alert dispatcher Lambda |
| `HealthSignals-Monitoring` | CloudWatch dashboards, X-Ray, alarms |
| `HealthSignals-Orchestration` | Pipeline coordinator Lambda, pipeline_runs DynamoDB, S3 event trigger |
| `HealthSignals-Subscription` | API Gateway, 5 subscription Lambdas, subscriptions DynamoDB, GSIs |

## Knowledge Bases (Pre-populated)

### CDC Guidelines (Precision Retrieval) — 6 documents, 44 KB
- Influenza preparedness (stockpiling, staffing surge, school closure framework)
- RSV guidance (immunoprophylaxis timing, pediatric capacity thresholds)
- COVID-19 current guidance (antivirals, wastewater interpretation, variants)
- Activity level definitions (ARI metric, percentile methodology)
- CERC communication principles (be first/right/credible, uncertainty language)
- Rural health resources (HPSA, mutual aid, Critical Access Hospital limits)

### Communication Templates (Variety Retrieval) — 27 templates, 44 KB
- 5 severity-graded email alerts (LOW → CRITICAL → ALL-CLEAR)
- 7 SMS templates (≤160 characters)
- 5 public announcement templates (press release, Facebook, school letters)
- 6 partner notification templates (state escalation, hospital, EMS, pharmacy)
- 4 follow-up templates (weekly update, feedback request, season wrap-up)

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

## Cost (Per 100 Counties)

| Component | Monthly |
|-----------|---------|
| Lambda + EventBridge + SQS | $12–20 |
| S3 + DynamoDB | $8–15 |
| Bedrock (Sonnet 4.5 routine + Sonnet 5 high-severity) | $150–300 |
| Step Functions | $3–7 |
| SES + SNS + CloudWatch | $10–20 |
| API Gateway (subscription API) | $1–3 |
| **Total** | **$184–358/month** |
| **Per county** | **$1.84–3.58/month** |

## Project Structure

```
aws-healthsignals/
├── README.md
├── LICENSE                          # Apache 2.0
├── CONTRIBUTING.md
├── architecture/
│   └── architecture.md             # Detailed architecture + decision rationale
├── cdk/                            # AWS CDK infrastructure (7 stacks)
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       ├── ingestion_stack.py      # S3 + SQS + DLQ + Lambdas + EventBridge
│       ├── prediction_stack.py     # DynamoDB + prediction Lambdas
│       ├── generation_stack.py     # Step Functions + Bedrock IAM
│       ├── delivery_stack.py       # SES + SNS + dispatcher Lambda
│       ├── monitoring_stack.py     # CloudWatch + X-Ray + alarms
│       ├── orchestration_stack.py  # Pipeline coordinator + S3 trigger
│       └── subscription_stack.py   # API Gateway + subscription Lambdas
├── config/                         # All operational config (JSON)
│   ├── system.json                 # Global settings
│   ├── data_sources/               # API endpoints + settings
│   ├── states/                     # Per-state metros, counties, overrides
│   ├── diseases/                   # Per-disease thresholds, signals
│   └── subscription_settings.json
├── lambdas/
│   ├── shared/                     # Config loader + token utilities
│   │   ├── config_loader.py
│   │   └── token_utils.py
│   ├── ingestion/                  # 3 data source fetchers
│   │   ├── delphi_fetcher/
│   │   ├── cdc_wastewater_fetcher/
│   │   └── cdc_respiratory_fetcher/
│   ├── prediction/                 # 3 prediction functions
│   │   ├── leader_detection/
│   │   ├── geographic_affinity/
│   │   └── timing_estimation/
│   ├── orchestration/              # Pipeline coordinator
│   │   └── pipeline_coordinator/
│   ├── delivery/                   # Alert dispatcher + feedback
│   │   ├── alert_dispatcher/
│   │   └── feedback_collector/
│   └── subscription/              # 5 subscription API handlers
│       ├── subscribe/
│       ├── verify/
│       ├── unsubscribe/
│       ├── update_preferences/
│       └── status/
├── stepfunctions/
│   └── alert_generation.asl.json   # 4-step Bedrock workflow (ASL)
├── bedrock/
│   ├── prompts/                    # 4 system prompts
│   ├── guardrails/                 # Denied topics + word filters
│   └── knowledge_bases/            # Pre-populated (11 docs, 88 KB)
│       ├── cdc_guidelines/         # 6 CDC reference documents
│       └── communication_templates/ # 27 templates (5 categories)
├── tests/
│   ├── unit/                       # pytest unit tests
│   ├── integration/                # Live API connectivity tests
│   └── data/                       # Mock API responses
├── docs/
│   ├── DEPLOYMENT.md
│   ├── CONFIGURATION.md
│   ├── DATA_SOURCES.md
│   └── SUBSCRIPTION_SCHEMA.md
└── scripts/
    ├── seed_calibration_data.py
    └── validate_predictions.py
```

## Methodology

The prediction engine uses established epidemiological methods — no algorithmic novelty is claimed:

| Component | Method | Reference |
|-----------|--------|-----------|
| Leader detection | Threshold crossing + cross-correlation | Standard syndromic surveillance |
| Timing estimation | Historical lag median ± stdev | Viboud et al. (2006) |
| Severity projection | Peak ratio across seasons | Pei & Shaman (2018) |
| GenAI role | Interpretation + communication ONLY | Not prediction |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Areas where help is needed:
- Epidemiologists: Validate threshold parameters
- Public health practitioners: Review communication templates
- Cloud engineers: Improve CDK constructs, add observability
- Data engineers: Add new data source integrations

## License

[Apache 2.0](LICENSE)

---

*Built by [Avinash Gogineni](https://github.com/goginea) — AWS Technical Account Manager*

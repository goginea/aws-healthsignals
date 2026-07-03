# Architecture — Amazon HealthSignals Bedrock Blueprint

## System Overview

Amazon HealthSignals is a **deterministic workflow** (not an autonomous agent) that uses generative AI for interpretation and communication, not for prediction. The prediction mechanism is historical lookup + arithmetic.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              DATA PLANE                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  External APIs       SQS Backpressure       Lambda Fleet    Storage      │
│  ┌─────────┐        ┌─────────────┐       ┌──────────┐   ┌────────┐   │
│  │ Delphi  │──┐     │ SQS Queues  │       │ Delphi   │   │  S3    │   │
│  │ Epidata │  │     │ (per source)│       │ Fetcher  │──▶│  Data  │   │
│  └─────────┘  │     │             │       ├──────────┤   │  Lake  │   │
│  ┌─────────┐  │     │ 3 retries   │       │ CDC NWSS │   │        │   │
│  │ CDC     │──┼────▶│ before DLQ  │──────▶│ Fetcher  │──▶│ raw/   │   │
│  │ NWSS    │  │     │             │       ├──────────┤   │ {src}/ │   │
│  └─────────┘  │     │ ┌─────────┐ │       │ CDC Resp │   │ {yr}/  │   │
│  ┌─────────┐  │     │ │   DLQ   │ │       │ Fetcher  │──▶│ W{wk}/ │   │
│  │ CDC     │──┘     │ │(14-day) │ │       └──────────┘   └────────┘   │
│  │ Resp.   │        │ └─────────┘ │                                     │
│  └─────────┘        └─────────────┘       ┌──────────────────┐         │
│                           ▲                │   DynamoDB        │         │
│  ┌──────────────┐         │                │  ├── configs      │         │
│  │  EventBridge │─────────┘                │  ├── alert_state  │         │
│  │  (Mon 6AM)   │                          │  ├── calibration  │         │
│  └──────────────┘                          │  ├── pipeline_runs│         │
│                                            │  └── subscriptions│         │
│  ┌──────────────┐                          └──────────────────┘         │
│  │  Config (S3) │  Loaded at Lambda cold start — drives all behavior    │
│  │  states/     │  Add state = one JSON. Add disease = one JSON.        │
│  │  diseases/   │  Zero code changes for expansion.                     │
│  │  data_srcs/  │                                                        │
│  └──────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                      PREDICTION PLANE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌────────────────┐    ┌─────────────────┐   ┌──────────────┐  │
│  │ Leader         │───▶│ Geographic      │──▶│ Timing       │  │
│  │ Detection      │    │ Affinity        │   │ Estimation   │  │
│  │                │    │                 │   │              │  │
│  │ "Who crossed   │    │ "Which counties │   │ "When will   │  │
│  │  threshold     │    │  are affected?" │   │  it arrive?" │  │
│  │  first?"       │    │                 │   │              │  │
│  └────────────────┘    └─────────────────┘   └──────┬───────┘  │
│                                                       │          │
│  NOTE: This is pure arithmetic + table lookup.        │          │
│  No ML models. No training. No inference.             │          │
└───────────────────────────────────────────────────────┼──────────┘
                                                        │
                                                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                      GENERATION PLANE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Step Functions State Machine                                    │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                                                              │ │
│  │  ┌──────────┐    ┌──────────┐    ┌──────────────────────┐  │ │
│  │  │ Step 1   │───▶│ Step 2   │───▶│ Model Routing Choice │  │ │
│  │  │ Situation│    │ Severity │    │                      │  │ │
│  │  │ Brief    │    │ Classify │    │ HIGH/CRITICAL → ─────┼──┼─┤
│  │  │          │    │          │    │                 │    │  │ │
│  │  │ [Sonnet 4.5]  │    │ [Sonnet 4.5]  │    │ LOW/MOD → ─────┼────┼──┼─┤
│  │  └──────────┘    └──────────┘    └────────────────┘    │  │ │
│  │                                                         │  │ │
│  │  ┌──────────────────────┐    ┌──────────────────────┐  │  │ │
│  │  │ Step 3: Checklist    │    │ Step 3: Checklist    │◀─┘  │ │
│  │  │ [Sonnet 5 — urgent]    │    │ [Sonnet 4.5 — routine]    │◀────┘ │
│  │  └──────────┬───────────┘    └──────────┬───────────┘       │
│  │             │                           │                    │
│  │             └─────────┬─────────────────┘                    │
│  │                       ▼                                      │
│  │  ┌──────────────────────────────────────┐                   │
│  │  │ Step 4: Communication Drafting       │                   │
│  │  │ [Sonnet 4.5 — always]       │                   │
│  │  │ Output: email_body + sms_summary     │                   │
│  │  └──────────────────────────────────────┘                   │
│  │                                                              │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  Supporting Services:                                            │
│  ┌───────────────────┐  ┌─────────────────────────────────────┐ │
│  │ Bedrock Guardrails │  │ Knowledge Bases                     │ │
│  │ • No clinical Rx   │  │ • CDC Guidelines (precision, 6 docs)│ │
│  │ • No diagnoses     │  │ • Comms Templates (variety, 33 tmpl)│ │
│  │ • No quarantine    │  │                                     │ │
│  └───────────────────┘  └─────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       DELIVERY PLANE                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────────┐         ┌──────────────────┐             │
│  │ Alert Dispatcher │────────▶│ SES (Email)      │──▶ Officers  │
│  │ Lambda           │    │    └──────────────────┘             │
│  │                  │    │    ┌──────────────────┐             │
│  │ Queries          │────┼───▶│ SNS (SMS)        │──▶ Officers  │
│  │ subscriptions    │    │    └──────────────────┘             │
│  │ table for        │    │                                      │
│  │ active/verified  │    │    ┌──────────────────┐             │
│  │ contacts         │    └───▶│ Feedback API     │◀── Officers  │
│  └──────────────────┘         └──────────────────┘             │
│                                                                   │
│  ┌────────────────────────────────────────┐                     │
│  │  Subscription API (API Gateway)         │                     │
│  │  POST /subscribe    GET /verify         │                     │
│  │  POST /unsubscribe  PUT /preferences    │                     │
│  │  GET  /status                           │                     │
│  │  Double opt-in • Signed unsubscribe     │                     │
│  │  tokens • Pause/resume • Annual reverif │                     │
│  └────────────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

## Key Architecture Decisions

### Why Step Functions + InvokeModel (Not Bedrock Agent)?

| Consideration | Bedrock Agent | Step Functions + InvokeModel |
|---------------|---------------|------------------------------|
| Workflow type | Autonomous, agentic | Deterministic, orchestrated |
| Model control | Agent chooses | We route explicitly |
| Cost control | Unpredictable (agent may loop) | Fixed 4 calls per alert |
| Observability | Opaque agent reasoning | X-Ray traces each step |
| Error handling | Agent retry logic | Explicit Catch/Retry states |
| Model routing | Single model | Sonnet 4.5 all steps, Sonnet 5 for HIGH/CRIT checklist (conditional) |

**Decision:** Our workflow is deterministic — we always do exactly 4 steps in order. There's no "decide what to do next" logic. Step Functions gives us explicit control over model routing, cost, and observability.

### Why Two Knowledge Bases?

| KB | Content | Retrieval Strategy | Reason |
|----|---------|-------------------|--------|
| CDC Guidelines | 6 official guidance docs (43 KB) | Precision (Top-3, high threshold) | Need exact, authoritative facts |
| Comms Templates | 33 writing templates (49 KB) | Variety (Top-8, MMR diversity) | Need diverse stylistic input |

### Why Dynamic Data In-Context (Not in KB)?

Surveillance data (metro signals, thresholds, peaks) changes weekly. Knowledge Bases are designed for static reference content. We pass surveillance data directly in the prompt context, not retrieved from KB.

### Why SQS Between EventBridge and Lambdas?

| Without SQS | With SQS |
|-------------|----------|
| EventBridge directly invokes Lambda | EventBridge sends to SQS, SQS invokes Lambda |
| API failure = silent loss | API failure = automatic retry (3×) |
| No backpressure | Queue absorbs spikes |
| No failure forensics | DLQ retains failed messages 14 days |
| One failure blocks all | Each source independent |

### Why a Pipeline Coordinator (Not Direct S3→Step Functions)?

The prediction pipeline has **conditional fan-out** — it only proceeds if a threshold is crossed, and then fans out to N counties (variable). Step Functions can't conditionally start itself based on S3 events with dynamic iteration. The coordinator Lambda provides:
1. Conditional gating (only proceed if leader detected)
2. Dynamic fan-out (different number of counties each time)
3. Circuit breaker (safety: >20 counties = human review)
4. Idempotency (no re-alerting same season)
5. Observability (pipeline_runs table tracks every execution)

### Token Economics

Per county per alert cycle (4 InvokeModel calls):
```
Step 1 (Situation Brief):     ~1,500 input + 800 output = 2,300 tokens
Step 2 (Severity):            ~1,200 input + 200 output = 1,400 tokens  
Step 3 (Checklist):           ~2,000 input + 1,500 output = 3,500 tokens
Step 4 (Communication):       ~2,500 input + 1,500 output = 4,000 tokens
                                                           ──────────────
TOTAL per county per alert:                                ~11,200 tokens
```

At Claude Sonnet 4.5 pricing (~$3.00/$15.00 per MTok input/output):
- Per alert cycle: ~$0.08 (7,200 input × $3/MTok + 4,000 output × $15/MTok)
- 100 counties × 4 alerts/season: ~$32 in Bedrock costs
- Monthly (100 counties, active monitoring): $184–$358
- Monthly amortized: $1.84–$3.58 per county

### Model Routing Logic

```
IF severity == HIGH or CRITICAL:
    Step 3 uses Claude Sonnet 5 (us.anthropic.claude-sonnet-5)
    → More thorough checklist for urgent situations
    → ~10-15% of alerts (2-3 per season per county)
ELSE:
    Step 3 uses Claude Sonnet 4.5 (us.anthropic.claude-sonnet-4-5-20250929-v1:0)
    → Fast, adequate for routine LOW/MODERATE alerts
    → ~85-90% of alerts

Steps 1, 2, 4 always use Sonnet 4.5 regardless of severity.
```

## Data Flow (Weekly Cycle)

```
Monday 6 AM UTC:
  EventBridge → SQS queues (3 independent queues)
  
Monday 6:00-6:05 AM:
  SQS → triggers 3 Lambda fetchers (independent, isolated failures)
  Each fetcher: API call → store raw JSON to S3
  Failure: SQS retries up to 3×, then → DLQ (alarmed)
  
Monday 6:05 AM:
  S3 PutObject event (raw/delphi/*.json) → Pipeline Coordinator Lambda
  
Monday 6:06 AM:
  Pipeline Coordinator (orchestration):
    1. Load latest metro signals from S3
    2. Invoke leader_detection Lambda (synchronous)
    3. IF threshold crossed AND new_alert=True:
       → Invoke geographic_affinity → get affected counties
       → Invoke timing_estimation → get lag/severity per county
       → Circuit breaker check (>20 counties = flag for review)
       → For each affected county (async fan-out):
         → StartExecution on Step Functions state machine
       → Record execution to DynamoDB pipeline_runs
    4. ELSE:
       → Log "no detection" → wait until next week

Monday 6:08-6:20 AM:
  Step Functions (per county, parallel, independent):
    → InvokeModel × 4 (Bedrock: situation → severity → checklist → comms)
    → Alert Dispatcher:
      → Query subscriptions table (active + verified + not paused)
      → Filter by disease + severity threshold
      → SES email (with unsubscribe link + confidence disclaimer)
      → SNS SMS (≤160 chars)
      → Update last_alert_sent on subscription record

Monday 6:25 AM:
  Health officers receive alerts via email/SMS
  Total time from data fetch to delivery: ~20-25 minutes
```

## Security & Compliance Considerations

- **No PHI/PII stored**: System uses aggregate population signals, never individual patient data
- **Data sources are public**: No BAA required for API consumption
- **Guardrails enforce**: No clinical content in outputs
- **IAM least privilege**: Each Lambda has minimal permissions
- **Encryption**: S3 SSE, DynamoDB at-rest, SES TLS in-transit
- **Audit**: CloudTrail logs all API calls, X-Ray traces all executions
- **Subscription tokens**: HMAC-SHA256 signed, time-limited, stored in Secrets Manager

## Scalability

| Component | Pilot (5 counties) | Scale (500 counties) | Action Needed |
|-----------|--------------------|--------------------|---------------|
| Lambda concurrency | 1-3 | 50-100 | Request quota increase |
| DynamoDB | On-demand OK | On-demand OK | No change |
| Bedrock throughput | Default OK | May hit RPM limits | Request quota increase |
| Step Functions | Default OK | Default OK | No change |
| SES sending | Sandbox (50/day) | Production (50K/day) | Request production access |
| API Gateway | Default OK | Default OK | No change |
| SQS throughput | Default OK | Default OK | No change |
| Config loading | S3 GetObject (cached) | S3 GetObject (cached) | No change |

## Subscription System

Counties self-service via REST API (API Gateway → Lambda → DynamoDB):

```
POST /subscribe         → Create subscription (pending_verification)
GET  /verify?token=...  → Validate HMAC token, activate subscription
POST /unsubscribe       → Soft-delete (or GET with signed token from email)
PUT  /preferences       → Update diseases, channels, pause/resume
GET  /status            → Subscription health + recent alerts
```

Lifecycle: `pending_verification → active → paused ↔ active → inactive`

Every alert includes a signed unsubscribe link (HMAC-SHA256, 30-day expiry).
Annual re-verification ensures contact info stays current.

## Config-Driven Architecture

All operational parameters are externalized to `config/` (loaded from S3):

```
config/
├── system.json          # Tables, models, delivery, circuit breaker
├── data_sources/        # API endpoints, rate limits, field mappings
├── states/              # Per-state: metros, counties, overrides
│   ├── texas.json       # 4 metros, 3 sample counties
│   └── _template.json   # Copy to add new state
├── diseases/            # Per-disease: thresholds, signals, Socrata IDs
│   ├── influenza.json   # 1.0% threshold, ymmh-divb wastewater
│   ├── rsv.json         # 0.5% threshold, 45cq-cw4i wastewater  
│   ├── covid.json       # 0.3% threshold, 2ew6-ywp6 wastewater
│   └── _template.json   # Copy to add new disease
└── subscription_settings.json
```

The shared config_loader (`lambdas/shared/config_loader.py`):
- Loads from S3 in production, local filesystem for dev/test
- Caches in memory for Lambda warm-start reuse
- Validates required fields at load time

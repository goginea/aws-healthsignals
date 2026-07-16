# CDC Outbreak Alerts Module — Scope & Task List

**Module name:** `cdc_outbreak_alerts`
**Feature flag:** `enable_cdc_outbreak_alerts`
**Branch:** `feature/cdc-outbreak-alerts`

---

## 1. Data Source Analysis

### Best Approach: CDC Outbreaks RSS Feed

**URL:** `https://tools.cdc.gov/api/v2/resources/media/285676.rss`

This is the most reliable structured source for CDC outbreak notifications:

- Standard RSS 2.0 format (machine-parseable XML)
- Updated in near-real-time when CDC posts new outbreak notices
- Covers all CDC-tracked outbreaks: Cyclosporiasis, E. coli, Listeria, Salmonella, Botulism, etc.
- Each item includes: title, description, link to outbreak investigation page, pubDate, category
- Public, no auth required, no rate limits documented

**What the RSS provides:**

```xml
<item>
  <title>Cyclosporiasis Outbreak with Unknown Source</title>
  <description>Cyclosporiasis Outbreak with Unknown Source</description>
  <link>https://tools.cdc.gov/api/embed/downloader/download.asp?m=285676&c=765996</link>
  <pubDate>Tue, 14 Jul 2026 17:09:00 GMT</pubDate>
  <category>Outbreaks</category>
</item>
```

**What the RSS does NOT provide:**

- Affected states/counties (must be scraped from the linked outbreak investigation page)
- Case counts
- Severity level

**Supplementary source:** Each outbreak item links to a detailed investigation page (e.g., `cdc.gov/cyclosporiasis/outbreaks/07-26/index.html`) that contains:

- Affected states
- Case counts
- Onset dates
- Severity context

### Alternative Sources Considered

| Source                                  | Pros                                     | Cons                                                    | Decision                              |
| --------------------------------------- | ---------------------------------------- | ------------------------------------------------------- | ------------------------------------- |
| CDC Outbreaks RSS                       | Structured XML, near-real-time, reliable | No state/county data in feed itself                     | **Primary** — use as trigger          |
| CDC HAN RSS                             | Official alert channel                   | Feed appears stale (last build Mar 2025), less frequent | Skip                                  |
| CDC Outbreak investigation pages (HTML) | Has states, case counts, severity        | Unstructured HTML, requires scraping, fragile           | **Secondary** — scrape for enrichment |
| NORS Dashboard / data.cdc.gov           | Historical structured data               | Reporting lag (weeks/months), not real-time             | Skip for alerting                     |
| CDC Food Safety RSS                     | Includes FDA recalls                     | Mixed content (recalls + outbreaks)                     | Skip                                  |

### Chosen Architecture

```
CDC Outbreaks RSS (poll daily)
         │
         ▼
  Fetcher Lambda — parse RSS, detect new items
         │
         ├── Compare against DynamoDB state (known outbreaks)
         ├── For NEW items → fetch linked investigation page
         ├── Extract: affected states, case counts, disease name
         │
         ▼
  Outbreak Processor Lambda
         │
         ├── Skip leader-detection entirely
         ├── For each affected state → find subscribing counties
         ├── Start Step Functions → Bedrock generates alert brief
         │
         ▼
  Alert Dispatcher (via plugin registry)
```

---

## 2. Module Design

### Key Design Decisions

1. **No leader detection** — the CDC notification IS the detection. All mentioned states are treated as affected immediately.
2. **No timing estimation** — there's no lag to predict. The outbreak is already happening.
3. **No geographic affinity** — affected states are explicitly listed by CDC, not inferred.
4. **Daily polling** — RSS checked every 24 hours via EventBridge schedule.
5. **Change detection** — DynamoDB tracks known outbreaks. Only NEW items or UPDATED items (page content changed) trigger alerts.
6. **State-level alerting** — alert all subscribing counties in each affected state. If CDC mentions specific counties, alert only those.

### What Bedrock Generates

A situation brief tailored for foodborne outbreaks:

- What: disease name, source food (if known), symptoms
- Where: affected states
- Scale: case count, hospitalization count
- Action: food safety guidance, what to tell patients, reporting instructions
- Disclaimer: "Based on CDC public data. Monitor cdc.gov for updates."

---

## 3. File Structure (following ADDING_MODULES.md)

```
cdk/stacks/cdc_outbreak_alerts_stack.py          # Own CDK stack
lambdas/ingestion/cdc_outbreak_fetcher/handler.py # RSS fetcher
lambdas/orchestration/outbreak_processor/handler.py  # State extraction + alert routing
lambdas/delivery/alert_dispatcher/outbreak_dispatch.py  # Dispatch plugin
stepfunctions/outbreak_alert_generation.asl.json  # Own Bedrock generation workflow
config/data_sources/cdc_outbreaks_rss.json        # RSS endpoint config
config/alert_categories.json                      # Add foodborne categories
tests/unit/test_cdc_outbreak_fetcher.py
tests/unit/test_outbreak_processor.py
tests/unit/test_outbreak_dispatch.py
```

---

## 4. Task List

### Task Group 1: Configuration

- [ ] **1.1** Create `config/data_sources/cdc_outbreaks_rss.json` — RSS URL, poll frequency (daily), retry settings
- [ ] **1.2** Add foodborne categories to `config/alert_categories.json` (e.g., "foodborne_general", "salmonella", "ecoli", "listeria", "cyclospora")

### Task Group 2: DynamoDB State Table

- [ ] **2.1** Define DynamoDB table `healthsignals-cdc-outbreak-state` in CDK — tracks known outbreak items (PK: outbreak_id, attributes: title, pubDate, last_checked, status, affected_states, case_count)

### Task Group 3: RSS Fetcher Lambda

- [ ] **3.1** Create `lambdas/ingestion/cdc_outbreak_fetcher/handler.py` — polls RSS feed, parses XML, compares against DynamoDB state, identifies NEW/UPDATED outbreaks
- [ ] **3.2** For each new/updated outbreak: fetch the linked investigation page HTML content (text extraction, strip tags)
- [ ] **3.3** Invoke Bedrock to extract structured data from page content: affected states, case count, hospitalizations, source food, onset date, severity indicators
- [ ] **3.4** Store parsed outbreak data in S3: `raw/cdc-outbreaks/{date}/{outbreak_id}.json`
- [ ] **3.5** Invoke outbreak processor Lambda with the structured data

### Task Group 4: Outbreak Processor Lambda

- [ ] **4.1** Create `lambdas/orchestration/outbreak_processor/handler.py` — reads parsed outbreak data, normalizes state names (lowercase mapping), resolves to subscribing counties
- [ ] **4.2** No leader detection, no affinity, no timing — maps CDC-stated states → counties from existing state configs
- [ ] **4.3** For updates: compares current affected_states against DynamoDB history, marks newly added states
- [ ] **4.4** Starts Step Functions with outbreak context: disease, affected_states, new_states, case_count, source_food, cdc_link, severity (from Bedrock classification)

### Task Group 5: Step Functions (own state machine)

- [ ] **5.1** Create `stepfunctions/outbreak_alert_generation.asl.json` — two Bedrock steps: (1) severity classification with grounding rules, (2) situation brief generation, then dispatch
- [ ] **5.2** Severity classification prompt: provide case count, state count, hospitalization data; Bedrock classifies as LOW/MODERATE/HIGH/CRITICAL using explicit thresholds in prompt
- [ ] **5.3** Brief generation prompt: foodborne-specific (food safety guidance, symptom awareness, reporting instructions, update history for re-alerts)

### Task Group 6: Dispatch Plugin

- [ ] **6.1** Create `lambdas/delivery/alert_dispatcher/outbreak_dispatch.py` with `register()` — handles `alert_type: "cdc_outbreak"`
- [ ] **6.2** Delivery routing: per-state subscription lookup — find all subscribing counties in affected states, send via email/SMS
- [ ] **6.3** No new GSI needed — reuses existing state-based county lookup from subscriptions table

### Task Group 7: CDK Stack

- [ ] **7.1** Create `cdk/stacks/cdc_outbreak_alerts_stack.py` — DynamoDB table, fetcher Lambda + EventBridge daily schedule, processor Lambda, Step Functions state machine, CloudWatch alarms
- [ ] **7.2** Own Bedrock IAM role for the state machine
- [ ] **7.3** Own CloudWatch dashboard + alarms (RSS fetch failures, processing errors)

### Task Group 8: app.py Registration

- [ ] **8.1** Add `enable_cdc_outbreak_alerts` feature flag to `cdk/cdk.json`
- [ ] **8.2** Register in `cdk/app.py`: feature flag, plugin table ARN, dispatch module name, plugin env vars, plugin GSI (if needed)
- [ ] **8.3** Conditional import and stack instantiation

### Task Group 9: Unit Tests

- [ ] **9.1** Test RSS parser — mock RSS XML, verify outbreak items extracted correctly
- [ ] **9.2** Test Bedrock extraction — mock Bedrock response, verify structured data parsed
- [ ] **9.3** Test change detection — new vs. known outbreaks, updated outbreak detection
- [ ] **9.4** Test state name normalization — "North Carolina" → "north carolina", edge cases
- [ ] **9.5** Test outbreak processor — state-to-county mapping, re-alert with new states highlighted
- [ ] **9.6** Test dispatch plugin — per-state subscriber lookup, delivery

### Task Group 10: Integration Test

- [ ] **10.1** Test against real CDC RSS feed — verify connectivity and parsing
- [ ] **10.2** Test against real outbreak page — verify state extraction

---

## 5. Key Differences from Drug Shortage Module

| Aspect                    | Drug Shortage                                  | CDC Outbreak Alerts                                      |
| ------------------------- | ---------------------------------------------- | -------------------------------------------------------- |
| Data source               | openFDA API (JSON, paginated)                  | CDC RSS feed (XML) + HTML scraping                       |
| Detection model           | Change detection (NEW/WORSENING/RESOLVED)      | New item detection (RSS pubDate)                         |
| Geographic model          | Therapeutic category → disease → counties      | Affected states listed by CDC → counties in those states |
| Prediction                | None (reactive)                                | None (reactive)                                          |
| Leader detection          | Skipped (own S3 trigger)                       | Skipped entirely                                         |
| Core pipeline interaction | Subscribes to EventBridge for combined signals | No interaction with core disease pipeline                |
| Alert frequency           | Weekly (openFDA polling)                       | Daily (RSS polling)                                      |
| Alert type                | "shortage" / "combined"                        | "cdc_outbreak"                                           |

---

## 6. Design Decisions (Resolved)

**1. Content extraction from CDC pages — Use Bedrock for parsing**

Instead of fragile regex/HTML scraping, invoke Bedrock to read the CDC outbreak page content and extract structured data (affected states, case counts, disease name, source food, onset date). This approach:

- Handles page layout changes gracefully (Bedrock understands context, not DOM structure)
- Extracts nuanced info (e.g., "more than 400 people" → case_count: 400+)
- Falls back cleanly if content is ambiguous (Bedrock says "unknown" rather than crashing)

The fetcher Lambda will fetch the raw HTML text content of the linked CDC page and pass it to Bedrock with a structured extraction prompt.

**2. State name normalization — Simple lowercase mapping**

CDC uses "Michigan", our config uses "michigan". The mapping is straightforward: `state_name.lower()`. For multi-word states like "North Carolina" → "north carolina". Build a hardcoded 50-state + DC + territories lookup table that maps various forms to our state keys. This is a one-time utility, not dynamic.

**3. Deduplication — Re-alert ALL states, highlight NEW additions**

When an outbreak is updated with new states:

- Re-alert ALL currently affected states (not just new ones)
- The generated brief clearly marks which states are newly added vs. previously reported
- Health officials can track outbreak magnitude and geographic spread over time
- DynamoDB state tracks `affected_states` history per outbreak to enable diff

**4. Severity classification — Bedrock classifies from context**

Use Bedrock to classify severity from the outbreak data (case count, hospitalization rate, spread speed, number of states affected). Bedrock has sufficient knowledge of public health severity frameworks. The prompt will include explicit classification criteria:

- LOW: <50 cases, 1-2 states, no hospitalizations reported
- MODERATE: 50-500 cases, 2-5 states, some hospitalizations
- HIGH: 500-1000 cases, 5+ states, significant hospitalizations
- CRITICAL: >1000 cases, 10+ states, deaths reported or rapid spread

This gives Bedrock grounding rules to prevent hallucination while allowing contextual judgment.

**5. Subscription model — Per-state (existing model)**

Users subscribe per-state, same as the disease module. "Alert me about outbreaks affecting Texas" = any CDC outbreak that mentions Texas triggers an alert to all subscribing counties in Texas. No per-outbreak-type filtering needed. This reuses the existing subscription table and state-based county lookup — no new GSI required.

---

_Estimated effort: 2-3 days_
_Zero core module changes required (verified against ADDING_MODULES.md checklist)_

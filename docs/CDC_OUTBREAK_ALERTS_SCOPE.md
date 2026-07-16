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

| Source | Pros | Cons | Decision |
|--------|------|------|----------|
| CDC Outbreaks RSS | Structured XML, near-real-time, reliable | No state/county data in feed itself | **Primary** — use as trigger |
| CDC HAN RSS | Official alert channel | Feed appears stale (last build Mar 2025), less frequent | Skip |
| CDC Outbreak investigation pages (HTML) | Has states, case counts, severity | Unstructured HTML, requires scraping, fragile | **Secondary** — scrape for enrichment |
| NORS Dashboard / data.cdc.gov | Historical structured data | Reporting lag (weeks/months), not real-time | Skip for alerting |
| CDC Food Safety RSS | Includes FDA recalls | Mixed content (recalls + outbreaks) | Skip |

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
- [ ] **3.2** For each new/updated outbreak: fetch the linked investigation page HTML, extract affected states and case counts using simple regex/string parsing
- [ ] **3.3** Store parsed outbreak data in S3: `raw/cdc-outbreaks/{date}/{outbreak_id}.json`
- [ ] **3.4** Trigger outbreak processor (direct Lambda invoke or S3 event)

### Task Group 4: Outbreak Processor Lambda

- [ ] **4.1** Create `lambdas/orchestration/outbreak_processor/handler.py` — reads parsed outbreak data, resolves affected states to subscribing counties, starts Step Functions for each state
- [ ] **4.2** No leader detection, no affinity, no timing — simply maps states → counties from existing state configs
- [ ] **4.3** Passes outbreak context to Step Functions: disease, affected_states, case_count, source_food, cdc_link, severity

### Task Group 5: Step Functions (own state machine)

- [ ] **5.1** Create `stepfunctions/outbreak_alert_generation.asl.json` — Bedrock generates foodborne outbreak situation brief, then dispatches
- [ ] **5.2** Bedrock prompt: foodborne-specific (food safety guidance, symptom awareness, reporting instructions, NOT clinical treatment)

### Task Group 6: Dispatch Plugin

- [ ] **6.1** Create `lambdas/delivery/alert_dispatcher/outbreak_dispatch.py` with `register()` — handles `alert_type: "cdc_outbreak"`
- [ ] **6.2** Delivery routing: find subscribers in affected states/counties, send via email/SMS

### Task Group 7: CDK Stack

- [ ] **7.1** Create `cdk/stacks/cdc_outbreak_alerts_stack.py` — DynamoDB table, fetcher Lambda + EventBridge daily schedule, processor Lambda, Step Functions state machine, CloudWatch alarms
- [ ] **7.2** Own Bedrock IAM role for the state machine
- [ ] **7.3** Own CloudWatch dashboard + alarms (RSS fetch failures, processing errors)

### Task Group 8: app.py Registration

- [ ] **8.1** Add `enable_cdc_outbreak_alerts` feature flag to `cdk/cdk.json`
- [ ] **8.2** Register in `cdk/app.py`: feature flag, plugin table ARN, dispatch module name, plugin env vars, plugin GSI (if needed)
- [ ] **8.3** Conditional import and stack instantiation

### Task Group 9: Unit Tests

- [ ] **9.1** Test RSS parser — mock RSS XML, verify outbreak items extracted
- [ ] **9.2** Test HTML scraper — mock investigation page, verify states/counts extracted
- [ ] **9.3** Test change detection — new vs. known outbreaks
- [ ] **9.4** Test outbreak processor — state-to-county mapping, SFN invocation
- [ ] **9.5** Test dispatch plugin — subscriber lookup, delivery

### Task Group 10: Integration Test

- [ ] **10.1** Test against real CDC RSS feed — verify connectivity and parsing
- [ ] **10.2** Test against real outbreak page — verify state extraction

---

## 5. Key Differences from Drug Shortage Module

| Aspect | Drug Shortage | CDC Outbreak Alerts |
|--------|--------------|---------------------|
| Data source | openFDA API (JSON, paginated) | CDC RSS feed (XML) + HTML scraping |
| Detection model | Change detection (NEW/WORSENING/RESOLVED) | New item detection (RSS pubDate) |
| Geographic model | Therapeutic category → disease → counties | Affected states listed by CDC → counties in those states |
| Prediction | None (reactive) | None (reactive) |
| Leader detection | Skipped (own S3 trigger) | Skipped entirely |
| Core pipeline interaction | Subscribes to EventBridge for combined signals | No interaction with core disease pipeline |
| Alert frequency | Weekly (openFDA polling) | Daily (RSS polling) |
| Alert type | "shortage" / "combined" | "cdc_outbreak" |

---

## 6. Open Questions for Implementation

1. **HTML scraping fragility** — CDC outbreak pages have no stable API. HTML structure can change. Should we build a resilient parser with fallbacks, or accept occasional failures and alert on parse errors?

2. **State name normalization** — CDC uses full state names ("Michigan", "Ohio"). Our config uses state keys ("michigan", "ohio"). Need a mapping utility.

3. **Deduplication** — if an outbreak is updated (e.g., new states added), should we re-alert all states or only the newly added ones?

4. **Severity classification** — CDC doesn't provide a machine-readable severity level. Should Bedrock classify it, or should we infer from case count thresholds?

5. **Subscription model** — should users subscribe per-state (as today for diseases) or per-outbreak-type (e.g., "alert me about all Salmonella outbreaks regardless of state")?

---

*Estimated effort: 2-3 days*
*Zero core module changes required (verified against ADDING_MODULES.md checklist)*

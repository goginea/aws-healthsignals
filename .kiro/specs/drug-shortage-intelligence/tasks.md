# Implementation Plan: Drug Shortage Intelligence Module

## Overview

This implementation plan extends Amazon HealthSignals with drug shortage monitoring capabilities using the openFDA Drug Shortages API. The module integrates seamlessly with existing CDK stacks and follows established patterns for config-driven design, weekly polling schedules, and Step Functions-based alert generation.

**Implementation Approach:**

- Python for all Lambda functions (matching existing HealthSignals codebase)
- Extend existing 7 CDK stacks rather than creating new infrastructure
- Reuse shared utilities (config_loader, S3 helpers, DynamoDB helpers)
- Integrate with existing EventBridge schedule, S3 data lake, and DynamoDB tables
- Follow established error handling and retry patterns

## Tasks

- [x] 1. Create configuration files for shortage monitoring
  - Create `config/data_sources/openfda_shortages.json` with API endpoint, rate limits, retry settings, and S3 storage prefix pattern
  - Create `config/shortage_monitoring/therapeutic_categories.json` defining monitored categories (Antivirals, Antibiotics, Respiratory, etc.) with FDA classification mappings, priority levels, and relevant disease associations
  - Include validation schemas for both configuration files
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

- [x] 2. Implement openFDA shortage fetcher Lambda function
  - [x] 2.1 Create Lambda handler with SQS trigger event parsing
    - Implement `lambdas/ingestion/openfda_shortage_fetcher/handler.py` with `lambda_handler(event, context)` function
    - Parse SQS message from EventBridge scheduler trigger
    - Load configuration from S3 using shared `config_loader` utility
    - Implement pagination logic to handle openFDA API limit (1000 records per request)
    - Implement retry logic with exponential backoff (5s, 10s, 20s) for HTTP 429/500/503 errors
    - Store raw API responses to S3 with prefix pattern `raw/openfda-shortages/{year}/W{week}/shortages_{timestamp}.json`
    - Emit CloudWatch metrics for records_fetched_count and api_success_rate
    - Return response with statusCode, s3_key, and records_fetched
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 8.1, 8.3, 8.5, 8.7_

  - [x] 2.2 Create openFDA response parser module
    - Implement `lambdas/ingestion/openfda_shortage_fetcher/parser.py` with `parse_openfda_response()` function
    - Extract fields: reportsReceived → product_id, productName → product_name (fallback to genericName), currentSupplyStatus → supply_status
    - Map supply status values: "Available" → AVAILABLE, "Discontinued" → DISCONTINUED, empty → UNKNOWN
    - Implement `infer_therapeutic_category()` using pattern matching against therapeutic category config mappings
    - Handle missing fields: skip records without product_id or product_name/genericName, log warnings
    - Assign "Uncategorized" to unmapped therapeutic categories and exclude from alert generation
    - Add current epiweek timestamp to each normalized record
    - _Requirements: 1.5, 4.3, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8_

  - [x]\* 2.3 Write unit tests for openFDA parser
    - Test normalization of valid openFDA API responses with all fields present
    - Test fallback from productName to genericName when productName missing
    - Test skipping records with missing product_id
    - Test therapeutic category inference from product names using config patterns
    - Test supply status mapping for all status values
    - Test handling of malformed JSON responses
    - _Requirements: 16.1, 16.3, 16.4, 16.5_

- [x] 3. Extend CDK Ingestion Stack with openFDA infrastructure
  - Modify `cdk/stacks/ingestion_stack.py` to add SQS queue `healthsignals-openfda-shortages-queue` with visibility timeout 300s, 3 retry attempts, and DLQ
  - Add Lambda function resource for openFDA fetcher with 256MB memory, 120s timeout, Python 3.11 runtime
  - Grant Lambda permissions: read S3 config prefix, write S3 raw data prefix, write CloudWatch Logs and metrics
  - Add EventBridge rule target to invoke openFDA fetcher Lambda via SQS on weekly schedule (Monday 6 AM UTC)
  - Configure Lambda environment variables: DATA_BUCKET, CONFIG_PREFIX, AWS_REGION
  - _Requirements: 9.1, 9.2, 9.3, 9.9, 15.8_

- [x] 4. Create DynamoDB tables for shortage state management
  - [x] 4.1 Add healthsignals-drug-shortage-state table to CDK Prediction Stack
    - Define table in `cdk/stacks/prediction_stack.py` with partition key `product_id` (String) and sort key `week_timestamp` (String)
    - Configure on-demand billing mode
    - Add attributes: product_name, therapeutic_category, supply_status, reason_for_shortage, estimated_resolution_date, shortage_status, previous_supply_status, created_at
    - Enable TTL on `ttl` attribute (52 weeks retention)
    - Enable encryption at rest with AWS managed keys
    - Enable CloudTrail logging for all table operations
    - _Requirements: 2.1, 2.4, 2.8, 9.4, 18.2, 18.6_

  - [x] 4.2 Add Global Secondary Index for therapeutic category queries
    - Create GSI `therapeutic-category-index` with partition key `therapeutic_category` and sort key `week_timestamp`
    - Project all attributes to GSI for querying all shortages by category in a specific week
    - _Requirements: 2.4, 9.5_

  - [x] 4.3 Add healthsignals-shortage-alerts table to CDK Prediction Stack
    - Define table with partition key `product_id` (String) and sort key `week_timestamp` (String)
    - Configure on-demand billing mode
    - Add attributes: therapeutic_category, shortage_status, detection_timestamp, alert_generated (PENDING|SENT|FAILED), step_function_execution_arn, delivery_timestamp, recipients_count, error_message, retry_count, created_at
    - Enable encryption at rest with AWS managed keys
    - _Requirements: 3.7, 12.1, 12.2, 12.3, 12.4, 12.5, 18.2_

- [x] 5. Checkpoint - Verify infrastructure deployment
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement shortage change detection Lambda function
  - [x] 6.1 Create shortage change detector Lambda handler
    - Implement `lambdas/prediction/shortage_change_detector/handler.py` with `lambda_handler(event, context)` function
    - Parse S3 PutObject event from Pipeline Coordinator with s3_key and week_timestamp
    - Load current shortage data from S3 using s3_key
    - Query DynamoDB shortage-state table for previous week records (week_timestamp - 1)
    - Implement `classify_changes()` function: NEW (not in DDB), WORSENING (status AVAILABLE→DISCONTINUED or reason changed), RESOLVED (in DDB but not current), UNCHANGED (same)
    - Load therapeutic category config and filter changes by monitored categories
    - Implement circuit breaker check: if NEW+WORSENING > 20, emit CloudWatch alarm and return without triggering alerts
    - Write change records to shortage-state table with current and previous_supply_status
    - Check idempotency: query shortage-alerts table to verify alert not already sent for (product_id, week_timestamp)
    - Write alert records to shortage-alerts table with status PENDING
    - Invoke Step Functions for each NEW/WORSENING shortage alert
    - Return summary with changes_detected counts by shortage_status and alerts_triggered count
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 12.2, 12.3, 12.5_

  - [x]\* 6.2 Write unit tests for change classification logic
    - Test NEW classification for product_id not in previous DynamoDB records
    - Test WORSENING classification for supply_status change AVAILABLE → DISCONTINUED
    - Test WORSENING classification for reason_for_shortage text change
    - Test RESOLVED classification for product_id in DDB but not in current data
    - Test UNCHANGED classification when all fields match previous week
    - Test therapeutic category filtering excludes "Uncategorized" records
    - Test circuit breaker activation when NEW+WORSENING count exceeds 20
    - _Requirements: 16.1, 16.3_

  - [x]\* 6.3 Write unit tests for idempotency logic
    - Test alert generation skipped when shortage-alerts record exists with status SENT for same product_id and week_timestamp
    - Test alert generation retried when record exists with status FAILED and retry_count < 3
    - Test alert record created with status PENDING before Step Functions invocation
    - _Requirements: 12.2, 12.3, 12.4, 12.5, 16.1_

- [x] 7. Extend CDK Prediction Stack with shortage change detector
  - Add Lambda function resource in `cdk/stacks/prediction_stack.py` with 512MB memory, 180s timeout, Python 3.11 runtime
  - Grant Lambda permissions: read S3 raw data prefix, read/write healthsignals-drug-shortage-state table, read/write healthsignals-shortage-alerts table, write CloudWatch Logs and metrics, invoke Step Functions
  - Configure Lambda environment variables: DATA_BUCKET, SHORTAGE_STATE_TABLE, SHORTAGE_ALERTS_TABLE, STEP_FUNCTION_ARN
  - Enable AWS X-Ray tracing for end-to-end request tracing
  - _Requirements: 9.6, 14.3, 18.1_

- [x] 8. Extend Pipeline Coordinator with shortage routing logic
  - [x] 8.1 Add shortage data source detection
    - Modify `lambdas/orchestration/pipeline_coordinator/handler.py` to detect openFDA shortage data from S3 key pattern `raw/openfda-shortages/`
    - Add routing logic: if source is openFDA, invoke shortage change detector Lambda
    - Pass s3_key and week_timestamp in event payload to shortage change detector
    - _Requirements: Extension of existing system for shortage path_

  - [x] 8.2 Implement shortage context enrichment for disease alerts
    - Add `query_shortage_context(disease_key)` function to query shortage-state table for relevant medications
    - Load therapeutic category config to map disease_key to therapeutic categories using relevant_diseases field
    - Query shortage-state GSI for current week records matching therapeutic categories with shortage_status NEW or WORSENING
    - When disease outbreak detected, check for relevant medication shortages before triggering Step Functions
    - If relevant shortages exist, enrich alert payload with shortage_context and set alert_type="combined"
    - If no shortages, set alert_type="disease_outbreak" and continue existing workflow
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [x]\* 8.3 Write unit tests for Pipeline Coordinator extensions
    - Test routing to shortage handler for S3 keys matching pattern `raw/openfda-shortages/`
    - Test shortage context query returns relevant medications for disease_key
    - Test combined signal payload includes disease_data and shortage_context
    - Test standard disease alert when no relevant shortages exist
    - _Requirements: 16.1_

- [x] 9. Extend CDK Orchestration Stack with shortage permissions
  - Modify `cdk/stacks/orchestration_stack.py` to grant Pipeline Coordinator Lambda read permissions for healthsignals-drug-shortage-state table and therapeutic-category-index GSI
  - Grant invoke permissions for shortage change detector Lambda
  - Add environment variable SHORTAGE_CHANGE_DETECTOR_FUNCTION for Lambda ARN
  - _Requirements: 9.7_

- [x] 10. Checkpoint - Verify shortage detection pipeline
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Create Bedrock prompts for shortage alert generation
  - [x] 11.1 Create shortage situation brief prompt
    - Write `bedrock/prompts/shortage_situation_brief.txt` with system instructions for Claude Sonnet 4.5
    - Structure brief with sections: Executive Summary, Affected Medications, Supply Status, Resolution Timeline, Pharmacist Actions
    - Include instruction: "Do NOT provide specific drug substitution recommendations or clinical guidance"
    - Include instruction: "Always include disclaimer: FOR PHARMACIST REVIEW ONLY"
    - Follow CDC CERC communication principles (clear, non-alarmist language)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x] 11.2 Create combined disease and shortage brief prompt
    - Write `bedrock/prompts/combined_disease_shortage_brief.txt` for enriched disease outbreak alerts
    - Integrate shortage information as subsection "Medication Availability Alert" within disease outbreak brief
    - Prioritize disease outbreak information with shortage context as supplemental preparedness intelligence
    - _Requirements: 10.6, 10.7, 10.8_

- [x] 12. Create Knowledge Base documents for shortage guidance
  - Create `bedrock/knowledge_bases/shortage_guidance/fda_shortage_protocols.md` with FDA guidance on managing drug shortages: conservation strategies, compounding alternatives, communication with prescribers, patient safety considerations
  - Create `bedrock/knowledge_bases/shortage_guidance/therapeutic_substitution_framework.md` with general frameworks for pharmacist review WITHOUT specifying exact drug-to-drug substitutions
  - Create `bedrock/knowledge_bases/shortage_guidance/inventory_management_strategies.md` with best practices for proactive shortage preparation
  - Keep total size under 30KB to minimize retrieval latency
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_

- [x] 13. Extend Step Functions workflow with shortage alert branching
  - [x] 13.1 Modify Step Functions state machine definition
    - Edit `stepfunctions/alert_generation.asl.json` to add "DetermineAlertType" Choice state at start
    - Add conditional branches for alert_type values: "disease_outbreak", "shortage", "combined"
    - Preserve existing "DiseaseOutbreakPath" states (SituationBrief, SeverityClassify, ChecklistGenerate, CommunicationDrafting)
    - Add new "ShortageAlertPath" parallel branch with states: ShortageSituationBrief, PharmacistActionGeneration, ShortageCommunicationDrafting
    - Add new "CombinedSignalPath" parallel branch with state: CombinedSituationBrief
    - All paths converge to existing "DispatchAlert" Lambda invoke state
    - _Requirements: Extension of existing alert generation workflow_

  - [x] 13.2 Configure Bedrock model invocations in state machine
    - Set modelId to "us.anthropic.claude-sonnet-4-5-20250929-v1:0" for all Bedrock invoke tasks
    - Reference prompt files using "file://" syntax: file://bedrock/prompts/shortage_situation_brief.txt
    - Configure Knowledge Base retrieval for shortage guidance documents with top-3 chunks and high relevance threshold
    - _Requirements: 5.2, 13.6, 13.8_

- [x] 14. Extend CDK Generation Stack with shortage alert support
  - Modify `cdk/stacks/generation_stack.py` to update Step Functions state machine definition with new conditional branching
  - Grant Step Functions IAM permissions to invoke Bedrock models for shortage prompts
  - Add Bedrock Knowledge Base permissions for retrieving shortage guidance documents
  - Enable X-Ray tracing on Step Functions for distributed tracing
  - _Requirements: 9.7, 14.3, 18.1_

- [x] 15. Extend Subscription API with therapeutic category support
  - [x] 15.1 Add GSI to subscriptions table for therapeutic category lookups
    - Modify `cdk/stacks/subscription_stack.py` to add GSI `therapeutic-category-lookup` to healthsignals-subscriptions table
    - Configure GSI with partition key `therapeutic_category` (String) and sort key `county_fips` (String)
    - Project all attributes for efficient alert routing queries
    - _Requirements: 7.8, 9.8_

  - [x] 15.2 Extend PUT /preferences endpoint for therapeutic category subscriptions
    - Modify `lambdas/subscription/update_preferences/handler.py` to accept `therapeutic_categories` array in request body
    - Load therapeutic category config from S3 and validate each category_key exists in config
    - Return HTTP 400 with error message if invalid category_key provided
    - Update DynamoDB subscriptions table: append new categories to therapeutic_categories array, remove duplicates
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 15.3 Extend GET /status endpoint to return therapeutic categories
    - Modify `lambdas/subscription/get_status/handler.py` to include `therapeutic_categories` field in response
    - _Requirements: 7.6_

  - [x]\* 15.4 Write unit tests for subscription API extensions
    - Test therapeutic_categories field validation against loaded config
    - Test HTTP 400 error returned for invalid category_key
    - Test preference update appends categories and removes duplicates
    - Test status endpoint includes therapeutic_categories in response
    - _Requirements: 16.1_

- [x] 16. Extend Alert Dispatcher with therapeutic category filtering
  - [x] 16.1 Add shortage alert filtering logic
    - Modify `lambdas/delivery/alert_dispatcher/handler.py` to detect alert_type "shortage" or "combined"
    - Extract therapeutic_category from alert payload
    - Query subscriptions table using GSI `therapeutic-category-lookup` with partition key therapeutic_category
    - Apply filter expression: status=active AND verified=true AND paused=false
    - For each matching subscription, send via SES email with unsubscribe link and "FOR PHARMACIST REVIEW ONLY" disclaimer
    - Send via SNS SMS if channel="sms" enabled and phone_number present (message ≤160 chars)
    - Update last_alert_sent timestamp in subscriptions table
    - Update shortage-alerts table record to status=SENT with delivery_timestamp and recipients_count
    - _Requirements: 5.5, 5.6, 5.7, 7.7, 12.6_

  - [x]\* 16.2 Write integration tests for subscription filtering
    - Create test subscriptions with different therapeutic_categories values
    - Trigger shortage alert generation for specific category
    - Verify Alert Dispatcher queries GSI correctly with therapeutic_category partition key
    - Verify only subscriptions matching therapeutic_category receive alerts
    - _Requirements: 16.2, 16.4_

- [x] 17. Extend CDK Delivery Stack with shortage alert support
  - Modify `cdk/stacks/delivery_stack.py` to grant Alert Dispatcher Lambda query permissions for therapeutic-category-lookup GSI
  - Grant read/write permissions for healthsignals-shortage-alerts table to update delivery status
  - _Requirements: 9.8_

- [x] 18. Add CloudWatch monitoring and observability
  - [x] 18.1 Implement CloudWatch metrics emission
    - Add metrics to openFDA fetcher Lambda: openfda_api_success_rate, openfda_rate_limit_errors, shortage_records_fetched_count
    - Add metrics to shortage change detector Lambda: shortage_changes_detected_count (with dimensions for shortage_status), shortage_alerts_generated_count, shortage_circuit_breaker_activations, therapeutic_category_distribution
    - Add metrics to Alert Dispatcher Lambda: shortage_alerts_delivered_count (with dimensions for alert_type)
    - Use namespace "HealthSignals/DrugShortages"
    - _Requirements: 14.1, 14.4_

  - [x] 18.2 Implement structured logging with JSON format
    - Configure all Lambda functions to emit JSON structured logs with fields: timestamp, level, function_name, trace_id, event_type, metadata
    - Add event types: openfda_api_request, openfda_api_response, shortage_change_detected, circuit_breaker_evaluated, alert_generated, subscription_matched, alert_delivered
    - Log at INFO level for: detected changes count by shortage_status, therapeutic categories affected, circuit breaker evaluations, Step Functions invocations, alert delivery success
    - Log at ERROR level for: openFDA API failures with status codes, DynamoDB throttling errors, Step Functions failures
    - _Requirements: 14.5, 14.6, 14.7, 14.8_

  - [x] 18.3 Create CloudWatch alarms for failure conditions
    - Add alarm `OpenFDAFetcherFailureRate` triggering when openfda_api_success_rate < 50% over 2 evaluation periods (10 min)
    - Add alarm `ShortageAlertsDLQ` triggering when shortage_alerts_dlq_message_count > 0
    - Add alarm `CircuitBreakerActivated` triggering when shortage_circuit_breaker_activations > 0
    - Add alarm `BedrockThrottling` triggering when bedrock_throttling_errors > 10 over 5 min
    - Configure SNS topic notifications to ops team for all alarms
    - _Requirements: 14.2, 15.3_

  - [x] 18.4 Extend CloudWatch dashboard with Drug Shortage Intelligence section
    - Modify existing `HealthSignals-Overview` dashboard to add new section
    - Add widget for OpenFDA API Health (success rate and rate limit errors)
    - Add widget for Shortage Changes Detected (sum over 1 hour periods)
    - Add widget for Shortage Alerts by Type (dimensions: alert_type="shortage" vs "combined")
    - Add log insights widget for Recent Shortage Changes (last 20 records with shortage_status NEW or WORSENING)
    - _Requirements: 14.5_

- [x] 19. Extend CDK Monitoring Stack with shortage observability
  - Modify `cdk/stacks/monitoring_stack.py` to create CloudWatch alarms for shortage monitoring
  - Extend existing dashboard definition with Drug Shortage Intelligence widgets
  - Configure SNS topic subscriptions for alarm notifications
  - _Requirements: 9.10_

- [x] 20. Checkpoint - Verify end-to-end alert generation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 21. Implement error handling and resilience patterns
  - [x] 21.1 Add retry logic for openFDA API failures
    - Implement exponential backoff retry in openFDA fetcher: 5s, 10s, 20s for HTTP 500/503 errors
    - Skip retry for HTTP 404 errors (log and exit since endpoint may have changed)
    - Send failed messages to DLQ after 3 retry attempts exhausted
    - Emit CloudWatch alarm when DLQ receives messages
    - _Requirements: 15.1, 15.2, 15.3_

  - [x] 21.2 Add DynamoDB throttling error handling
    - Implement exponential backoff with jitter for DynamoDB throttling errors (ProvisionedThroughputExceededException)
    - Retry up to 5 times with increasing delays
    - _Requirements: 15.4_

  - [x] 21.3 Add Bedrock throttling error handling
    - Implement SQS queue for delayed retry when Bedrock throttling errors occur in Step Functions
    - Set retry delay to 60s
    - _Requirements: 15.5_

  - [x] 21.4 Implement graceful degradation
    - If shortage change detection fails, ensure raw API data still stored to S3 for manual review
    - If therapeutic category config file missing or invalid, use default config monitoring only "Antivirals" and "Antibiotics" categories and log warning
    - _Requirements: 15.6, 15.7_

  - [x] 21.5 Configure Lambda timeout values
    - Set openFDA fetcher timeout to 120s
    - Set shortage change detector timeout to 180s
    - Set alert generator timeout to 300s
    - _Requirements: 15.8_

- [x]\* 22. Write integration tests for shortage ingestion flow
  - Mock openFDA API responses using test fixtures from `tests/data/openfda_mock_responses.json`
  - Trigger openFDA fetcher Lambda with test event
  - Verify S3 object written to correct prefix `raw/openfda-shortages/{year}/W{week}/`
  - Verify CloudWatch metrics emitted for records_fetched_count and api_success_rate
  - Test retry logic with HTTP 503 server error mock response
  - Test DLQ message delivery for failed API calls after 3 retries
  - _Requirements: 16.2, 16.3, 16.4_

- [x]\* 23. Write integration tests for shortage change detection flow
  - Seed DynamoDB shortage-state table with previous week test data
  - Upload test shortage data to S3 with S3 key pattern
  - Trigger shortage change detector Lambda with S3 PutObject event
  - Verify DynamoDB shortage-state table records written with correct shortage_status classifications
  - Verify shortage-alerts table records created for NEW and WORSENING shortages with status PENDING
  - Verify Step Functions executions started for NEW and WORSENING shortages
  - _Requirements: 16.2, 16.4_

- [x]\* 24. Write integration tests for combined signal generation
  - Seed shortage-state table with relevant medication shortages (Antivirals category, status NEW)
  - Trigger disease outbreak detection with test surveillance data
  - Verify Pipeline Coordinator queries shortage context using disease_key
  - Verify Step Functions receives combined payload with alert_type="combined" and shortage_context
  - Verify Bedrock receives both disease_data and shortage_context in combined brief generation
  - _Requirements: 16.2, 16.4_

- [x] 25. Create configuration validation script
  - Write `scripts/validate_shortage_config.py` to verify therapeutic_categories.json schema before deployment
  - Check all disease_key values in relevant_diseases exist in `config/diseases/` directory
  - Check all priority_level values are one of HIGH, MEDIUM, or LOW
  - Check fda_classification_mapping fields contain valid regex patterns
  - Run as pre-deployment validation step
  - _Requirements: 17.5_

- [x] 26. Create comprehensive documentation
  - [x] 26.1 Create configuration documentation
    - Write `docs/DRUG_SHORTAGE_CONFIGURATION.md` with JSON schema definitions for therapeutic_categories.json and openfda_shortages.json
    - Provide example configurations for adding new therapeutic category, mapping FDA classifications, configuring shortage-to-disease relationships
    - Explain field purposes: priority_level (HIGH/MEDIUM/LOW), relevant_diseases (array of disease_key values), fda_classification_mapping (regex patterns)
    - Include migration guide for adding shortage monitoring to existing HealthSignals deployment
    - Include troubleshooting section with common configuration errors and resolutions
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.6, 17.7_

  - [x] 26.2 Create manual testing documentation
    - Write `docs/DRUG_SHORTAGE_TESTING.md` with procedures for manually triggering shortage alerts
    - Include curl commands for subscribing to therapeutic categories via Subscription API
    - Document validation of subscription filtering by therapeutic category
    - Document testing of combined disease and shortage signals
    - _Requirements: 16.6, 16.7_

  - [x] 26.3 Create configuration template file
    - Write `config/shortage_monitoring/_template_therapeutic_category.json` with empty template structure for copying when adding new categories
    - _Requirements: 17.8_

- [x] 27. Final checkpoint - Complete deployment and verification
  - Deploy all CDK stacks to staging environment
  - Run configuration validation script
  - Execute manual test plan scenarios
  - Verify CloudWatch dashboard displays shortage metrics
  - Verify alarms trigger correctly for failure conditions
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for faster MVP delivery
- Each task references specific requirements from the requirements document for traceability
- The design uses Python for all Lambda functions, matching the existing HealthSignals codebase
- Checkpoints ensure incremental validation at logical breakpoints (infrastructure, detection pipeline, alert generation, deployment)
- All code integrates with existing HealthSignals patterns: config_loader from S3, shared DynamoDB helpers, existing EventBridge schedules
- Error handling follows established retry patterns with exponential backoff and DLQ for failures
- Monitoring uses existing CloudWatch dashboards extended with Drug Shortage Intelligence section

## Task Dependency Graph

```json
{
  "waves": [
    {
      "id": 0,
      "tasks": ["1", "11.1", "11.2", "12"]
    },
    {
      "id": 1,
      "tasks": ["2.1", "2.2"]
    },
    {
      "id": 2,
      "tasks": ["2.3", "3", "4.1", "4.2", "4.3"]
    },
    {
      "id": 3,
      "tasks": ["6.1"]
    },
    {
      "id": 4,
      "tasks": ["6.2", "6.3", "7"]
    },
    {
      "id": 5,
      "tasks": ["8.1", "8.2"]
    },
    {
      "id": 6,
      "tasks": ["8.3", "9"]
    },
    {
      "id": 7,
      "tasks": ["13.1", "13.2", "15.1", "15.2", "15.3"]
    },
    {
      "id": 8,
      "tasks": ["14", "15.4", "16.1"]
    },
    {
      "id": 9,
      "tasks": ["16.2", "17", "18.1", "18.2", "18.3", "18.4"]
    },
    {
      "id": 10,
      "tasks": ["19", "21.1", "21.2", "21.3", "21.4", "21.5"]
    },
    {
      "id": 11,
      "tasks": ["22", "23", "24", "25", "26.1", "26.2", "26.3"]
    }
  ]
}
```

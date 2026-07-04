# Requirements Document

## Introduction

The Drug Shortage Intelligence Module extends Amazon HealthSignals to monitor pharmaceutical supply chain disruptions using the openFDA Drug Shortages API. This Phase 1 MVP provides reactive monitoring (not predictive) to help rural health facilities prepare for medication availability challenges during disease outbreaks.

The module integrates seamlessly with the existing HealthSignals architecture: config-driven design, weekly polling, DynamoDB state management, Step Functions generation pipeline, and subscription API. It enables two alert types: (1) standalone shortage alerts when monitored therapeutic categories experience supply disruptions, and (2) enriched disease outbreak alerts that include drug availability context when relevant medications are in shortage.

This is a monitoring-only system. All outputs include the disclaimer "FOR PHARMACIST REVIEW ONLY" and never provide specific drug substitution recommendations.

## Glossary

- **Drug_Shortage_Module**: The new HealthSignals subsystem that monitors pharmaceutical supply disruptions
- **OpenFDA_API**: The FDA's public API for drug shortage data (approximately 1,638 active records)
- **Shortage_State**: The current status of drug shortages stored in DynamoDB for change detection
- **Therapeutic_Category**: Drug classifications (e.g., antivirals, antibiotics, respiratory medications) used for filtering
- **Shortage_Change_Detector**: Lambda function that compares current API state against historical DynamoDB records
- **Shortage_Status**: One of NEW, WORSENING, RESOLVED, or UNCHANGED
- **Bedrock_Generator**: Step Functions workflow that produces GenAI briefs using Claude Sonnet 4.5
- **Subscription_Filter**: Facility preferences for which therapeutic categories trigger shortage alerts
- **Combined_Signal**: A disease outbreak alert enriched with drug availability context
- **Config_Loader**: Shared utility that loads configuration from S3
- **Pipeline_Coordinator**: Lambda that orchestrates detection and alert generation workflows
- **Ingestion_Stack**: CDK stack containing data fetchers, SQS queues, and EventBridge schedules
- **Generation_Stack**: CDK stack containing Step Functions workflows and Bedrock IAM permissions
- **Subscription_API**: REST API allowing facilities to manage alert preferences

## Requirements

### Requirement 1: OpenFDA Drug Shortage Data Integration

**User Story:** As a system operator, I want to poll the openFDA Drug Shortages API weekly, so that the system maintains current pharmaceutical supply chain data.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL poll the openFDA Drug Shortages API endpoint on the same weekly schedule as existing disease surveillance fetchers
2. WHEN the weekly EventBridge schedule triggers, THE Drug_Shortage_Module SHALL fetch all active drug shortage records from the openFDA API
3. THE Drug*Shortage_Module SHALL store raw API responses to S3 following the existing pattern: raw/openfda-shortages/{year}/W{week}/shortages*{timestamp}.json
4. IF the openFDA API request fails, THEN THE Drug_Shortage_Module SHALL retry up to 3 times with exponential backoff before sending the message to the DLQ
5. THE Drug_Shortage_Module SHALL extract and normalize the following fields from each shortage record: product_name, therapeutic_category, current_supply_status, reason_for_shortage, estimated_resolution_date
6. THE Drug_Shortage_Module SHALL load configuration from S3 using the existing Config_Loader shared utility
7. WHEN the API response exceeds 30 seconds timeout, THE Drug_Shortage_Module SHALL log a timeout error and retry according to the SQS queue configuration

### Requirement 2: Historical Shortage State Management

**User Story:** As a change detection system, I want to maintain historical shortage state in DynamoDB, so that I can identify when shortages are NEW, WORSENING, or RESOLVED.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL create a DynamoDB table named healthsignals-drug-shortage-state with partition key product_id and sort key week_timestamp
2. WHEN new shortage data is fetched from openFDA, THE Drug_Shortage_Module SHALL compare each record against the most recent state in DynamoDB for that product
3. THE Drug_Shortage_Module SHALL classify each shortage record as one of: NEW, WORSENING, RESOLVED, or UNCHANGED based on comparison with previous week state
4. THE Drug_Shortage_Module SHALL store the following attributes for each shortage state record: product_id, product_name, therapeutic_category, supply_status, reason, estimated_resolution_date, week_timestamp, shortage_status, previous_supply_status
5. WHEN a shortage appears in the current week but not in DynamoDB, THE Drug_Shortage_Module SHALL classify it as NEW
6. WHEN a shortage supply_status changes from "Available" to "Discontinued" or reason_for_shortage changes, THE Drug_Shortage_Module SHALL classify it as WORSENING
7. WHEN a shortage appears in DynamoDB but not in the current week API response, THE Drug_Shortage_Module SHALL classify it as RESOLVED
8. THE Drug_Shortage_Module SHALL retain shortage state records in DynamoDB for at minimum 52 weeks for historical analysis

### Requirement 3: Change Detection Logic

**User Story:** As a monitoring system, I want to identify meaningful changes in drug shortage status, so that pharmacists receive relevant alerts rather than noise.

#### Acceptance Criteria

1. THE Shortage_Change_Detector SHALL identify shortage records with Shortage_Status equal to NEW, WORSENING, or RESOLVED
2. WHEN Shortage_Status equals NEW, THE Shortage_Change_Detector SHALL trigger shortage alert generation
3. WHEN Shortage_Status equals WORSENING, THE Shortage_Change_Detector SHALL trigger shortage alert generation
4. WHEN Shortage_Status equals RESOLVED, THE Shortage_Change_Detector SHALL trigger all-clear notification generation
5. WHEN Shortage_Status equals UNCHANGED, THE Shortage_Change_Detector SHALL skip alert generation for that record
6. THE Shortage_Change_Detector SHALL filter shortage records by the therapeutic categories configured in config/shortage_monitoring/therapeutic_categories.json before triggering alerts
7. THE Shortage_Change_Detector SHALL write detected changes to DynamoDB table healthsignals-shortage-alerts with attributes: alert_id, product_id, therapeutic_category, shortage_status, detection_timestamp, alert_generated
8. WHEN more than 20 NEW or WORSENING shortages are detected in a single week, THE Shortage_Change_Detector SHALL log a circuit breaker warning and require manual review before generating alerts

### Requirement 4: Therapeutic Category Configuration

**User Story:** As a system administrator, I want to configure which therapeutic categories are monitored, so that the system focuses on medications relevant to public health preparedness.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL load therapeutic category configuration from S3 path config/shortage_monitoring/therapeutic_categories.json
2. THE configuration file SHALL define at minimum the following fields for each therapeutic category: category_key, display_name, fda_classification_mapping, priority_level, relevant_diseases
3. THE Drug_Shortage_Module SHALL map openFDA therapeutic classifications to internal category_key values using the fda_classification_mapping field
4. WHEN a shortage record therapeutic classification does not match any configured category, THE Drug_Shortage_Module SHALL classify it as "Other" and exclude it from alert generation
5. THE Drug_Shortage_Module SHALL support priority_level values of HIGH, MEDIUM, and LOW for routing alert severity
6. THE relevant_diseases field SHALL contain an array of disease_key values from existing disease configurations to enable combined signal generation
7. THE Drug_Shortage_Module SHALL validate the configuration file schema on Lambda cold start and log errors if required fields are missing

### Requirement 5: Standalone Shortage Alert Generation

**User Story:** As a pharmacist, I want to receive alerts when medications in my subscribed therapeutic categories experience shortages, so that I can proactively manage inventory and patient care.

#### Acceptance Criteria

1. WHEN Shortage_Change_Detector identifies a NEW or WORSENING shortage in a monitored therapeutic category, THE Drug_Shortage_Module SHALL invoke the Bedrock_Generator Step Functions workflow
2. THE Bedrock_Generator SHALL use Claude Sonnet 4.5 model (us.anthropic.claude-sonnet-4-5-20250929-v1:0) for all shortage alert generation steps
3. THE Bedrock_Generator SHALL produce a shortage situation brief containing: affected medications list, supply status summary, estimated resolution timeline, therapeutic category, reason for shortage
4. THE Bedrock_Generator SHALL include the disclaimer "FOR PHARMACIST REVIEW ONLY — No specific drug substitution recommendations provided" in all shortage alerts
5. THE Bedrock_Generator SHALL query the subscriptions DynamoDB table for active subscriptions matching the therapeutic_category
6. WHEN no active subscriptions exist for the therapeutic category, THE Bedrock_Generator SHALL log the event and skip email/SMS delivery
7. THE shortage alert SHALL contain: brief title, affected drug names, therapeutic category, current status, reason for shortage, estimated resolution (if available), pharmacist action recommendations (inventory review, communication with prescribers)
8. THE Bedrock_Generator SHALL deliver shortage alerts via SES email to subscribed contacts with unsubscribe link and via SNS SMS if SMS delivery is enabled in subscription preferences

### Requirement 6: Combined Disease Outbreak and Shortage Signals

**User Story:** As a rural health officer, I want disease outbreak alerts to include drug availability context, so that I understand supply chain constraints when preparing for patient surges.

#### Acceptance Criteria

1. WHEN the existing disease outbreak Pipeline_Coordinator detects a leader threshold crossing, THE Drug_Shortage_Module SHALL check if any relevant medications for that disease are in shortage
2. THE Drug_Shortage_Module SHALL determine medication relevance by matching the disease_key from the outbreak against the relevant_diseases array in therapeutic category configuration
3. IF relevant medications are in shortage status NEW or WORSENING, THEN THE Drug_Shortage_Module SHALL enrich the disease outbreak alert payload with shortage context
4. THE enriched alert payload SHALL include: disease_name, affected_medications array, shortage_summary, therapeutic_categories_affected
5. THE Bedrock_Generator SHALL incorporate shortage context into the existing disease outbreak situation brief generation step
6. THE enriched situation brief SHALL contain a dedicated section titled "Medication Availability Alert" when shortage context is present
7. WHEN no relevant medication shortages exist for the detected disease outbreak, THE Bedrock_Generator SHALL generate the disease outbreak alert without shortage context using the existing workflow
8. THE enriched alert SHALL maintain the existing disease outbreak alert structure and only add shortage information as a supplemental section

### Requirement 7: Subscription Extensions for Shortage Alerts

**User Story:** As a facility administrator, I want to subscribe to drug shortage alerts and filter by therapeutic categories, so that I only receive notifications relevant to my facility needs.

#### Acceptance Criteria

1. THE Subscription_API SHALL extend the existing subscriptions DynamoDB table schema to include a therapeutic_categories field containing an array of category_key values
2. THE Subscription_API PUT /preferences endpoint SHALL accept a therapeutic_categories parameter allowing subscribers to add or remove therapeutic category subscriptions
3. WHEN a subscription record is created without therapeutic_categories specified, THE Subscription_API SHALL default to an empty array indicating no shortage alert subscriptions
4. THE Subscription_API SHALL validate that each category_key in the therapeutic_categories array exists in the loaded therapeutic category configuration
5. IF an invalid category_key is provided, THEN THE Subscription_API SHALL return HTTP 400 with error message identifying the invalid category
6. THE Subscription_API GET /status endpoint SHALL return the current therapeutic_categories subscriptions in the response payload
7. THE Alert_Dispatcher SHALL filter shortage alerts to only send to subscriptions where the shortage therapeutic_category matches a value in the subscription therapeutic_categories array
8. THE Subscription_API SHALL support querying subscriptions by therapeutic_category using a GSI (Global Secondary Index) with partition key therapeutic_category for efficient alert routing

### Requirement 8: OpenFDA API Configuration

**User Story:** As a system operator, I want openFDA API configuration to follow existing data source patterns, so that the shortage data integration is consistent with disease surveillance data sources.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL define openFDA API configuration in file config/data_sources/openfda_shortages.json
2. THE configuration file SHALL include fields: source_name, display_name, description, enabled, api.base_url, api.auth_type, api.rate_limit_per_hour, api.timeout_seconds, api.retry_max_attempts, s3_storage.prefix_pattern
3. THE api.base_url field SHALL contain value "https://api.fda.gov/drug/shortages.json"
4. THE api.auth_type field SHALL contain value "none" since openFDA API does not require authentication
5. THE api.rate_limit_per_hour field SHALL contain value 240 to respect openFDA rate limit of 240 requests per hour per IP address
6. THE Drug_Shortage_Module SHALL load this configuration using the existing Config_Loader shared utility on Lambda cold start
7. THE configuration SHALL define s3*storage.prefix_pattern as "raw/openfda-shortages/{year}/W{week}/shortages*{timestamp}.json"
8. THE Drug_Shortage_Module SHALL validate required configuration fields on load and raise an exception if base_url or timeout_seconds are missing

### Requirement 9: CDK Infrastructure Stack Integration

**User Story:** As a DevOps engineer, I want the Drug Shortage Module infrastructure to integrate with existing CDK stacks, so that deployment follows established HealthSignals patterns.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL add openFDA shortage fetcher Lambda to the existing Ingestion_Stack CDK stack definition
2. THE Ingestion_Stack SHALL create an SQS queue named healthsignals-openfda-shortages-queue with visibility timeout 300 seconds and 3 retry attempts before DLQ
3. THE Ingestion_Stack SHALL create an EventBridge rule that invokes the openFDA fetcher on the same weekly schedule as existing disease surveillance fetchers (Monday 6 AM UTC)
4. THE Drug_Shortage_Module SHALL add DynamoDB table healthsignals-drug-shortage-state to the existing Prediction_Stack CDK stack definition with on-demand billing mode
5. THE Prediction_Stack SHALL create a GSI named therapeutic-category-index on healthsignals-drug-shortage-state with partition key therapeutic_category for filtering
6. THE Drug_Shortage_Module SHALL add shortage change detection Lambda to the Prediction_Stack with IAM permissions to read from S3 raw data prefix and write to DynamoDB shortage state table
7. THE Generation_Stack SHALL extend the existing Step Functions state machine to include conditional branching for shortage alert generation
8. THE Subscription_Stack SHALL add GSI named therapeutic-category-lookup to healthsignals-subscriptions table with partition key therapeutic_category for alert routing
9. THE Drug_Shortage_Module SHALL create Lambda layers for shared shortage processing utilities that can be reused across fetcher, detector, and alert generation functions

### Requirement 10: Bedrock Prompt Engineering for Shortage Alerts

**User Story:** As a content quality manager, I want shortage alerts to follow CDC communication principles and avoid clinical recommendations, so that alerts are informative but not prescriptive.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL create a Bedrock prompt file at bedrock/prompts/shortage_situation_brief.txt for generating shortage situation briefs
2. THE prompt SHALL instruct the model to structure shortage briefs with sections: Executive Summary, Affected Medications, Supply Status, Resolution Timeline, Pharmacist Actions
3. THE prompt SHALL include the instruction "Do NOT provide specific drug substitution recommendations or clinical guidance"
4. THE prompt SHALL include the instruction "Always include disclaimer: FOR PHARMACIST REVIEW ONLY"
5. THE prompt SHALL instruct the model to use clear, non-alarmist language consistent with CDC CERC communication principles
6. THE Drug_Shortage_Module SHALL create a Bedrock prompt file at bedrock/prompts/combined_disease_shortage_brief.txt for enriched disease outbreak alerts
7. THE combined alert prompt SHALL instruct the model to integrate shortage information as a subsection within the existing disease outbreak brief structure
8. THE combined alert prompt SHALL prioritize disease outbreak information and present shortage context as supplemental preparedness intelligence

### Requirement 11: OpenFDA API Response Parser

**User Story:** As a data engineer, I want to parse openFDA API responses into normalized shortage records, so that downstream change detection and alert generation functions receive consistent data structures.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL create a Parser component that transforms openFDA JSON responses into normalized shortage record objects
2. THE Parser SHALL extract the following fields from openFDA records: reportsReceived (mapped to product_id), productName (mapped to product_name), currentSupplyStatus (mapped to supply_status), reason (mapped to reason_for_shortage), estimatedResolutionDate (mapped to estimated_resolution_date)
3. WHEN an openFDA record is missing the productName field, THE Parser SHALL use the genericName field as fallback
4. WHEN an openFDA record is missing both productName and genericName, THE Parser SHALL log a warning and skip that record
5. THE Parser SHALL map openFDA currentSupplyStatus values to internal status codes: "Available" maps to AVAILABLE, "Discontinued" maps to DISCONTINUED, empty or missing maps to UNKNOWN
6. THE Parser SHALL infer therapeutic_category by pattern matching against the productName or genericName using the therapeutic category configuration mappings
7. WHEN therapeutic category cannot be inferred, THE Parser SHALL assign value "Uncategorized" and exclude from alert generation
8. THE Parser SHALL output normalized records as Python dictionaries with consistent field names for storage in DynamoDB

### Requirement 12: Shortage Alert Idempotency

**User Story:** As a system reliability engineer, I want shortage alert generation to be idempotent, so that subscribers do not receive duplicate alerts for the same shortage event.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL record alert generation events in DynamoDB table healthsignals-shortage-alerts with composite key: product_id (partition key) and week_timestamp (sort key)
2. THE Shortage_Change_Detector SHALL check healthsignals-shortage-alerts table before triggering Bedrock_Generator to determine if an alert was already generated for this product_id in the current week
3. WHEN an alert record exists for product_id and current week_timestamp with alert_generated status SENT, THE Shortage_Change_Detector SHALL skip alert generation
4. WHEN an alert record exists with alert_generated status FAILED, THE Shortage_Change_Detector SHALL retry alert generation one time
5. THE Shortage_Change_Detector SHALL write alert records with initial status PENDING before invoking Bedrock_Generator
6. WHEN Bedrock_Generator successfully delivers alerts, THE Alert_Dispatcher SHALL update the shortage-alerts record to status SENT with delivery_timestamp
7. IF Bedrock_Generator fails after 3 retry attempts, THE system SHALL update the shortage-alerts record to status FAILED with error_message
8. THE Drug_Shortage_Module SHALL support manual alert regeneration by deleting the shortage-alerts record for a specific product_id and week combination

### Requirement 13: Knowledge Base Content for Shortage Guidance

**User Story:** As a content curator, I want Bedrock knowledge bases to include pharmaceutical supply chain guidance, so that generated shortage alerts provide actionable context.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL create a knowledge base document at bedrock/knowledge_bases/shortage_guidance/fda_shortage_protocols.md
2. THE document SHALL contain FDA guidance on managing drug shortages including: conservation strategies, compounding alternatives, communication with prescribers, patient safety considerations
3. THE Drug_Shortage_Module SHALL create a knowledge base document at bedrock/knowledge_bases/shortage_guidance/therapeutic_substitution_framework.md
4. THE therapeutic substitution document SHALL provide general frameworks for pharmacist review of alternatives WITHOUT specifying exact drug-to-drug substitutions
5. THE Drug_Shortage_Module SHALL create knowledge base document at bedrock/knowledge_bases/shortage_guidance/inventory_management_strategies.md containing best practices for proactive shortage preparation
6. THE Bedrock_Generator Step Functions workflow SHALL retrieve relevant shortage guidance from knowledge bases using precision retrieval (top-3 chunks, high relevance threshold)
7. THE knowledge base documents SHALL total under 30 KB to minimize retrieval latency
8. THE Drug_Shortage_Module SHALL configure knowledge base retrieval to prioritize fda_shortage_protocols.md for regulatory compliance and patient safety sections

### Requirement 14: Monitoring and Observability

**User Story:** As a site reliability engineer, I want comprehensive monitoring for the Drug Shortage Module, so that I can detect and respond to integration failures quickly.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL emit CloudWatch metrics for: openfda_api_success_rate, shortage_records_fetched_count, shortage_changes_detected_count, shortage_alerts_generated_count, shortage_alerts_delivered_count
2. THE Drug_Shortage_Module SHALL create CloudWatch alarms for: openfda_fetcher_failure_rate exceeds 50 percent over 2 evaluation periods, shortage_alerts_dlq_message_count exceeds 0
3. THE Drug_Shortage_Module SHALL enable AWS X-Ray tracing on all Lambda functions for end-to-end request tracing from API fetch through alert delivery
4. WHEN the openFDA API returns HTTP 429 rate limit error, THE Drug_Shortage_Module SHALL log the error with ERROR severity and increment metric openfda_rate_limit_errors
5. THE Drug_Shortage_Module SHALL add shortage module metrics to the existing HealthSignals CloudWatch dashboard with dedicated section titled "Drug Shortage Intelligence"
6. THE shortage change detection Lambda SHALL log at INFO level: detected changes count by shortage_status (NEW/WORSENING/RESOLVED), therapeutic categories affected, circuit breaker evaluations
7. THE Bedrock_Generator SHALL log at INFO level: shortage alert generation start, model invocation latency, alert delivery success per channel (email/SMS)
8. THE Drug_Shortage_Module SHALL configure structured logging using JSON format with fields: timestamp, level, function_name, trace_id, event_type, metadata

### Requirement 15: Error Handling and Resilience

**User Story:** As a system reliability engineer, I want robust error handling across the Drug Shortage Module, so that transient failures do not prevent shortage monitoring.

#### Acceptance Criteria

1. WHEN the openFDA API returns HTTP 500 or 503 errors, THE Drug_Shortage_Module SHALL retry the request with exponential backoff: 5 seconds, 10 seconds, 20 seconds
2. WHEN the openFDA API returns HTTP 404 error, THE Drug_Shortage_Module SHALL log the error and skip retry since the endpoint may have changed
3. IF all retry attempts are exhausted for the openFDA fetcher, THEN THE Drug_Shortage_Module SHALL send the failed message to the DLQ and emit a CloudWatch alarm
4. WHEN DynamoDB throttling errors occur during shortage state writes, THE Drug_Shortage_Module SHALL use exponential backoff with jitter and retry up to 5 times
5. WHEN the Shortage_Change_Detector invokes Bedrock_Generator and receives a throttling error, THE system SHALL place the alert request in an SQS queue for delayed retry after 60 seconds
6. THE Drug_Shortage_Module SHALL implement graceful degradation: if change detection fails, the system SHALL still store raw API data to S3 for manual review
7. WHEN the therapeutic category configuration file is missing or invalid, THE Drug_Shortage_Module SHALL use a default configuration monitoring only "Antivirals" and "Antibiotics" categories and log a warning
8. THE Drug_Shortage_Module SHALL set Lambda function timeout values: fetcher 120 seconds, change detector 180 seconds, alert generator 300 seconds to prevent indefinite hangs

### Requirement 16: Testing and Validation Requirements

**User Story:** As a quality assurance engineer, I want comprehensive testing for the Drug Shortage Module, so that integration with HealthSignals is reliable.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL include unit tests for: openFDA response parser with valid and malformed JSON inputs, shortage state comparison logic for all status transitions (NEW/WORSENING/RESOLVED/UNCHANGED), therapeutic category mapping with edge cases
2. THE Drug_Shortage_Module SHALL include integration tests for: end-to-end flow from openFDA API mock through alert delivery, DynamoDB state persistence and retrieval, S3 raw data storage with correct prefix patterns
3. THE integration tests SHALL mock the openFDA API responses to avoid external dependencies during CI/CD pipeline execution
4. THE Drug_Shortage_Module SHALL include test fixtures in tests/data/openfda_mock_responses.json containing: sample responses with NEW shortages, sample responses with RESOLVED shortages, sample responses with malformed data
5. THE unit tests SHALL achieve at minimum 80 percent code coverage for Lambda function business logic
6. THE Drug_Shortage_Module SHALL include a manual test plan in docs/DRUG_SHORTAGE_TESTING.md documenting: how to trigger shortage alerts manually, how to validate subscription filtering by therapeutic category, how to test combined disease and shortage signals
7. THE testing documentation SHALL include example curl commands for invoking the Subscription_API to add therapeutic category subscriptions
8. THE Drug_Shortage_Module SHALL include validation scripts in scripts/validate_shortage_config.py to verify therapeutic category configuration schema before deployment

### Requirement 17: Configuration Schema Documentation

**User Story:** As a system administrator, I want clear documentation of all configuration schemas, so that I can correctly configure therapeutic categories and alert routing.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL create documentation file docs/DRUG_SHORTAGE_CONFIGURATION.md
2. THE documentation SHALL include JSON schema definitions for: therapeutic_categories.json structure, openfda_shortages.json data source config, shortage alert subscription preferences schema
3. THE documentation SHALL provide example configurations for: adding a new therapeutic category, mapping FDA classifications to internal categories, configuring shortage-to-disease relationships
4. THE documentation SHALL explain the field purpose for: priority_level (HIGH/MEDIUM/LOW routing), relevant_diseases (array of disease_key values for combined signals), fda_classification_mapping (string pattern matching rules)
5. THE Drug_Shortage_Module SHALL include configuration validation logic that checks: all disease_key values in relevant_diseases exist in config/diseases/, all priority_level values are one of HIGH/MEDIUM/LOW, fda_classification_mapping contains valid regex patterns
6. THE documentation SHALL include a migration guide section explaining how to add shortage monitoring to an existing HealthSignals deployment
7. THE documentation SHALL include a troubleshooting section with common configuration errors and resolutions
8. THE Drug_Shortage_Module SHALL include a configuration template file at config/shortage_monitoring/\_template_therapeutic_category.json for copying when adding new categories

### Requirement 18: Security and Compliance

**User Story:** As a security engineer, I want the Drug Shortage Module to maintain the same security posture as existing HealthSignals components, so that no new vulnerabilities are introduced.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL use IAM roles with least privilege permissions for: Lambda execution (read S3 config, write S3 raw data, read/write DynamoDB, invoke Bedrock), Step Functions execution (invoke Lambda, invoke Bedrock, write CloudWatch logs)
2. THE Drug_Shortage_Module SHALL enable encryption at rest for: DynamoDB table healthsignals-drug-shortage-state using AWS managed keys, S3 objects in raw/openfda-shortages prefix using SSE-S3
3. THE Drug_Shortage_Module SHALL enable encryption in transit by using HTTPS for all openFDA API requests
4. THE Drug_Shortage_Module SHALL not store or log any Protected Health Information (PHI) since openFDA data contains only aggregate drug shortage information
5. THE subscription therapeutic_categories field SHALL be stored in DynamoDB with the same encryption and access controls as existing subscription fields
6. THE Drug_Shortage_Module SHALL enable AWS CloudTrail logging for all DynamoDB table operations and S3 object writes
7. THE Drug_Shortage_Module SHALL not require any additional IAM permissions beyond those already granted to existing HealthSignals stacks
8. THE Drug_Shortage_Module SHALL validate all input from openFDA API responses to prevent injection attacks before storing to DynamoDB or passing to Bedrock

### Requirement 19: Phase 1 Scope Limitations

**User Story:** As a product manager, I want to clearly document what is NOT included in Phase 1 MVP, so that stakeholders have correct expectations.

#### Acceptance Criteria

1. THE Phase 1 Drug_Shortage_Module SHALL NOT provide predictive shortage forecasting or machine learning models
2. THE Phase 1 Drug_Shortage_Module SHALL NOT provide specific drug-to-drug substitution recommendations
3. THE Phase 1 Drug_Shortage_Module SHALL NOT integrate with pharmacy inventory management systems or EMR systems
4. THE Phase 1 Drug_Shortage_Module SHALL NOT provide real-time shortage monitoring (limited to weekly polling aligned with existing HealthSignals schedule)
5. THE Phase 1 Drug_Shortage_Module SHALL NOT include shortage severity scoring beyond the classifications provided by openFDA API
6. THE Phase 1 Drug_Shortage_Module SHALL NOT support user-initiated shortage searches or queries (push alerts only)
7. THE documentation SHALL clearly state in all generated alerts: "This is reactive monitoring only based on FDA reported shortages"
8. THE documentation SHALL include a Phase 2 roadmap section in docs/DRUG_SHORTAGE_CONFIGURATION.md outlining potential future enhancements: predictive shortage modeling, real-time API webhooks, pharmacy system integrations, interactive shortage dashboard

### Requirement 20: Deployment and Rollback Strategy

**User Story:** As a DevOps engineer, I want a safe deployment strategy for the Drug Shortage Module, so that existing HealthSignals functionality is not disrupted.

#### Acceptance Criteria

1. THE Drug_Shortage_Module SHALL be deployable as an incremental update to existing HealthSignals CDK stacks without requiring redeployment of unmodified stacks
2. THE deployment SHALL create new resources (DynamoDB tables, Lambdas, SQS queues) without modifying existing disease surveillance resources
3. THE Drug_Shortage_Module SHALL include a feature flag in config/system.json: shortage_monitoring.enabled (boolean) to enable or disable the module without redeployment
4. WHEN shortage_monitoring.enabled is false, THE EventBridge schedule for openFDA fetcher SHALL not trigger and no shortage alerts SHALL be generated
5. THE Drug_Shortage_Module SHALL include rollback documentation in docs/DEPLOYMENT.md explaining: how to disable the module via feature flag, how to remove infrastructure using CDK destroy for specific stacks, how to preserve shortage state data during rollback
6. THE deployment SHALL validate therapeutic category configuration on first Lambda invocation and log clear errors if configuration is missing
7. THE Drug_Shortage_Module SHALL not require changes to existing Lambda function code for disease outbreak detection (new shortage logic is additive)
8. THE deployment process SHALL include a post-deployment smoke test: manual invocation of openFDA fetcher Lambda, validation that shortage state table is created, validation that test subscription receives shortage alert

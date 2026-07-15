# Subscription Management Schema

## DynamoDB Table: `healthsignals-subscriptions`

### Key Schema

| Key | Attribute         | Type   | Description                           |
| --- | ----------------- | ------ | ------------------------------------- |
| PK  | `county_fips`     | String | 5-digit county FIPS code              |
| SK  | `subscription_id` | String | UUID — unique subscription identifier |

### Attributes

| Attribute              | Type         | Required | Description                                                    |
| ---------------------- | ------------ | -------- | -------------------------------------------------------------- |
| `county_name`          | String       | Yes      | Human-readable county name                                     |
| `state`                | String       | Yes      | State config key (e.g., "texas")                               |
| `contact_name`         | String       | Yes      | Health officer name                                            |
| `contact_email`        | String       | Yes      | Primary contact email                                          |
| `contact_phone`        | String       | No       | E.164 format phone for SMS (e.g., "+15551234567")              |
| `diseases`             | List[String] | Yes      | Subscribed diseases (e.g., ["influenza", "rsv", "covid"])      |
| `delivery_preferences` | Map          | Yes      | See Delivery Preferences below                                 |
| `status`               | String       | Yes      | One of: `pending_verification`, `active`, `paused`, `inactive` |
| `created_at`           | String       | Yes      | ISO 8601 timestamp                                             |
| `updated_at`           | String       | Yes      | ISO 8601 timestamp                                             |
| `verified_at`          | String       | No       | When email was verified (null if pending)                      |
| `last_alert_sent`      | String       | No       | Timestamp of last alert dispatched                             |
| `pause_until`          | String       | No       | ISO date — alerts suppressed until this date                   |
| `unsubscribed_at`      | String       | No       | When unsubscribed (only if status=inactive)                    |
| `unsubscribe_reason`   | String       | No       | How they unsubscribed (email_link, api_request)                |
| `metadata`             | Map          | No       | Internal tracking (source IP, user agent, etc.)                |

### Delivery Preferences Map

```json
{
  "channels": ["email", "sms"],
  "alert_threshold": "MODERATE",
  "quiet_hours": "22:00-07:00",
  "digest_mode": false
}
```

| Field             | Type         | Default    | Description                                                      |
| ----------------- | ------------ | ---------- | ---------------------------------------------------------------- |
| `channels`        | List[String] | ["email"]  | Delivery channels: "email", "sms"                                |
| `alert_threshold` | String       | "MODERATE" | Minimum severity to trigger alert: LOW, MODERATE, HIGH, CRITICAL |
| `quiet_hours`     | String       | null       | Time range to suppress SMS (email still sent)                    |
| `digest_mode`     | Boolean      | false      | If true, batch weekly alerts instead of immediate                |

### Status Lifecycle

```
                   ┌─────────────────┐
                   │    SUBSCRIBE    │
                   └────────┬────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │  pending_verification │
                └───────────┬───────────┘
                            │ (verify email)
                            ▼
                ┌───────────────────────┐
           ┌────│        active         │────┐
           │    └───────────────────────┘    │
           │ (pause)                    (unsub)│
           ▼                                  ▼
    ┌──────────────┐                 ┌──────────────┐
    │    paused    │                 │   inactive   │
    └──────┬───────┘                 └──────────────┘
           │ (resume / pause_until expires)
           ▼
    ┌──────────────┐
    │    active    │
    └──────────────┘
```

### Global Secondary Indexes

| Index Name              | PK               | SK            | Use Case                                                                                       |
| ----------------------- | ---------------- | ------------- | ---------------------------------------------------------------------------------------------- |
| `status-index`          | `status`         | `updated_at`  | Find all pending verifications, expired pauses                                                 |
| `state-index`           | `state`          | `county_fips` | State admin: list all subscriptions for a state                                                |
| `alert-category-lookup` | `alert_category` | `county_fips` | Plugin module: find subscribers by alert category (added dynamically when plugins are enabled) |

> The `alert-category-lookup` GSI is only created when a plugin module requests it via the `plugin_gsis` parameter in CDK. When no plugins are enabled, this GSI does not exist.

---

## Subscriber Alert Categories

When plugin modules are enabled, subscribers can opt into specific alert categories:

```json
{
  "alert_categories": ["antivirals", "antibiotics"]
}
```

This field is managed via the `PUT /subscription/preferences` endpoint. Valid categories are defined in `config/alert_categories.json`.

---

## API Endpoints

| Method | Path                                               | Description                      | Auth            |
| ------ | -------------------------------------------------- | -------------------------------- | --------------- |
| POST   | `/subscription/subscribe`                          | Create new subscription          | None (public)   |
| GET    | `/subscription/verify?token=[REDACTED_PARAM]`      | Verify email (double opt-in)     | Signed token    |
| GET    | `/subscription/unsubscribe?token=[REDACTED_PARAM]` | One-click unsubscribe from email | Signed token    |
| POST   | `/subscription/unsubscribe`                        | Programmatic unsubscribe         | subscription_id |
| PUT    | `/subscription/preferences`                        | Update settings                  | subscription_id |
| GET    | `/subscription/status?county_fips=X`               | Check status                     | county_fips     |

---

## Token Format

Tokens are HMAC-SHA256 signed, base64url-encoded payloads:

```
{base64url(payload)}.{hmac_signature}
```

Payload structure:

```json
{
  "fips": "48143",
  "sub": "uuid-subscription-id",
  "purpose": "verification|unsubscribe|auth",
  "exp": 1720000000
}
```

---

## Integration with Alert Dispatcher

When the alert_dispatcher sends an alert:

1. Query `healthsignals-subscriptions` by `county_fips`
2. Filter: `status = "active"` AND `verified_at IS NOT NULL`
3. Filter: `pause_until IS NULL OR pause_until < NOW()`
4. Check: `alert_threshold <= alert_severity`
5. For each matching subscription: dispatch via configured channels
6. Update `last_alert_sent` timestamp
7. Include unsubscribe URL in every email/SMS

---

## Capacity Planning

| Scale                            | Subscriptions | Reads/week | Writes/week | Estimated Cost |
| -------------------------------- | ------------- | ---------- | ----------- | -------------- |
| Pilot (1 state, 100 counties)    | ~200          | ~400       | ~50         | < $1/month     |
| Growth (5 states, 500 counties)  | ~1,000        | ~2,000     | ~250        | < $5/month     |
| Scale (50 states, 3000 counties) | ~6,000        | ~12,000    | ~1,500      | < $25/month    |

DynamoDB on-demand pricing: effectively free at these volumes.

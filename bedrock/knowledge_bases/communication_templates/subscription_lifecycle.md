# Subscription Lifecycle Email Templates

These templates are used by the subscription management system for administrative
communications (not disease alerts). Bedrock does NOT generate these — they are
sent as-is with placeholder substitution.

---

## 1. Verification Email (Double Opt-In)

**When sent:** Immediately after POST /subscription/subscribe
**Purpose:** Confirm email ownership before activating alerts

```
Subject: Verify your HealthSignals subscription — [COUNTY_NAME]

Hello [CONTACT_NAME],

Thank you for subscribing [COUNTY_NAME] to Amazon HealthSignals disease
preparedness alerts.

To activate your subscription, please verify your email by clicking below:

[VERIFICATION_URL]

This link expires in 72 hours. If it expires, you can request a new
verification email.

What you'll receive once verified:
• Weekly situation briefs when respiratory disease activity is detected
  in sentinel metro areas (Houston, DFW, Austin, San Antonio)
• Preparation checklists with estimated arrival timing for your county
• SMS alerts for high-severity situations (if phone number provided)

Subscribed diseases: [DISEASE_LIST]
Delivery: [CHANNEL_LIST]
Minimum alert level: [THRESHOLD]

If you did not request this subscription, you can safely ignore this email.
No alerts will be sent without verification.

—
Amazon HealthSignals
Predictive Disease Surveillance for Rural Communities
```

---

## 2. Welcome Email (Post-Verification)

**When sent:** After successful GET /subscription/verify
**Purpose:** Confirm activation and set expectations

```
Subject: Welcome to HealthSignals — [COUNTY_NAME] is now subscribed

Hello [CONTACT_NAME],

Your HealthSignals subscription for [COUNTY_NAME] is now active! ✓

Subscription details:
• County: [COUNTY_NAME] (FIPS: [COUNTY_FIPS])
• State: [STATE]
• Monitoring: [DISEASE_LIST]
• Delivery: [CHANNEL_LIST]
• Alert threshold: [THRESHOLD] and above

How it works:
1. We monitor disease signals in major Texas metro areas every week
2. When a metro crosses threshold, we calculate when your county
   will likely be affected using historical patterns
3. You receive a preparation brief with:
   - Situation summary
   - Expected timing (weeks until arrival)
   - Severity estimate with confidence level
   - Actionable preparation checklist
   - Draft communications for your community

What to expect:
• During quiet season (May–September): No alerts typically
• During respiratory season (October–April): 0-3 alerts per month
• Time commitment: ≤5 minutes per week

Important disclaimers:
• All alerts are advisory — based on historical pattern analysis
• Confidence levels are always included
• This is NOT a clinical diagnostic tool
• You can pause or unsubscribe at any time

To manage your subscription:
• Pause alerts: [PREFERENCES_URL]
• Change settings: [PREFERENCES_URL]
• Unsubscribe: Link included in every alert email

Stay prepared,
Amazon HealthSignals Team

Subscription ID: [SUBSCRIPTION_ID]
```

---

## 3. Unsubscribe Confirmation

**When sent:** After successful unsubscribe (GET or POST)
**Purpose:** Confirm removal and provide re-subscribe path

```
Subject: HealthSignals — [COUNTY_NAME] unsubscribed

Hello [CONTACT_NAME],

This confirms that [COUNTY_NAME] has been unsubscribed from HealthSignals
disease preparedness alerts.

Effective immediately, you will no longer receive:
• Disease situation briefs
• Preparation checklists
• SMS notifications

Your subscription data will be retained for 90 days in case you wish to
re-subscribe. After 90 days, all data is permanently deleted.

Changed your mind?
You can re-subscribe at any time by contacting your state health department
or visiting: [SUBSCRIBE_URL]

We'd appreciate your feedback:
What could we have done better? Reply to this email or visit:
[FEEDBACK_URL]

Thank you for using HealthSignals.

—
Amazon HealthSignals Team
```

---

## 4. Preference Update Confirmation

**When sent:** After successful PUT /subscription/preferences
**Purpose:** Confirm changes were applied

```
Subject: HealthSignals settings updated — [COUNTY_NAME]

Hello [CONTACT_NAME],

Your HealthSignals subscription settings have been updated:

Changes applied:
[CHANGES_LIST]

Current settings:
• Diseases: [DISEASE_LIST]
• Delivery: [CHANNEL_LIST]
• Alert threshold: [THRESHOLD]
• Status: [STATUS]
[IF_PAUSED]• Paused until: [PAUSE_UNTIL][/IF_PAUSED]

These changes take effect immediately.

If you did not make these changes, please contact your state health
department administrator immediately.

—
Amazon HealthSignals Team
Subscription ID: [SUBSCRIPTION_ID]
```

---

## 5. Annual Re-Verification Warning

**When sent:** 30 days before subscription anniversary
**Purpose:** CAN-SPAM compliance + confirm continued interest

```
Subject: Action needed: Renew your HealthSignals subscription — [COUNTY_NAME]

Hello [CONTACT_NAME],

Your HealthSignals subscription for [COUNTY_NAME] will expire in 30 days
(on [EXPIRY_DATE]) unless you confirm you'd like to continue receiving alerts.

To renew for another year, click below:

[REVERIFICATION_URL]

If you do nothing, your subscription will be automatically paused on
[EXPIRY_DATE]. You can reactivate at any time after that.

Your subscription stats this year:
• Alerts received: [ALERT_COUNT]
• Diseases monitored: [DISEASE_LIST]
• Active since: [VERIFIED_AT]

—
Amazon HealthSignals Team
```

---

## 6. Subscription Paused Notification

**When sent:** When pause_until date is set (manual or auto)
**Purpose:** Confirm alerts are paused

```
Subject: HealthSignals alerts paused — [COUNTY_NAME]

Hello [CONTACT_NAME],

HealthSignals alerts for [COUNTY_NAME] have been paused.

• Paused until: [PAUSE_UNTIL]
• Alerts will automatically resume after this date

During the pause:
• No situation briefs or SMS alerts will be sent
• Monitoring continues in the background
• If a CRITICAL alert occurs, you will still be notified (safety override)

To resume alerts early:
[RESUME_URL]

—
Amazon HealthSignals Team
```

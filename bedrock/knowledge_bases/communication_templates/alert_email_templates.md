# Alert Email Templates — Severity-Graded

*These templates are retrieved by Bedrock and adapted for each specific alert. [PLACEHOLDER] values are filled dynamically.*

---

## Template 1: LOW Severity — Routine Surveillance Update

**Subject:** [COUNTY_NAME] Health Update: [DISEASE] activity detected in [METRO_NAME]

---

Good [morning/afternoon] [RECIPIENT_NAME],

This is your weekly surveillance update from HealthSignals.

**Current Situation:**
[DISEASE] activity has been detected in [METRO_NAME] at [VALUE]% of emergency department visits as of [DATE]. This is above the seasonal baseline but remains at normal early-season levels.

**What This Means for [COUNTY_NAME]:**
Based on [SEASONS_COUNT] prior seasons of data, [COUNTY_NAME] typically experiences [DISEASE] activity [LAG_WEEKS] weeks after [METRO_NAME]. At this time, no elevated activity is expected in your area for approximately [WEEKS_UNTIL] weeks.

**Recommended Actions:**
- No immediate action required
- Continue routine surveillance monitoring
- Verify seasonal vaccine supply and staff vaccination status
- Review preparedness plans for the upcoming season

**Confidence Level:** [CONFIDENCE]%
*This advisory is based on historical pattern analysis from [SEASONS_COUNT] prior respiratory seasons. Actual timing and severity may differ from projections.*

---

Regards,
HealthSignals Automated Surveillance System
[STATE_NAME] Department of Health

Questions? Contact [STATE_EPI_EMAIL] or call [STATE_EPI_PHONE]
Next scheduled update: [NEXT_UPDATE_DATE]

---

## Template 2: MODERATE Severity — Preparation Advisory

**Subject:** [COUNTY_NAME] ADVISORY: [DISEASE] preparation recommended — [LAG_WEEKS]-week window

---

[RECIPIENT_NAME],

**PREPARATION ADVISORY — [DISEASE]**

HealthSignals has detected sustained [DISEASE] activity in [METRO_NAME] that historically precedes activity in [COUNTY_NAME]. Based on [SEASONS_COUNT] seasons of calibration data, we recommend beginning preparation now.

**Situation Summary:**
- **Leader metro:** [METRO_NAME] crossed threshold on [DETECTION_DATE]
- **Current metro level:** [VALUE]% ED visits ([TREND] trend)
- **State activity level:** [STATE_ACTIVITY_LEVEL]
- **Expected arrival in [COUNTY_NAME]:** Approximately [LAG_WEEKS] weeks (±[CONFIDENCE_INTERVAL] weeks)
- **Expected severity:** [SEVERITY_MULTIPLIER]× the metro peak ([SEVERITY_CATEGORY])

**Preparation Checklist (3–5 week timeline):**

☐ **This week:**
- Review and update staffing surge plan
- Verify antiviral medication stock (oseltamivir — [ANTIVIRAL_QUANTITY] courses needed)
- Confirm vaccine supply and schedule extended-hours clinic if needed
- Test notification systems (staff phone tree, community alert)

☐ **Within 2 weeks:**
- Activate Medical Reserve Corps awareness (not deployment yet)
- Coordinate with local pharmacy on antiviral/test kit supply
- Issue preparedness reminder to healthcare partners
- Pre-position respiratory supplies (masks, tests, hand sanitizer)

☐ **Within 3 weeks:**
- Brief community partners (schools, long-term care, EMS)
- Prepare public communication materials
- Verify hospital transfer protocols
- Update isolation/treatment spaces

**Confidence Level:** [CONFIDENCE]%
*Based on historical pattern analysis from [SEASONS_COUNT] prior seasons. This is an advisory — not a guarantee of [DISEASE] arrival. Confidence interval: ±[CONFIDENCE_INTERVAL] weeks. Use this information alongside your professional judgment and local context.*

---

[HEALTH_OFFICER_NAME], [TITLE]
[COUNTY_NAME] Health Department
Via HealthSignals — [STATE_NAME] Department of Health

Report concern or feedback: [FEEDBACK_URL]

---

## Template 3: HIGH Severity — Urgent Preparation Alert

**Subject:** ⚠️ URGENT [COUNTY_NAME]: [DISEASE] alert HIGH — Immediate preparation needed

---

**URGENT PREPARATION ALERT**
**[COUNTY_NAME] — [DISEASE]**
**Severity: HIGH | Expected arrival: [LAG_WEEKS] weeks**

---

[RECIPIENT_NAME],

This is an urgent preparation alert. [DISEASE] activity in [METRO_NAME] has reached HIGH severity levels, significantly exceeding historical averages. Based on [SEASONS_COUNT] seasons of data, [COUNTY_NAME] should expect elevated activity within [LAG_WEEKS] weeks.

**Why This Alert is HIGH Severity:**
[SEVERITY_REASONING]

**Situation:**
| Metric | Value |
|--------|-------|
| Metro signal | [VALUE]% ED visits ([TREND]) |
| Historical severity multiplier | [SEVERITY_MULTIPLIER]× |
| Expected county peak | [PEAK_ESTIMATE]% ED visits |
| Confidence | [CONFIDENCE]% |
| Preparation window | [LAG_WEEKS] weeks |

**IMMEDIATE ACTIONS (this week):**

1. ☐ **Activate preparedness plan** — Move from monitoring to active preparation
2. ☐ **Verify staffing** — Confirm 2-week staff availability; cancel non-essential leave
3. ☐ **Stock antivirals** — Ensure [ANTIVIRAL_QUANTITY]+ courses of oseltamivir available
4. ☐ **Notify partners** — Send advisory to:
   - [ ] Hospital/clinic administrators
   - [ ] School superintendents
   - [ ] Long-term care facilities
   - [ ] EMS providers
5. ☐ **Prepare public communications** — Draft community advisory for release in 1-2 weeks
6. ☐ **Coordinate with state** — Notify state epidemiologist of elevated local preparedness

**NEXT WEEK:**

7. ☐ **Activate Medical Reserve Corps** if available
8. ☐ **Implement telehealth triage** to preserve clinic capacity
9. ☐ **Pre-position supplies** at community distribution points
10. ☐ **Brief elected officials** on expected timeline and resource needs

**Escalation Path:**
If situation worsens or resources are insufficient, contact:
- State Epidemiologist: [STATE_EPI_PHONE]
- Regional Healthcare Coalition: [RHC_PHONE]
- HealthSignals support: [SUPPORT_EMAIL]

**Confidence Level:** [CONFIDENCE]%
*This advisory is based on historical pattern analysis from [SEASONS_COUNT] prior respiratory seasons. It is NOT a clinical diagnosis or certainty. Actual timing may be ±[CONFIDENCE_INTERVAL] weeks from projection. Please use alongside professional judgment.*

---

[HEALTH_OFFICER_NAME], [TITLE]
[COUNTY_NAME] Health Department
Via HealthSignals — [STATE_NAME] Department of Health

---

## Template 4: CRITICAL Severity — Emergency Preparedness Notification

**Subject:** 🚨 CRITICAL [COUNTY_NAME]: [DISEASE] surge imminent — ACTIVATE EMERGENCY PROTOCOLS

---

**🚨 CRITICAL ALERT — EMERGENCY PREPAREDNESS NOTIFICATION 🚨**

**[COUNTY_NAME] | [DISEASE] | Severity: CRITICAL**
**Expected arrival: <[LAG_WEEKS] week(s)**
**Issued: [TIMESTAMP]**

---

[RECIPIENT_NAME],

**This alert requires immediate action.**

[DISEASE] activity in [METRO_NAME] has reached CRITICAL levels — [VALUE]% of ED visits, which is [MULTIPLIER_VS_AVERAGE]× the seasonal average. Multiple sentinel metros are now affected simultaneously. Based on [SEASONS_COUNT] seasons, [COUNTY_NAME] should expect significant activity within days to [LAG_WEEKS] week(s).

**IMMEDIATE ACTIONS — TODAY:**

1. 🔴 **ACTIVATE** Emergency Operations (Level 2 minimum)
2. 🔴 **NOTIFY** State Health Department EOC: [STATE_EOC_PHONE]
3. 🔴 **BRIEF** all clinical staff on surge protocols
4. 🔴 **CONFIRM** hospital bed availability and transfer capacity
5. 🔴 **ACTIVATE** mutual aid agreements
6. 🔴 **DEPLOY** Medical Reserve Corps

**THIS WEEK:**

7. ☐ Issue public health advisory to community
8. ☐ Coordinate school response (monitor absenteeism, prepare for possible closure)
9. ☐ Establish 12-hour shift rotations
10. ☐ Open emergency antiviral distribution point
11. ☐ Activate community health worker network for outreach
12. ☐ Confirm ambulance/air medical availability for transfers

**CRITICAL CONTACTS:**
| Role | Contact |
|------|---------|
| State Epidemiologist | [STATE_EPI_PHONE] |
| State EOC | [STATE_EOC_PHONE] |
| Regional Healthcare Coalition | [RHC_PHONE] |
| Nearest Hospital ED | [HOSPITAL_PHONE] |
| Medical Reserve Corps | [MRC_PHONE] |

**Confidence Level:** [CONFIDENCE]%
*Based on [SEASONS_COUNT] prior seasons. Critical severity assigned due to: [CRITICAL_CRITERIA]. Despite high confidence in DIRECTION of threat, exact timing carries ±[CONFIDENCE_INTERVAL] week uncertainty. Prepare for EARLY arrival.*

---

This alert has been simultaneously sent to:
- [RECIPIENT_NAME] (you)
- State Epidemiologist (CC)
- [STATE_NAME] EOC (CC)

[HEALTH_OFFICER_NAME], [TITLE]
[COUNTY_NAME] Health Department
Via HealthSignals — [STATE_NAME] Department of Health

---

## Template 5: ALL-CLEAR — Activity Declining, Stand Down

**Subject:** ✅ [COUNTY_NAME] [DISEASE] Update: Activity declining — Preparation stand-down

---

[RECIPIENT_NAME],

**GOOD NEWS: [DISEASE] activity is declining.**

The [DISEASE] wave that prompted our earlier alert (issued [ORIGINAL_ALERT_DATE]) appears to be subsiding. Metropolitan sentinel signals and local indicators show sustained decline over the past [DECLINE_WEEKS] weeks.

**Current Status:**
- Metro signal: [VALUE]% ED visits (down from peak of [PEAK_VALUE]%)
- Trend: Declining for [DECLINE_WEEKS] consecutive weeks
- State activity level: [STATE_ACTIVITY_LEVEL]
- Local indicators: [LOCAL_STATUS]

**Recommended Actions:**
- ☐ Begin demobilization of surge resources
- ☐ Resume normal staffing schedules
- ☐ Conduct after-action review (what worked, what didn't)
- ☐ Replenish supplies used during response
- ☐ Submit feedback on HealthSignals prediction accuracy: [FEEDBACK_URL]

**Season Summary:**
- Alert issued: [ORIGINAL_ALERT_DATE]
- Predicted arrival: Week of [PREDICTED_WEEK]
- Actual peak observed: [ACTUAL_PEAK_INFO]
- Prediction accuracy: [ACCURACY_ASSESSMENT]

**Your Feedback Matters:**
Please take 2 minutes to report whether this season's alerts were helpful:
[FEEDBACK_URL]

Thank you for your dedication to [COUNTY_NAME]'s health preparedness.

---

[HEALTH_OFFICER_NAME], [TITLE]
[COUNTY_NAME] Health Department
Via HealthSignals — [STATE_NAME] Department of Health

Monitoring will continue through end of respiratory season ([END_DATE]).

# SMS Alert Templates (≤160 Characters)

*Each template must be ≤160 characters to fit in a single SMS segment. [PLACEHOLDERS] are filled dynamically. Character counts include placeholders at expected max length.*

---

## LOW Severity — Informational

**Template:**
```
[COUNTY] Health: [DISEASE] activity rising in [METRO]. No action needed now. Monitoring. Details emailed.
```
*Characters: ~100 (with typical fills)*

**Alternate:**
```
[COUNTY] Update: Seasonal [DISEASE] detected in [METRO]. Normal levels. No action needed. Weekly email sent.
```

---

## MODERATE Severity — Preparation Advisory

**Template:**
```
[COUNTY] ADVISORY: [DISEASE] expected in ~[N] wks. Begin prep now. Checklist emailed. Questions: [PHONE]
```
*Characters: ~105 (with typical fills)*

**Alternate:**
```
[COUNTY]: [DISEASE] advisory issued. [METRO] signal elevated. Prepare over next [N] wks. See email for details.
```

---

## HIGH Severity — Urgent Alert

**Template:**
```
URGENT [COUNTY]: [DISEASE] HIGH alert. Arrival est [N] wks. Prepare immediately. Checklist emailed. Call [PHONE] w/questions.
```
*Characters: ~125 (with typical fills)*

**Alternate:**
```
[COUNTY] HIGH ALERT: [DISEASE] surge approaching. Est [N] wks. Take action now. Full brief emailed. State notified.
```

---

## CRITICAL Severity — Emergency

**Template:**
```
[COUNTY] CRITICAL: [DISEASE] surge imminent. Activate emergency protocols. Call state EOC [PHONE]. Details emailed.
```
*Characters: ~115 (with typical fills)*

**Alternate (with emoji for visual urgency — supported on all modern phones):**
```
🚨[COUNTY] CRITICAL: [DISEASE] emergency. Activate surge plan NOW. State EOC: [PHONE]. Check email immediately.
```

---

## ALL-CLEAR — Stand Down

**Template:**
```
[COUNTY]: [DISEASE] declining. Worst has passed. Begin stand-down. After-action details emailed. Thank you.
```
*Characters: ~105 (with typical fills)*

---

## WEEKLY UPDATE — Monitoring (No Alert)

**Template:**
```
[COUNTY] weekly: [DISEASE] quiet. No alerts. [METRO] at [X]% (below threshold). Next update [DAY].
```
*Characters: ~95 (with typical fills)*

---

## FEEDBACK REQUEST — Post-Season

**Template:**
```
[COUNTY]: Season ending. Was our [DISEASE] alert helpful? Reply Y/N or visit [SHORT_URL]. Takes 30 sec. Thanks!
```
*Characters: ~110 (with typical fills)*

---

## Design Rules for SMS

1. **County name first** — recipient sees it immediately in notification preview
2. **Severity word in CAPS** — ADVISORY, URGENT, CRITICAL for visual scanning
3. **Timeframe always included** — "[N] wks" gives immediate mental model
4. **Action verb** — "Begin prep," "Prepare immediately," "Activate"
5. **Reference email** — SMS is notification; email has the detail
6. **Phone number for CRITICAL only** — don't overwhelm in lower severities
7. **No URLs in SMS** (except feedback) — links look like spam, get ignored
8. **No clinical language** — "surge" not "outbreak", "activity" not "infection rate"

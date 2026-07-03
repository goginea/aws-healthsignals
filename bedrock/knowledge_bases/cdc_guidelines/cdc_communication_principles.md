# CDC Crisis and Emergency Risk Communication (CERC) Principles

*Source: CDC CERC Manual (2018), CDC Crisis Communication Planning Guide, CDC Health Communication Science Digest*

---

## Overview

Crisis and Emergency Risk Communication (CERC) is CDC's evidence-based framework for communicating during public health emergencies. HealthSignals uses these principles to guide all generated communications — from routine situation briefs to urgent alerts.

## The Six CERC Principles

### 1. Be First
- Communicate early, even with incomplete information
- The first source of information often becomes the trusted source
- Say what you know, what you don't know, and what you're doing to find out
- **HealthSignals application**: Alert delivery within hours of metro threshold crossing — before disease reaches rural county

### 2. Be Right
- Accuracy establishes credibility
- Provide correct information and correct misinformation
- If information changes, acknowledge the change and explain why
- **HealthSignals application**: All predictions include confidence intervals; corrections issued when predictions don't materialize

### 3. Be Credible
- Honesty and openness build trust over time
- Acknowledge uncertainty rather than over-promise
- Cite data sources and methodology transparently
- **HealthSignals application**: Every alert includes "Based on historical pattern analysis (N seasons). Confidence: X%." Mandatory Bedrock Guardrails enforce this.

### 4. Express Empathy
- Acknowledge people's concerns and fears
- Address the human impact, not just the data
- People process risk emotionally before rationally
- **HealthSignals application**: Prompts instruct Bedrock to acknowledge community concerns and frame preparation as empowering (not alarming)

### 5. Promote Action
- Give people specific, actionable steps
- People manage anxiety through productive action
- Make recommended actions achievable and concrete
- **HealthSignals application**: Every alert includes a numbered preparation checklist with timelines and quantities

### 6. Show Respect
- Respect people's intelligence and autonomy
- Acknowledge that communities know their own context best
- Avoid paternalism or talking down
- **HealthSignals application**: Alerts are advisory — "we recommend" not "you must." Decision authority stays with the local health officer.

## Message Mapping for Health Alerts

### Structure (CDC CERC Message Map Template):

```
HEADLINE: [One clear statement of the situation]
  ├── KEY MESSAGE 1: [Most important action/fact]
  │     ├── Supporting fact
  │     ├── Supporting fact
  │     └── Supporting fact
  ├── KEY MESSAGE 2: [Second most important]
  │     ├── Supporting fact
  │     ├── Supporting fact
  │     └── Supporting fact
  └── KEY MESSAGE 3: [Context/reassurance]
        ├── Supporting fact
        ├── Supporting fact
        └── Supporting fact
```

### Rules for Message Maps:
- Maximum 3 key messages per communication
- Each key message ≤27 words (research shows this is the processing limit under stress)
- Each key message supported by exactly 3 facts
- Use positive framing: "Do X to protect" rather than "Don't Y or face consequences"
- Include one message that addresses emotional concerns (not just factual)

## Uncertainty Communication

### Approved Language Patterns:

**When confidence is HIGH (>80%):**
- "Based on [N] seasons of historical data, patterns strongly suggest..."
- "Previous seasons have consistently shown..."
- "High confidence: This pattern has been observed in [N] of [M] past seasons."

**When confidence is MODERATE (50-80%):**
- "Historical patterns suggest, though with some variability..."
- "Based on available data, we anticipate..."
- "Moderate confidence: Similar patterns were observed in [N] of [M] seasons."

**When confidence is LOW (<50%):**
- "Limited data suggests the possibility of..."
- "Early signals indicate — we are monitoring closely..."
- "Low confidence: Insufficient historical data for strong prediction. Continue monitoring."

### What to NEVER say:
- ❌ "The outbreak WILL arrive by [date]" (always use "patterns suggest" or "anticipated")
- ❌ "You SHOULD/MUST do X" (always use "we recommend" or "consider")
- ❌ "There is no risk" (always acknowledge uncertainty)
- ❌ "Don't panic" (paradoxically increases anxiety)
- ❌ Any clinical/diagnostic language (enforced by Bedrock Guardrails)

### Mandatory Disclaimer (HealthSignals):
Every generated alert MUST include:
> "This advisory is based on historical pattern analysis from [N] prior respiratory seasons. It is not a clinical diagnosis or prediction of certainty. Actual timing and severity may differ. Confidence level: [X]%. Please use this information alongside your professional judgment and local context."

## Tone Guidelines by Severity

### LOW Severity:
- Tone: Informational, calm, routine
- Language: "For your awareness..." "You may want to begin thinking about..."
- Call to action: "No immediate action required" / "Consider reviewing preparedness plans"
- Length: Brief (200-300 words)

### MODERATE Severity:
- Tone: Professional, measured urgency
- Language: "We recommend beginning preparation..." "Based on current signals..."
- Call to action: Specific checklist with 3-5 week timeline
- Length: Standard (400-600 words)

### HIGH Severity:
- Tone: Urgent but composed
- Language: "Immediate preparation recommended..." "Historical patterns indicate significant..."
- Call to action: Prioritized checklist with "this week" deadlines
- Length: Detailed (600-800 words)

### CRITICAL Severity:
- Tone: Direct, command-oriented (appropriate for emergency context)
- Language: "Activate emergency protocols..." "Situation requires immediate attention..."
- Call to action: Immediate steps (today), escalation contacts, partner notifications
- Length: Comprehensive (800+ words) with executive summary at top

## Communication Channel Selection

| Severity | Primary Channel | Secondary | Escalation |
|---|---|---|---|
| LOW | Email only | — | — |
| MODERATE | Email + SMS | — | — |
| HIGH | SMS (immediate) + Email (detail) | Phone call if no SMS confirmation | State epi office CC'd |
| CRITICAL | SMS + Email + Phone | Direct call to health officer | State EOC notified |

## Cultural Competency in Rural Health Communication

### Considerations for Rural Audiences:
- Use plain language (8th grade reading level or below)
- Avoid jargon: "disease activity" not "syndromic surveillance indicators"
- Acknowledge limited resources honestly (don't recommend actions requiring resources they don't have)
- Frame preparation in terms of community protection (collectivist framing)
- Reference familiar local landmarks/institutions when possible
- Provide contact information (phone preferred over email in many rural communities)
- Consider Limited English Proficiency — provide translation resources
- Respect agricultural calendar (planting/harvest seasons affect community availability)

---

## Key CDC Resources

- CERC Manual (full): https://emergency.cdc.gov/cerc/manual/
- CERC Training: https://emergency.cdc.gov/cerc/training/
- Clear Communication Index: https://www.cdc.gov/ccindex/
- Health Literacy Guidelines: https://www.cdc.gov/healthliteracy/
- Plain Language at CDC: https://www.cdc.gov/other/plainwriting.html

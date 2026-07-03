# CDC Respiratory Virus Activity Level Definitions

*Source: CDC Respiratory Virus Activity Levels Dashboard FAQ (https://www.cdc.gov/respiratory-viruses/data/faqs.html), CDC Respiratory Illnesses Data Channel*

---

## Acute Respiratory Illness (ARI) Metric

### Definition:
ARI captures a broad range of emergency department visits for respiratory illnesses — from the common cold to severe infections like influenza, RSV, and COVID-19.

The ARI definition includes ED visits with:
- Discharge diagnosis codes for respiratory illness (ICD-10 codes J00-J99)
- Chief complaint terms related to cough, fever, sore throat, respiratory distress, shortness of breath, pneumonia, bronchitis, or influenza-like symptoms

### Data Source:
- National Syndromic Surveillance Program (NSSP)
- Covers approximately 78% of US emergency departments
- Updated weekly (typically on Fridays with previous week's data)
- Geographic levels: National, HHS Region, State, and some sub-state regions

## Activity Level System

CDC categorizes respiratory illness activity into five levels based on the percentage of ED visits for respiratory illness compared to historical data.

### How Activity Levels Are Calculated:

1. **Calculate current percentage**: % of ED visits meeting ARI definition for the current week
2. **Compare to historical baseline**: Compare to the same geographic area's historical data from past seasons
3. **Assign percentile rank**: Where does the current value fall relative to historical values?
4. **Map to category**: Based on the percentile distribution

### Activity Level Thresholds:

| Activity Level | Percentile Range | Interpretation |
|---|---|---|
| **Very Low** | Below the 10th percentile | Activity well below what is typically observed |
| **Low** | 10th to below 25th percentile | Activity below typical levels |
| **Moderate** | 25th to below 50th percentile | Activity at typical levels |
| **High** | 50th to below 75th percentile | Activity above typical levels |
| **Very High** | At or above 75th percentile | Activity well above what is typically observed |

### Important Notes on Methodology:
- Percentiles are calculated separately for EACH geographic unit (state, region)
- Historical comparison period includes multiple past respiratory seasons
- The baseline accounts for seasonal variation (compares similar time periods)
- Levels are NOT absolute thresholds — they are RELATIVE to that location's history
- A "High" level in one state may correspond to a different % ED visits than in another state

## What Each Activity Level Means for Public Health Action

### Very Low:
- **Situation**: Minimal respiratory illness activity
- **Action**: Routine surveillance operations; no additional action needed
- **Communication**: No public health alerts needed
- **HealthSignals**: System in passive monitoring mode; no alerts generated

### Low:
- **Situation**: Some respiratory illness present but below historical averages
- **Action**: Enhanced monitoring; verify preparedness supplies
- **Communication**: Routine seasonal health messages (get vaccinated, wash hands)
- **HealthSignals**: Monitoring signals; may issue informational brief if rising

### Moderate:
- **Situation**: Activity at typical seasonal levels
- **Action**: Active surveillance; ensure staffing plans ready; verify vaccine supply
- **Communication**: Issue public reminder about respiratory virus prevention
- **HealthSignals**: Monitor metro trends; issue alert if acceleration detected

### High:
- **Situation**: Activity above typical levels — active community transmission
- **Action**: Implement preparedness plan; surge staffing as needed; coordinate with hospitals
- **Communication**: Active messaging about staying home when sick, seeking care if high-risk
- **HealthSignals**: Generate preparation checklists for subscribing counties

### Very High:
- **Situation**: Activity well above typical — possible outbreak/peak
- **Action**: Full preparedness activation; request mutual aid if needed; possible school interventions
- **Communication**: Urgent public health messaging; partner notifications
- **HealthSignals**: HIGH/CRITICAL severity alerts with urgent preparation checklists

## Disease-Specific Activity Monitoring

CDC provides activity data for individual pathogens in addition to overall ARI:

### Influenza:
- Metric: % ED visits with influenza diagnosis (ICD-10 J09-J11)
- Historical seasons available for comparison: 2018-19 through present
- Typical peak: December–February
- Signal name in Delphi API: `nssp:pct_ed_visits_influenza`

### COVID-19:
- Metric: % ED visits with COVID-19 diagnosis (ICD-10 U07.1)
- Historical data: 2020 through present (post-2022 more comparable)
- No consistent seasonal peak — can occur any time
- Signal name in Delphi API: `nssp:pct_ed_visits_covid`

### RSV:
- Metric: % ED visits with RSV diagnosis (ICD-10 J12.1, J20.5, J21.0)
- Historical seasons: 2018-19 through present (disrupted 2020-22)
- Typical peak: November–January (varies by region)
- Signal name in Delphi API: `nssp:pct_ed_visits_rsv`

## Geographic Variation

### Regional Timing Differences:
- **South/Southeast**: Typically earliest onset (October–November)
- **West**: Variable timing; often later onset
- **Northeast/Midwest**: Typically peaks later (January–February)
- **HealthSignals Implication**: Sentinel metro in Texas often leads national trends

### State vs. National:
- National-level activity is the aggregate — may mask significant state-level variation
- A state at "Very High" while national is "Moderate" indicates localized outbreak
- HealthSignals uses STATE-level data for context and METRO-level for detection

## Data Availability and Access

### CDC Dashboards:
- Respiratory Virus Activity Levels: https://www.cdc.gov/respiratory-viruses/data-research/dashboard/activity-levels.html
- Updated weekly (Fridays)
- Available at national, regional, and state levels

### Programmatic Access:
- **CDC Socrata API**: `https://data.cdc.gov/resource/rdmq-nq56.json`
  - Dataset: "Inpatient, Emergency Department, and Outpatient Visits for Respiratory Illnesses"
  - Fields: geography, pathogen, week_end, percent, visit_type
- **CMU Delphi Epidata API**: `https://api.delphi.cmu.edu/epidata/covidcast/`
  - MSA-level granularity (essential for metro-specific detection)
  - Near-daily updates (vs. weekly for CDC direct)

---

## Key CDC Resources

- Activity Levels FAQ: https://www.cdc.gov/respiratory-viruses/data/faqs.html
- Respiratory Illnesses Data Channel: https://www.cdc.gov/respiratory-viruses/data/index.html
- NSSP Overview: https://www.cdc.gov/nssp/
- Weekly Respiratory Virus Snapshot: https://www.cdc.gov/respiratory-viruses/data-research/dashboard/

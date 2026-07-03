# Data Sources — Amazon HealthSignals

All data sources are publicly available, require no authentication for basic access, and contain no PHI (Protected Health Information). No data sharing agreements or HIPAA compliance required.

---

## 1. CMU Delphi Epidata API (PRIMARY — Metro-level)

| Field | Value |
|-------|-------|
| **Provider** | Carnegie Mellon University Delphi Group |
| **Endpoint** | `https://api.delphi.cmu.edu/epidata/covidcast/` |
| **Auth** | None required (public, grant-funded) |
| **Rate Limits** | Undocumented; academic courtesy applies |
| **Update Frequency** | Daily (we fetch weekly) |
| **SLA** | **None** — academic project, no uptime guarantee |
| **Documentation** | https://cmu-delphi.github.io/delphi-epidata/api/covidcast.html |
| **Status** | ✅ Validated live (July 2026) |

### Signals Used

| Signal | Data Source | Description |
|--------|-------------|-------------|
| `nssp:pct_ed_visits_influenza` | NSSP | % ED visits for influenza |
| `nssp:pct_ed_visits_covid` | NSSP | % ED visits for COVID-19 |
| `nssp:pct_ed_visits_rsv` | NSSP | % ED visits for RSV |
| `nssp:smoothed_pct_ed_visits_influenza` | NSSP | 3-week moving average of flu % |
| `nssp:smoothed_pct_ed_visits_covid` | NSSP | 3-week moving average of COVID % |
| `nssp:smoothed_pct_ed_visits_rsv` | NSSP | 3-week moving average of RSV % |

### Geography

- **Geo type**: `msa` (Metropolitan Statistical Area)
- **Sentinel metros**: 
  - Houston-The Woodlands-Sugar Land: `26420`
  - Dallas-Fort Worth-Arlington: `19100`
  - Austin-Round Rock-Georgetown: `12420`
  - San Antonio-New Braunfels: `41700`

### Query Example

```
GET https://api.delphi.cmu.edu/epidata/covidcast/?data_source=nssp&signal=pct_ed_visits_influenza&geo_type=county&geo_value=48201&time_type=week&time_values=202439-202524
```

> ⚠️ **IMPORTANT:** NSSP signals do NOT support `geo_type=msa` — use `geo_type=county` with the metro's primary county FIPS.
> Use `time_type=week` with epiweek format (YYYYWW), NOT `time_type=day` with YYYYMMDD.

### Limitations

- Grant-funded with no commercial SLA; may experience downtime without notice
- MSA-level granularity only (not county-level)
- Historical data may be revised retroactively
- Coverage: ~78% of US EDs report to NSSP (as of May 2024)
- **Risk mitigation**: CDC NSSP direct access provides redundancy (see Source #3)

---

## 2. CDC NWSS Wastewater Surveillance (SUPPLEMENTAL — State/County-level)

| Field | Value |
|-------|-------|
| **Provider** | CDC National Wastewater Surveillance System (NWSS) |
| **API** | Socrata Open Data API (SODA) on `data.cdc.gov` |
| **Auth** | None required; optional app token for higher rate limits |
| **Rate Limits** | 1,000 req/hr (unauthenticated), 40,000 req/hr (with app token) |
| **Update Frequency** | Weekly on Fridays |
| **Documentation** | https://dev.socrata.com/docs/queries/ |
| **CDC Info** | https://www.cdc.gov/nwss/index.html |
| **Status** | ✅ Active, ~1,500 sampling sites nationwide |

### Datasets (Socrata Identifiers)

| Disease | Dataset ID | Name | Endpoint |
|---------|-----------|------|----------|
| **Influenza A** | `ymmh-divb` | CDC Wastewater Data for Influenza A | `https://data.cdc.gov/resource/ymmh-divb.json` |
| **RSV** | `45cq-cw4i` | CDC Wastewater Data for RSV | `https://data.cdc.gov/resource/45cq-cw4i.json` |
| **SARS-CoV-2** | `2ew6-ywp6` | NWSS Public SARS-CoV-2 Wastewater Metric Data | `https://data.cdc.gov/resource/2ew6-ywp6.json` |
| **Avian Flu (H5)** | `mtpu-urpp` | CDC Wastewater Data for Avian Influenza A (H5) | `https://data.cdc.gov/resource/mtpu-urpp.json` |
| **Combined WVAL** | *(TBD)* | CDC Wastewater Viral Activity Level for SARS-CoV-2, Influenza A and RSV | Combined dataset with weekly WVAL values |

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `wwtp_jurisdiction` | string | State abbreviation (e.g., "TX") |
| `wwtp_id` | integer | Unique anonymous plant identifier |
| `county_fips` | string | 5-digit FIPS code(s) served by this plant |
| `county_names` | string | County names served |
| `date_start` | date | Start of 15-day measurement interval |
| `date_end` | date | End of 15-day measurement interval |
| `ptc_15d` | float | Percent change in viral RNA over 15 days |
| `detect_prop_15d` | float | Proportion of tests with virus detected |
| `percentile` | float | Percentile vs. historical levels at this site |
| `activity_level` | string | very_low / low / moderate / high / very_high |
| `population_served` | integer | Population covered by sampling site |

### Query Example (SoQL)

```
GET https://data.cdc.gov/resource/ymmh-divb.json?$where=wwtp_jurisdiction='TX' AND date_end > '2026-06-01'&$order=date_end DESC&$limit=1000
```

### How We Use It

Wastewater data provides **early signal confirmation** — viral RNA appears in wastewater 4-6 days before clinical cases increase. We use it to:
1. Confirm Delphi ED visit signals (cross-validation)
2. Detect emerging signals before they appear in ED data
3. Provide geographic granularity (county-level via WWTP coverage)

### Limitations

- Not all counties have wastewater sampling sites
- 15-day rolling metrics mean signal is smoothed (less sensitive to rapid changes)
- Some sites have limited historical data (WVAL can't be calculated)
- Wastewater detects presence but can't distinguish human vs. animal source (relevant for flu)

---

## 3. CDC NSSP ED Visit Proportions (SUPPLEMENTAL — State-level)

| Field | Value |
|-------|-------|
| **Provider** | CDC National Syndromic Surveillance Program (NSSP) |
| **API** | Socrata Open Data API (SODA) on `data.cdc.gov` |
| **Dataset ID** | `rdmq-nq56` |
| **Name** | Inpatient, Emergency Department, and Outpatient Visits for Respiratory Illnesses |
| **Auth** | None required |
| **Rate Limits** | Same as NWSS (1K/40K per hour) |
| **Update Frequency** | Weekly on Fridays |
| **Endpoint** | `https://data.cdc.gov/resource/rdmq-nq56.json` |
| **Status** | ✅ Active |

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `geography` | string | State name or "National" |
| `pathogen` | string | "Influenza", "COVID-19", "RSV", "ARI" |
| `week_end` | date | End of epiweek (Saturday) |
| `percent` | float | % of ED visits for this pathogen |
| `visit_type` | string | "ed", "inpatient", "outpatient" |

### Query Example

```
GET https://data.cdc.gov/resource/rdmq-nq56.json?$where=geography='Texas' AND pathogen='Influenza' AND visit_type='ed' AND week_end > '2026-01-01'&$order=week_end DESC&$limit=100
```

### How We Use It

This provides **state-level context** for alert generation:
1. Current state activity level (for inclusion in situation briefs)
2. Redundancy for Delphi API (same underlying NSSP data, different access path)
3. National baseline comparison

### Relationship to Delphi API

The NSSP data on data.cdc.gov and the CMU Delphi Epidata API **share the same underlying data source** (NSSP surveillance system). The difference:
- **Delphi**: Provides MSA-level (metro area) granularity — essential for leader detection
- **CDC direct**: Provides state/national-level only — used for context and redundancy

We use BOTH because Delphi can go down (academic project, no SLA) and the CDC endpoint provides official state-level context for alert narratives.

---

## Data Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                     HealthSignals Data Flow                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────┐   PRIMARY (metro-level detection)              │
│  │ CMU Delphi   │──▶ MSA-level % ED visits                      │
│  │ Epidata API  │   (flu, COVID, RSV by metro)                   │
│  └──────────────┘                                                │
│                                                                   │
│  ┌──────────────┐   SUPPLEMENTAL (early confirmation)            │
│  │ CDC NWSS     │──▶ Wastewater viral RNA levels                 │
│  │ Wastewater   │   (county-level via WWTP FIPS)                 │
│  └──────────────┘                                                │
│                                                                   │
│  ┌──────────────┐   SUPPLEMENTAL (state context)                 │
│  │ CDC NSSP     │──▶ State-level % ED visits                     │
│  │ ED Visits    │   (Texas + National baselines)                  │
│  └──────────────┘                                                │
│                                                                   │
│  All sources ──▶ S3 Data Lake ──▶ Prediction Pipeline            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Registering for a Socrata App Token (Optional)

To increase rate limits from 1,000 to 40,000 requests/hour:

1. Create account at https://data.cdc.gov/signup
2. Go to Developer Settings → Create New App Token
3. Set the `CDC_SOCRATA_APP_TOKEN` environment variable in Lambda configuration

This is optional — HealthSignals makes ~15-20 API calls per weekly run, well within unauthenticated limits.

---

## Data Freshness & Reliability Matrix

| Source | Update Day | Lag | Reliability | Fallback |
|--------|-----------|-----|-------------|----------|
| CMU Delphi | Daily | 1-3 days | Medium (no SLA) | CDC NSSP direct |
| CDC Wastewater | Friday | 7 days | High (CDC operated) | — |
| CDC NSSP ED | Friday | 7 days | High (CDC operated) | Delphi API |

---

*Last validated: 2026-07-02*
*All endpoints confirmed active and returning data as documented.*

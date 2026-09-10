# Data Sources — Amazon HealthSignals

All data sources are publicly available, require no authentication for basic access, and contain no PHI (Protected Health Information). No data sharing agreements or HIPAA compliance required.

---

## 1. CMU Delphi Epidata API (PRIMARY — Metro-level)

| Field                | Value                                                          |
| -------------------- | -------------------------------------------------------------- |
| **Provider**         | Carnegie Mellon University Delphi Group                        |
| **Endpoint**         | `https://api.delphi.cmu.edu/epidata/covidcast/`                |
| **Auth**             | None required (public, grant-funded)                           |
| **Rate Limits**      | Undocumented; academic courtesy applies                        |
| **Update Frequency** | Daily (we fetch weekly)                                        |
| **SLA**              | **None** — academic project, no uptime guarantee               |
| **Documentation**    | https://cmu-delphi.github.io/delphi-epidata/api/covidcast.html |
| **Status**           | ✅ Validated live (July 2026)                                  |

### Signals Used

| Signal                                  | Data Source | Description                      |
| --------------------------------------- | ----------- | -------------------------------- |
| `nssp:pct_ed_visits_influenza`          | NSSP        | % ED visits for influenza        |
| `nssp:pct_ed_visits_covid`              | NSSP        | % ED visits for COVID-19         |
| `nssp:pct_ed_visits_rsv`                | NSSP        | % ED visits for RSV              |
| `nssp:smoothed_pct_ed_visits_influenza` | NSSP        | 3-week moving average of flu %   |
| `nssp:smoothed_pct_ed_visits_covid`     | NSSP        | 3-week moving average of COVID % |
| `nssp:smoothed_pct_ed_visits_rsv`       | NSSP        | 3-week moving average of RSV %   |

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

| Field                | Value                                                          |
| -------------------- | -------------------------------------------------------------- |
| **Provider**         | CDC National Wastewater Surveillance System (NWSS)             |
| **API**              | Socrata Open Data API (SODA) on `data.cdc.gov`                 |
| **Auth**             | None required; optional app token for higher rate limits       |
| **Rate Limits**      | 1,000 req/hr (unauthenticated), 40,000 req/hr (with app token) |
| **Update Frequency** | Weekly on Fridays                                              |
| **Documentation**    | https://dev.socrata.com/docs/queries/                          |
| **CDC Info**         | https://www.cdc.gov/nwss/index.html                            |
| **Status**           | ✅ Active, ~1,500 sampling sites nationwide                    |

### Dataset (Socrata Identifier)

We use a single **unified** NWSS dataset that covers all three pathogens with a
site-level viral activity level (WVAL) metric:

| Pathogen(s)                      | Dataset ID  | Name                                                                    | Endpoint                                       |
| -------------------------------- | ----------- | ----------------------------------------------------------------------- | ---------------------------------------------- |
| **SARS-CoV-2, Influenza A, RSV** | `atcp-73re` | CDC Wastewater Viral Activity Level for SARS-CoV-2, Influenza A and RSV | `https://data.cdc.gov/resource/atcp-73re.json` |

Select the pathogen via the `pathogen_target` field:

| disease_key | `pathogen_target` value |
| ----------- | ----------------------- |
| `covid`     | `SARS-CoV-2`            |
| `influenza` | `Influenza A virus`     |
| `rsv`       | `RSV`                   |

> **History (2026-09):** CDC changed the upstream NWSS datasets. The prior
> per-disease datasets used incompatible schemas — `2ew6-ywp6` (SARS-CoV-2
> metric) worked, but `ymmh-divb` (flu) and `45cq-cw4i` (rsv) were raw
> lab-sample datasets keyed by `state_territory`/`sample_collect_date` with no
> jurisdiction summary. Consolidating onto `atcp-73re` gives one consistent
> schema across all three pathogens.

### Key Fields

| Field                | Type    | Description                                   |
| -------------------- | ------- | --------------------------------------------- |
| `state_territory`    | string  | Full state name (e.g., "Texas")               |
| `pathogen_target`    | string  | `SARS-CoV-2` / `Influenza A virus` / `RSV`    |
| `counties_served`    | string  | County name(s) served by this site (not FIPS) |
| `site`               | string  | Site identifier                               |
| `week_end`           | date    | End of the reporting week                     |
| `site_wval`          | float   | Site viral activity level (numeric)           |
| `site_wval_category` | string  | Very Low / Low / Moderate / High / Very High  |
| `population_served`  | integer | Population covered by sampling site           |

### Query Example (SoQL)

```
GET https://data.cdc.gov/resource/atcp-73re.json?$where=state_territory='Texas' AND pathogen_target='Influenza A virus' AND week_end > '2026-06-01'&$order=week_end DESC&$limit=1000
```

> County matching is by **name** (`counties_served`), since this dataset
> exposes no county FIPS. The fetcher matches against each sentinel metro's
> configured `county_names`.

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

| Field                | Value                                                           |
| -------------------- | --------------------------------------------------------------- |
| **Provider**         | CDC National Syndromic Surveillance Program (NSSP)              |
| **API**              | Socrata Open Data API (SODA) on `data.cdc.gov`                  |
| **Dataset ID**       | `vutn-jzwm`                                                     |
| **Name**             | NSSP Emergency Department Visits - COVID-19, Flu, RSV, Combined |
| **Auth**             | None required                                                   |
| **Rate Limits**      | Same as NWSS (1K/40K per hour)                                  |
| **Update Frequency** | Weekly on Fridays                                               |
| **Endpoint**         | `https://data.cdc.gov/resource/vutn-jzwm.json`                  |
| **Status**           | ✅ Active                                                       |

> **History (2026-09):** CDC repurposed the previous dataset ID `rdmq-nq56`
> into a trajectories/trends table (columns `ed_trends_*`, no `pathogen` or
> `percent`), which broke the fetcher. Repointed to `vutn-jzwm`, which exposes
> the state-level ED-visit percentages we need.

### Key Fields

| Field            | Type   | Description                                 |
| ---------------- | ------ | ------------------------------------------- |
| `geography`      | string | State name, or "United States" for national |
| `pathogen`       | string | "Influenza", "COVID-19", "RSV", "Combined"  |
| `week_end`       | date   | End of epiweek (Saturday)                   |
| `percent_visits` | float  | % of ED visits for this pathogen            |

> Note: this dataset has **no `visit_type` column** (it is ED-visit
> percentages by construction), and the national geography label is
> **"United States"**, not "National".

### Query Example

```
GET https://data.cdc.gov/resource/vutn-jzwm.json?$where=geography='Texas' AND pathogen='Influenza' AND week_end > '2026-01-01'&$order=week_end DESC&$limit=100
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

| Source         | Update Day | Lag      | Reliability         | Fallback        |
| -------------- | ---------- | -------- | ------------------- | --------------- |
| CMU Delphi     | Daily      | 1-3 days | Medium (no SLA)     | CDC NSSP direct |
| CDC Wastewater | Friday     | 7 days   | High (CDC operated) | —               |
| CDC NSSP ED    | Friday     | 7 days   | High (CDC operated) | Delphi API      |

---

## 4. openFDA Drug Shortages API (PLUGIN — Drug Shortage Module)

| Field                | Value                                               |
| -------------------- | --------------------------------------------------- |
| **Provider**         | U.S. Food and Drug Administration (FDA)             |
| **Endpoint**         | `https://api.fda.gov/drug/shortages.json`           |
| **Auth**             | None required (public API)                          |
| **Rate Limits**      | 240 requests/hour (unauthenticated)                 |
| **Update Frequency** | Weekly (Monday 6 AM UTC via EventBridge)            |
| **SLA**              | None — public API, schema may change without notice |
| **Documentation**    | https://open.fda.gov/apis/drug/shortages/           |
| **Status**           | Active (~1,638 records as of 2024)                  |

### How We Use It

The Drug Shortage Intelligence module polls this API weekly to:

1. Detect NEW shortages in monitored therapeutic categories
2. Detect WORSENING supply status changes
3. Detect RESOLVED shortages
4. Enrich disease outbreak alerts with medication availability context

### Key Fields

| Field                  | Description                    |
| ---------------------- | ------------------------------ |
| `generic_name`         | Generic drug name              |
| `proprietary_name`     | Brand name                     |
| `status`               | Current shortage status        |
| `availability`         | Supply availability            |
| `initial_posting_date` | When shortage was first posted |
| `update_date`          | Most recent update             |

### Limitations

- No SLA — FDA may change schema without notice
- No real-time webhooks (polling only)
- Cannot distinguish between shortage severity levels from API alone (requires change detection logic)

This data source is only active when `enable_drug_shortage: true` in `cdk/cdk.json`.

---

_Last validated: 2026-07-02_
_All endpoints confirmed active and returning data as documented._

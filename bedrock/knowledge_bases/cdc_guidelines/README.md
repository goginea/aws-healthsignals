# CDC Guidelines Knowledge Base

This directory contains reference documents for the **factual precision retrieval** Knowledge Base in Amazon HealthSignals. Bedrock retrieves specific CDC guidance when generating preparation checklists and situation briefs.

## Documents

| File | Content | Size | Source |
|------|---------|------|--------|
| `cdc_flu_preparedness.md` | Influenza planning for local health departments: ILI definition, stockpile items, staffing surge, community mitigation, vaccination timing, EOC activation, school closure framework | 8 KB | CDC Pandemic Influenza Plan, Community Mitigation Guidelines |
| `cdc_rsv_guidance.md` | RSV prevention and response: high-risk populations, nirsevimab/palivizumab, hospital capacity planning, pediatric thresholds, LTCF protocols | 6.5 KB | CDC RSV Prevention, ACIP Recommendations |
| `cdc_covid_current.md` | Current COVID-19 guidance (post-PHE): updated isolation guidance, Paxlovid treatment window, wastewater interpretation, variant monitoring, testing strategy | 7 KB | CDC Respiratory Virus Guidance (2024 update) |
| `cdc_activity_levels.md` | Respiratory virus activity level definitions: ARI metric, threshold methodology, percentile-based levels, what each level means for action | 6.7 KB | CDC Respiratory Virus Activity Levels FAQ |
| `cdc_communication_principles.md` | Crisis and Emergency Risk Communication (CERC): 6 principles, message mapping, uncertainty language patterns, tone guidelines by severity | 7.4 KB | CDC CERC Manual (2018) |
| `rural_health_resources.md` | Rural health preparedness: HRSA definitions, HPSA considerations, mutual aid, telehealth surge, MRC activation, CAH capacity, state notification thresholds | 8.5 KB | NACCHO, ASTHO, HRSA |

## Knowledge Base Configuration

**Retrieval Strategy:** Precision — return the most relevant chunk for the query
**Chunking:** Semantic chunking (Bedrock default) — respects markdown headers as section boundaries
**Embedding Model:** Amazon Titan Embeddings V2
**Vector Store:** Amazon OpenSearch Serverless (recommended) or Pinecone

## Upload Instructions

To populate this Knowledge Base in Bedrock:

1. Create an S3 bucket: `healthsignals-kb-cdc-guidelines-{account_id}`
2. Upload all `.md` files from this directory to the bucket root
3. In Bedrock Console → Knowledge Bases → Create:
   - Name: `healthsignals-cdc-guidelines`
   - Data source: S3 bucket above
   - Chunking strategy: Default (semantic)
   - Embedding model: Titan Embeddings V2
4. Sync the data source
5. Note the Knowledge Base ID and add to `config/system.json`

## Content Maintenance

- **Update frequency:** Review annually before respiratory season (August/September)
- **COVID guidance:** Update when CDC issues significant guidance changes
- **RSV guidance:** Update when new immunization products are approved/recommended
- **Flu guidance:** Stable — update only for major framework changes
- **Activity levels:** Stable — methodology rarely changes

## Content Principles

1. **Factual only** — No opinions, no HealthSignals-specific language
2. **Structured for retrieval** — Clear headers, tables, numbered lists
3. **Source-attributed** — Every document cites its CDC/government source
4. **Actionable** — Focus on what local health departments DO, not just what they know
5. **Current** — Dated guidance clearly marked; updated when CDC changes recommendations

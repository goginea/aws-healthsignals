# Communication Templates Knowledge Base

This directory contains professional communication templates for the **variety retrieval** Knowledge Base in Amazon HealthSignals. Bedrock retrieves template styles and formats, then adapts them with situation-specific content.

## Documents

| File | Content | Templates | Size |
|------|---------|-----------|------|
| `alert_email_templates.md` | Severity-graded email alerts for health officers: LOW (routine update), MODERATE (preparation advisory), HIGH (urgent alert), CRITICAL (emergency notification), ALL-CLEAR (stand down) | 5 templates | 10 KB |
| `sms_alert_templates.md` | SMS messages ≤160 characters for each severity level + weekly update + feedback request | 7 templates | 3 KB |
| `public_announcement_templates.md` | Community-facing communications: press release, Facebook posts (3 severity levels), community meeting talking points, school notification letter, healthcare provider advisory | 5 templates | 10.3 KB |
| `partner_notification_templates.md` | Inter-agency coordination: state health dept escalation, hospital/clinic notification, EMS advisory, school superintendent letter, LTCF alert, pharmacy supply advisory | 6 templates | 11.3 KB |
| `follow_up_templates.md` | Ongoing communications: weekly situation update, accuracy feedback request, season wrap-up summary, lessons learned survey | 4 templates | 9.3 KB |

**Total: 27 templates across 5 categories**

## Knowledge Base Configuration

**Retrieval Strategy:** Variety — retrieve multiple template options for the model to synthesize
**Chunking:** Fixed-size chunking at 1000 tokens with 200 token overlap (keeps templates intact)
**Embedding Model:** Amazon Titan Embeddings V2
**Vector Store:** Amazon OpenSearch Serverless (recommended) or Pinecone

## Upload Instructions

1. Create an S3 bucket: `healthsignals-kb-comms-templates-{account_id}`
2. Upload all `.md` files from this directory to the bucket root
3. In Bedrock Console → Knowledge Bases → Create:
   - Name: `healthsignals-communication-templates`
   - Data source: S3 bucket above
   - Chunking: Fixed size, 1000 tokens, 200 overlap
   - Embedding model: Titan Embeddings V2
4. Sync the data source
5. Note the Knowledge Base ID and add to `config/system.json`

## How Bedrock Uses These Templates

1. **Step 4 (Communication Drafting)** in the Step Functions workflow queries this KB
2. Retrieval query: "email template for [SEVERITY] [DISEASE] alert to rural health officer"
3. Bedrock receives 2-3 relevant template chunks
4. The model adapts the template structure while filling in dynamic content from previous steps
5. Guardrails verify the output includes mandatory disclaimers before delivery

## Template Design Principles

1. **Placeholder-driven** — `[PLACEHOLDER]` markers for dynamic content
2. **Severity-appropriate tone** — from calm/informational to urgent/direct
3. **Plain language** — 8th grade reading level for community-facing templates
4. **Actionable** — Every template includes specific next steps
5. **Disclaimer-included** — Confidence level and advisory language mandatory
6. **Professional** — Government health communication standards
7. **Accessible** — No jargon in community templates; clinical precision in provider templates

## Content Maintenance

- **Review annually** before respiratory season (August)
- **Add templates** when new communication needs emerge (e.g., new partner type)
- **Test with users** — get feedback from actual county health officers on tone/usefulness
- **Update contact patterns** — phone vs email vs text preferences may shift

## Customization

States deploying HealthSignals should review these templates and customize:
- State-specific reporting requirements
- Local emergency contact numbers
- State-specific regulatory language
- Cultural/demographic considerations for their counties
- Language translation needs

Custom templates can be added to the KB alongside defaults — retrieval will return the most relevant match.

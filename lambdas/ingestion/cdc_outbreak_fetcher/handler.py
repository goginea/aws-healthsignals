"""CDC Outbreak Fetcher — Polls CDC Outbreaks RSS feed and extracts structured data via Bedrock.

Trigger: EventBridge daily schedule (8 AM UTC) or manual invocation.

Flow:
    1. Fetch CDC Outbreaks RSS feed (XML)
    2. Parse RSS items (title, link, pubDate, category)
    3. Compare against DynamoDB state table to identify NEW/UPDATED outbreaks
    4. For each new/updated outbreak:
       a. Fetch linked CDC investigation page (HTML → text)
       b. Invoke Bedrock to extract structured data (states, case counts, food source)
       c. Store extracted data to S3
    5. Invoke outbreak processor Lambda with the extracted data
    6. Update DynamoDB state table

Environment Variables:
    DATA_BUCKET: S3 bucket for storing outbreak data
    CONFIG_BUCKET: S3 bucket for config files
    CONFIG_PREFIX: S3 key prefix for configs
    OUTBREAK_STATE_TABLE: DynamoDB table tracking known outbreaks
    OUTBREAK_PROCESSOR_FUNCTION: Lambda function name for the outbreak processor
    BEDROCK_MODEL_ID: Model for content extraction (default: Claude Sonnet 4.5)
    LOG_LEVEL: Logging level
"""
import json
import os
import sys
import logging
import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

import boto3
import urllib3

# Add shared module to path
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import get_data_source_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
bedrock = boto3.client("bedrock-runtime")
cloudwatch = boto3.client("cloudwatch")

# Configuration
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
OUTBREAK_STATE_TABLE = os.environ.get("OUTBREAK_STATE_TABLE", "healthsignals-cdc-outbreak-state")
OUTBREAK_PROCESSOR_FUNCTION = os.environ.get("OUTBREAK_PROCESSOR_FUNCTION", "healthsignals-outbreak-processor")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

METRIC_NAMESPACE = "HealthSignals/CDCOutbreaks"
FUNCTION_NAME = "cdc_outbreak_fetcher"

state_table = dynamodb.Table(OUTBREAK_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch CDC outbreak RSS, detect changes, extract data, invoke processor.

    Input event (from EventBridge or manual):
    {
        "source": "scheduled" | "manual"
    }

    Returns:
    {
        "statusCode": 200,
        "outbreaks_found": N,
        "new_outbreaks": N,
        "updated_outbreaks": N,
        "processed": [...]
    }
    """
    logger.info(f"CDC Outbreak Fetcher invoked: {json.dumps(event)}")

    # Load data source config
    try:
        config = get_data_source_config("cdc_outbreaks_rss")
    except Exception:
        # Fallback to defaults if config not yet uploaded
        config = {"api": {"base_url": "https://tools.cdc.gov/api/v2/resources/media/285676.rss"}}

    rss_url = config.get("api", {}).get("base_url", "https://tools.cdc.gov/api/v2/resources/media/285676.rss")
    timeout = config.get("api", {}).get("timeout_seconds", 30)

    # 1. Fetch RSS feed
    rss_items = fetch_rss_feed(rss_url, timeout)
    logger.info(f"RSS feed returned {len(rss_items)} items")

    if not rss_items:
        emit_metrics(outbreaks_found=0, new_count=0, updated_count=0)
        return {"statusCode": 200, "outbreaks_found": 0, "new_outbreaks": 0, "updated_outbreaks": 0}

    # 2. Compare against known state
    new_outbreaks, updated_outbreaks = detect_changes(rss_items)
    logger.info(f"Changes: {len(new_outbreaks)} new, {len(updated_outbreaks)} updated")

    # 3. Process each new/updated outbreak
    processed = []
    actionable = new_outbreaks + updated_outbreaks

    for outbreak in actionable:
        try:
            result = process_outbreak(outbreak)
            if result:
                processed.append(result)
        except Exception as e:
            logger.error(f"Failed to process outbreak '{outbreak['title']}': {e}", exc_info=True)

    # 4. Emit metrics
    emit_metrics(
        outbreaks_found=len(rss_items),
        new_count=len(new_outbreaks),
        updated_count=len(updated_outbreaks),
    )

    return {
        "statusCode": 200,
        "outbreaks_found": len(rss_items),
        "new_outbreaks": len(new_outbreaks),
        "updated_outbreaks": len(updated_outbreaks),
        "processed": processed,
    }


# === RSS Fetching ===


def fetch_rss_feed(url: str, timeout: int = 30) -> list[dict]:
    """Fetch and parse CDC Outbreaks RSS feed.

    Returns list of dicts with: title, link, pub_date, category, outbreak_id
    """
    try:
        response = http.request("GET", url, timeout=timeout)
        if response.status != 200:
            logger.error(f"RSS fetch failed: HTTP {response.status}")
            return []

        xml_content = response.data.decode("utf-8")
        return parse_rss_xml(xml_content)

    except Exception as e:
        logger.error(f"RSS fetch error: {e}")
        return []


def parse_rss_xml(xml_content: str) -> list[dict]:
    """Parse RSS 2.0 XML into list of outbreak items."""
    items = []
    try:
        root = ET.fromstring(xml_content)
        channel = root.find("channel")
        if channel is None:
            return []

        for item_el in channel.findall("item"):
            title = item_el.findtext("title", "").strip()
            # Strip HTML tags from title (e.g., <em>E. coli</em>)
            title = re.sub(r"<[^>]+>", "", title).strip()
            link = item_el.findtext("link", "").strip()
            pub_date = item_el.findtext("pubDate", "").strip()
            category = item_el.findtext("category", "Outbreaks").strip()

            # Generate stable outbreak_id from title (slugified)
            outbreak_id = generate_outbreak_id(title)

            items.append({
                "outbreak_id": outbreak_id,
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "category": category,
            })

    except ET.ParseError as e:
        logger.error(f"RSS XML parse error: {e}")

    return items


def generate_outbreak_id(title: str) -> str:
    """Generate a stable, unique outbreak ID from the title.

    Uses a slugified version of the title for readability,
    with a short hash suffix for uniqueness.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    hash_suffix = hashlib.md5(title.encode()).hexdigest()[:8]
    return f"{slug}-{hash_suffix}"


# === Change Detection ===


def detect_changes(rss_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Compare RSS items against DynamoDB state to find new/updated outbreaks.

    Returns:
        (new_outbreaks, updated_outbreaks) — each is a list of item dicts
    """
    new_outbreaks = []
    updated_outbreaks = []

    for item in rss_items:
        outbreak_id = item["outbreak_id"]

        try:
            response = state_table.get_item(Key={"outbreak_id": outbreak_id})
            existing = response.get("Item")

            if not existing:
                # New outbreak — never seen before
                new_outbreaks.append(item)
            elif existing.get("pub_date") != item["pub_date"]:
                # Updated — pubDate changed (CDC updated the page)
                item["previous_states"] = existing.get("affected_states", [])
                updated_outbreaks.append(item)
            # else: unchanged, skip

        except Exception as e:
            logger.error(f"DynamoDB lookup failed for {outbreak_id}: {e}")
            # Treat as new if we can't check state
            new_outbreaks.append(item)

    return new_outbreaks, updated_outbreaks


# === Outbreak Processing ===


def process_outbreak(outbreak: dict) -> dict | None:
    """Process a single outbreak: fetch page, extract via Bedrock, store, invoke processor.

    Returns extracted data dict, or None on failure.
    """
    outbreak_id = outbreak["outbreak_id"]
    link = outbreak["link"]
    title = outbreak["title"]

    logger.info(f"Processing outbreak: {title} ({outbreak_id})")

    # Fetch the linked CDC investigation page
    page_content = fetch_outbreak_page(link)
    if not page_content:
        logger.warning(f"Could not fetch page for {title}")
        return None

    # Extract structured data using Bedrock
    extracted = extract_with_bedrock(page_content, title)
    if not extracted:
        logger.warning(f"Bedrock extraction failed for {title}")
        return None

    # Enrich with RSS metadata
    extracted["outbreak_id"] = outbreak_id
    extracted["title"] = title
    extracted["cdc_link"] = link
    extracted["pub_date"] = outbreak["pub_date"]
    extracted["category"] = outbreak["category"]
    extracted["previous_states"] = outbreak.get("previous_states", [])
    extracted["fetched_at"] = datetime.now(timezone.utc).isoformat()

    # Store to S3
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s3_key = f"raw/cdc-outbreaks/{date_str}/{outbreak_id}.json"
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=s3_key,
        Body=json.dumps(extracted, default=str),
        ContentType="application/json",
    )
    logger.info(f"Stored outbreak data: s3://{DATA_BUCKET}/{s3_key}")

    # Update DynamoDB state
    update_outbreak_state(extracted)

    # Invoke outbreak processor
    invoke_processor(extracted)

    return {
        "outbreak_id": outbreak_id,
        "title": title,
        "affected_states": extracted.get("affected_states", []),
        "case_count": extracted.get("case_count"),
    }


def fetch_outbreak_page(link: str) -> str | None:
    """Fetch a CDC outbreak investigation page and return text content.

    The link from RSS may be a redirect URL. We follow redirects to get
    the actual investigation page, then strip HTML to plain text.
    """
    try:
        # Follow redirects (CDC links redirect to investigation pages)
        response = http.request("GET", link, timeout=30, redirect=True)
        if response.status != 200:
            logger.warning(f"Page fetch failed: HTTP {response.status} for {link}")
            return None

        html = response.data.decode("utf-8", errors="replace")
        # Strip HTML tags to get plain text for Bedrock
        text = strip_html_to_text(html)

        # Limit to first 8000 chars (Bedrock input limit and cost management)
        return text[:8000]

    except Exception as e:
        logger.error(f"Page fetch error for {link}: {e}")
        return None


def strip_html_to_text(html: str) -> str:
    """Strip HTML tags and normalize whitespace to get plain text."""
    # Remove script and style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# === Bedrock Extraction ===


EXTRACTION_PROMPT = """You are a public health data extraction assistant. Read the following CDC outbreak investigation page content and extract structured information.

Return ONLY a valid JSON object with these fields:
{
  "disease_name": "string — the disease or pathogen name (e.g., Cyclosporiasis, Salmonella, E. coli)",
  "affected_states": ["array of US state names mentioned as having cases"],
  "case_count": number or null if not stated,
  "hospitalizations": number or null if not stated,
  "deaths": number or null if not stated,
  "source_food": "string — the food item linked to the outbreak, or 'Unknown' if not identified",
  "onset_date": "string — earliest reported symptom onset date, or null",
  "status": "active" or "resolved",
  "summary": "1-2 sentence summary of the outbreak situation"
}

Rules:
- Extract ONLY information explicitly stated in the content
- Use full state names (e.g., "Michigan" not "MI")
- If case count says "more than X", use X as the number
- If no states are mentioned, return an empty array
- If a field is not mentioned, use null
- Do NOT hallucinate or infer information not present in the text

CDC Page Content:
"""


def extract_with_bedrock(page_content: str, title: str) -> dict | None:
    """Invoke Bedrock to extract structured outbreak data from page content.

    Returns parsed dict or None on failure.
    """
    try:
        prompt = EXTRACTION_PROMPT + page_content

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "thinking": {"type": "disabled"},
        })

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read().decode())
        content_text = result.get("content", [{}])[0].get("text", "")

        # Parse the JSON from Bedrock's response
        # Handle case where Bedrock wraps JSON in markdown code block
        json_text = content_text.strip()
        if json_text.startswith("```"):
            json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
            json_text = re.sub(r"\s*```$", "", json_text)

        extracted = json.loads(json_text)
        logger.info(f"Bedrock extracted: {extracted.get('disease_name')}, "
                    f"{len(extracted.get('affected_states', []))} states, "
                    f"{extracted.get('case_count')} cases")
        return extracted

    except json.JSONDecodeError as e:
        logger.error(f"Bedrock response not valid JSON for '{title}': {e}")
        return None
    except Exception as e:
        logger.error(f"Bedrock extraction failed for '{title}': {e}")
        return None


# === DynamoDB State Management ===


def update_outbreak_state(extracted: dict) -> None:
    """Update or create the outbreak state record in DynamoDB."""
    try:
        state_table.put_item(Item={
            "outbreak_id": extracted["outbreak_id"],
            "title": extracted["title"],
            "pub_date": extracted["pub_date"],
            "category": extracted["category"],
            "disease_name": extracted.get("disease_name", "Unknown"),
            "affected_states": extracted.get("affected_states", []),
            "case_count": extracted.get("case_count"),
            "status": extracted.get("status", "active"),
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "cdc_link": extracted.get("cdc_link", ""),
        })
    except Exception as e:
        logger.error(f"DynamoDB state update failed for {extracted['outbreak_id']}: {e}")


# === Invoke Processor ===


def invoke_processor(extracted: dict) -> None:
    """Invoke the outbreak processor Lambda asynchronously."""
    try:
        lambda_client.invoke(
            FunctionName=OUTBREAK_PROCESSOR_FUNCTION,
            InvocationType="Event",  # Async invocation
            Payload=json.dumps(extracted, default=str),
        )
        logger.info(f"Invoked processor for: {extracted['title']}")
    except Exception as e:
        logger.error(f"Failed to invoke processor for {extracted['title']}: {e}")


# === CloudWatch Metrics ===


def emit_metrics(outbreaks_found: int, new_count: int, updated_count: int) -> None:
    """Emit CloudWatch metrics for monitoring."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "outbreaks_in_rss_feed",
                    "Value": outbreaks_found,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "FunctionName", "Value": FUNCTION_NAME}],
                },
                {
                    "MetricName": "new_outbreaks_detected",
                    "Value": new_count,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "FunctionName", "Value": FUNCTION_NAME}],
                },
                {
                    "MetricName": "updated_outbreaks_detected",
                    "Value": updated_count,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "FunctionName", "Value": FUNCTION_NAME}],
                },
            ],
        )
    except Exception as e:
        logger.error(f"Metrics emission failed: {e}")

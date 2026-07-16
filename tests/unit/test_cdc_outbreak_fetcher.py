"""Unit tests for CDC Outbreak Fetcher Lambda.

Tests RSS parsing, change detection, Bedrock extraction, and outbreak ID generation.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from tests.conftest import load_handler


MOCK_SYSTEM = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"},
}

MOCK_RSS_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
<channel>
<title>CDC Outbreaks - US Based</title>
<item>
  <title>Cyclosporiasis Outbreak with Unknown Source</title>
  <description>Cyclosporiasis Outbreak with Unknown Source</description>
  <link>https://tools.cdc.gov/api/embed/downloader/download.asp?m=285676&amp;c=765996</link>
  <pubDate>Tue, 14 Jul 2026 17:09:00 GMT</pubDate>
  <category>Outbreaks</category>
</item>
<item>
  <title>&lt;em&gt;E. coli&lt;/em&gt; Outbreak Linked to Frozen Blueberries</title>
  <description>E. coli Outbreak Linked to Frozen Blueberries</description>
  <link>https://tools.cdc.gov/api/embed/downloader/download.asp?m=285676&amp;c=765972</link>
  <pubDate>Tue, 07 Jul 2026 16:34:00 GMT</pubDate>
  <category>E. coli Infection</category>
</item>
</channel>
</rss>"""

MOCK_BEDROCK_RESPONSE = {
    "content": [{
        "text": json.dumps({
            "disease_name": "Cyclosporiasis",
            "affected_states": ["Michigan", "Ohio", "West Virginia", "Kentucky"],
            "case_count": 400,
            "hospitalizations": None,
            "deaths": None,
            "source_food": "Unknown",
            "onset_date": "2026-06-22",
            "status": "active",
            "summary": "Large multistate outbreak of cyclosporiasis in 4 midwestern states."
        })
    }]
}


@pytest.fixture(scope="module")
def handler():
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    os.environ["DATA_BUCKET"] = "healthsignals-data-test"
    os.environ["OUTBREAK_STATE_TABLE"] = "healthsignals-cdc-outbreak-state-test"
    os.environ["OUTBREAK_PROCESSOR_FUNCTION"] = "healthsignals-outbreak-processor"

    return load_handler(
        "ingestion/cdc_outbreak_fetcher",
        extra_patches={
            "shared.config_loader.get_data_source_config": {
                "api": {"base_url": "https://rss.test/feed", "timeout_seconds": 30}
            },
            "boto3.client": MagicMock(),
            "boto3.resource": MagicMock(return_value=mock_dynamo),
        },
    )


class TestRSSParsing:
    """Test RSS XML parsing into structured outbreak items."""

    def test_parse_rss_xml_extracts_items(self, handler):
        """Correctly parses 2 items from valid RSS XML."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        assert len(items) == 2

    def test_parse_rss_xml_strips_html_from_title(self, handler):
        """HTML tags like <em> are removed from titles."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        ecoli_item = next(i for i in items if "coli" in i["title"].lower())
        assert "<em>" not in ecoli_item["title"]
        assert "E. coli" in ecoli_item["title"]

    def test_parse_rss_xml_extracts_link(self, handler):
        """Link URL is extracted correctly."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        assert items[0]["link"].startswith("https://")

    def test_parse_rss_xml_extracts_pub_date(self, handler):
        """pubDate is extracted as string."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        assert "Jul 2026" in items[0]["pub_date"]

    def test_parse_rss_xml_extracts_category(self, handler):
        """Category is extracted from item."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        assert items[0]["category"] == "Outbreaks"

    def test_parse_rss_xml_generates_outbreak_id(self, handler):
        """Each item gets a stable outbreak_id."""
        items = handler.parse_rss_xml(MOCK_RSS_XML)
        assert items[0]["outbreak_id"]
        assert "-" in items[0]["outbreak_id"]

    def test_parse_rss_xml_empty_feed(self, handler):
        """Empty RSS feed returns empty list."""
        empty_xml = '<?xml version="1.0"?><rss><channel></channel></rss>'
        items = handler.parse_rss_xml(empty_xml)
        assert items == []

    def test_parse_rss_xml_invalid_xml(self, handler):
        """Invalid XML returns empty list without crashing."""
        items = handler.parse_rss_xml("not valid xml <><>")
        assert items == []


class TestOutbreakIdGeneration:
    """Test stable outbreak ID generation."""

    def test_generate_outbreak_id_deterministic(self, handler):
        """Same title produces same ID."""
        id1 = handler.generate_outbreak_id("Cyclosporiasis Outbreak")
        id2 = handler.generate_outbreak_id("Cyclosporiasis Outbreak")
        assert id1 == id2

    def test_generate_outbreak_id_different_for_different_titles(self, handler):
        """Different titles produce different IDs."""
        id1 = handler.generate_outbreak_id("Cyclosporiasis Outbreak")
        id2 = handler.generate_outbreak_id("E. coli Outbreak")
        assert id1 != id2

    def test_generate_outbreak_id_max_length(self, handler):
        """ID is bounded in length."""
        long_title = "A" * 200
        outbreak_id = handler.generate_outbreak_id(long_title)
        assert len(outbreak_id) <= 70  # 60 slug + dash + 8 hash


class TestChangeDetection:
    """Test new vs. updated outbreak detection."""

    def test_new_outbreak_detected(self, handler):
        """Item not in DynamoDB is classified as new."""
        handler.state_table.get_item.return_value = {}  # No Item key = not found

        items = [{"outbreak_id": "test-123", "title": "Test", "pub_date": "Mon, 01 Jul 2026", "link": "", "category": ""}]
        new, updated = handler.detect_changes(items)

        assert len(new) == 1
        assert len(updated) == 0

    def test_unchanged_outbreak_skipped(self, handler):
        """Item with same pubDate is unchanged — not returned."""
        handler.state_table.get_item.return_value = {
            "Item": {"outbreak_id": "test-123", "pub_date": "Mon, 01 Jul 2026"}
        }

        items = [{"outbreak_id": "test-123", "title": "Test", "pub_date": "Mon, 01 Jul 2026", "link": "", "category": ""}]
        new, updated = handler.detect_changes(items)

        assert len(new) == 0
        assert len(updated) == 0

    def test_updated_outbreak_detected(self, handler):
        """Item with different pubDate is classified as updated."""
        handler.state_table.get_item.return_value = {
            "Item": {"outbreak_id": "test-123", "pub_date": "Sun, 30 Jun 2026", "affected_states": ["Ohio"]}
        }

        items = [{"outbreak_id": "test-123", "title": "Test", "pub_date": "Mon, 01 Jul 2026", "link": "", "category": ""}]
        new, updated = handler.detect_changes(items)

        assert len(new) == 0
        assert len(updated) == 1
        assert updated[0]["previous_states"] == ["Ohio"]


class TestBedrockExtraction:
    """Test Bedrock content extraction (mocked)."""

    def test_extract_with_bedrock_returns_structured_data(self, handler):
        """Mocked Bedrock response is parsed into dict."""
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(MOCK_BEDROCK_RESPONSE).encode()
        handler.bedrock = MagicMock()
        handler.bedrock.invoke_model.return_value = {"body": mock_body}

        result = handler.extract_with_bedrock("Some CDC page content", "Cyclosporiasis Outbreak")

        assert result is not None
        assert result["disease_name"] == "Cyclosporiasis"
        assert "Michigan" in result["affected_states"]
        assert result["case_count"] == 400

    def test_extract_with_bedrock_handles_code_block(self, handler):
        """Bedrock response wrapped in markdown code block is handled."""
        wrapped_response = {
            "content": [{"text": "```json\n" + json.dumps({
                "disease_name": "Salmonella",
                "affected_states": ["Texas"],
                "case_count": 50,
                "hospitalizations": 3,
                "deaths": None,
                "source_food": "Moringa Capsules",
                "onset_date": None,
                "status": "active",
                "summary": "Salmonella outbreak."
            }) + "\n```"}]
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(wrapped_response).encode()
        handler.bedrock = MagicMock()
        handler.bedrock.invoke_model.return_value = {"body": mock_body}

        result = handler.extract_with_bedrock("Some content", "Salmonella Outbreak")

        assert result is not None
        assert result["disease_name"] == "Salmonella"
        assert result["source_food"] == "Moringa Capsules"

    def test_extract_with_bedrock_invalid_json_returns_none(self, handler):
        """Non-JSON Bedrock response returns None."""
        bad_response = {"content": [{"text": "I cannot extract data from this."}]}
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(bad_response).encode()
        handler.bedrock = MagicMock()
        handler.bedrock.invoke_model.return_value = {"body": mock_body}

        result = handler.extract_with_bedrock("Garbled content", "Unknown")
        assert result is None


class TestHTMLStripping:
    """Test HTML to text conversion."""

    def test_strip_html_removes_tags(self, handler):
        text = handler.strip_html_to_text("<p>Hello <b>world</b></p>")
        assert "Hello" in text
        assert "world" in text
        assert "<" not in text

    def test_strip_html_removes_script_blocks(self, handler):
        html = "<script>var x=1;</script><p>Content</p>"
        text = handler.strip_html_to_text(html)
        assert "var x" not in text
        assert "Content" in text

    def test_strip_html_decodes_entities(self, handler):
        text = handler.strip_html_to_text("&amp; &lt; &gt;")
        assert "&" in text
        assert "<" in text

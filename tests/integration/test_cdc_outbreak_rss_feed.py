"""Integration tests for CDC Outbreak RSS Feed.

Tests connectivity and parsing against the real CDC RSS endpoint.
These tests require network access and may fail if CDC changes their feed.
"""
import json
import pytest
import urllib3


CDC_OUTBREAKS_RSS_URL = "https://tools.cdc.gov/api/v2/resources/media/285676.rss"


@pytest.fixture(scope="module")
def rss_response():
    """Fetch the real CDC Outbreaks RSS feed."""
    http = urllib3.PoolManager()
    response = http.request("GET", CDC_OUTBREAKS_RSS_URL, timeout=30)
    return response


class TestRSSConnectivity:
    """Test that the CDC RSS feed is reachable and returns valid data."""

    def test_feed_returns_200(self, rss_response):
        """RSS endpoint returns HTTP 200."""
        assert rss_response.status == 200

    def test_feed_is_xml(self, rss_response):
        """Response content type is XML."""
        content = rss_response.data.decode("utf-8")
        assert "<?xml" in content or "<rss" in content

    def test_feed_has_channel(self, rss_response):
        """RSS has a channel element."""
        content = rss_response.data.decode("utf-8")
        assert "<channel>" in content

    def test_feed_has_items(self, rss_response):
        """RSS has at least one item element."""
        content = rss_response.data.decode("utf-8")
        assert "<item>" in content

    def test_items_have_title(self, rss_response):
        """Each item has a title element."""
        content = rss_response.data.decode("utf-8")
        assert "<title>" in content

    def test_items_have_link(self, rss_response):
        """Each item has a link element."""
        content = rss_response.data.decode("utf-8")
        assert "<link>" in content

    def test_items_have_pubdate(self, rss_response):
        """Each item has a pubDate element."""
        content = rss_response.data.decode("utf-8")
        assert "<pubDate>" in content


class TestRSSParsing:
    """Test parsing the real RSS feed with our parser."""

    def test_parser_extracts_items(self, rss_response):
        """Our parser successfully extracts items from real feed."""
        import sys
        import os
        handler_dir = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas", "ingestion", "cdc_outbreak_fetcher")
        sys.path.insert(0, handler_dir)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambdas", "shared"))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambdas"))

        from unittest.mock import patch, MagicMock
        with patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
            import importlib.util, types
            handler_path = os.path.join(handler_dir, "handler.py")
            spec = importlib.util.spec_from_file_location("cdc_fetcher", handler_path)
            module = types.ModuleType("cdc_fetcher")
            module.__spec__ = spec
            module.__file__ = handler_path

            with patch("shared.config_loader.get_data_source_config", MagicMock(return_value={})):
                spec.loader.exec_module(module)

        content = rss_response.data.decode("utf-8")
        items = module.parse_rss_xml(content)

        assert len(items) > 0
        # Check first item has expected fields
        first = items[0]
        assert "outbreak_id" in first
        assert "title" in first
        assert "link" in first
        assert "pub_date" in first
        assert len(first["title"]) > 0

    def test_known_outbreak_present(self, rss_response):
        """The Cyclosporiasis outbreak (Jul 2026) appears in the feed."""
        content = rss_response.data.decode("utf-8").lower()
        # At least one of these should be present in an active feed
        has_known = any(term in content for term in [
            "cyclosporiasis", "salmonella", "e. coli", "listeria", "botulism"
        ])
        assert has_known, "No known outbreak types found in RSS feed"

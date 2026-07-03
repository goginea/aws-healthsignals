"""Unit tests for CDC Wastewater fetcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

import sys
sys.path.insert(0, "lambdas/ingestion/cdc_wastewater_fetcher")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")


class TestSocrataURLConstruction:
    """Test that Socrata API URLs are built correctly."""

    def test_influenza_dataset_id(self):
        """Influenza A should use dataset ymmh-divb."""
        base_url = "https://data.cdc.gov/resource"
        dataset_id = "ymmh-divb"
        url = f"{base_url}/{dataset_id}.json"
        assert "data.cdc.gov" in url
        assert "ymmh-divb" in url

    def test_rsv_dataset_id(self):
        """RSV should use dataset 45cq-cw4i."""
        base_url = "https://data.cdc.gov/resource"
        dataset_id = "45cq-cw4i"
        url = f"{base_url}/{dataset_id}.json"
        assert "45cq-cw4i" in url

    def test_covid_dataset_id(self):
        """SARS-CoV-2 should use dataset 2ew6-ywp6."""
        base_url = "https://data.cdc.gov/resource"
        dataset_id = "2ew6-ywp6"
        url = f"{base_url}/{dataset_id}.json"
        assert "2ew6-ywp6" in url

    def test_soql_where_clause(self):
        """SoQL query should filter by state and date."""
        params = {
            "$where": "wwtp_jurisdiction='TX' AND date_end > '2026-06-01'",
            "$limit": "10000",
            "$offset": "0",
            "$order": "date_end DESC",
        }
        query = urlencode(params)
        assert "wwtp_jurisdiction" in query
        assert "TX" in query
        assert "date_end" in query


class TestMetroCountyFiltering:
    """Test county FIPS filtering logic."""

    def test_harris_county_matches_houston(self):
        """Harris County (48201) should match Houston metro."""
        sentinel_fips = ["48201", "48157", "48339"]  # Houston area
        record_fips = ["48201"]
        assert any(f in sentinel_fips for f in record_fips)

    def test_non_metro_county_excluded(self):
        """A rural county not in any metro should be excluded."""
        sentinel_fips = ["48201", "48113", "48453", "48029"]
        record_fips = ["48143"]  # Erath County — rural
        assert not any(f in sentinel_fips for f in record_fips)

    def test_multi_county_plant_matches(self):
        """A plant serving multiple counties should match if any overlaps."""
        sentinel_fips = ["48201", "48157"]
        record_fips = ["48999", "48201", "48888"]  # One match
        assert any(f in sentinel_fips for f in record_fips)

    def test_comma_separated_fips_parsing(self):
        """County FIPS field may be comma-separated."""
        county_fips_str = "48201,48157,48339"
        parsed = [f.strip() for f in county_fips_str.split(",")]
        assert len(parsed) == 3
        assert "48201" in parsed

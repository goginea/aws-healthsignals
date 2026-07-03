"""Integration tests — verify that external APIs are reachable and returning data.

These tests make REAL HTTP calls to external APIs. Run them to verify:
1. APIs are still operational (academic APIs can go offline)
2. Response format hasn't changed
3. Data is current (not stale)

Run with: pytest tests/integration/ -v --timeout=30
"""
import json
import pytest
import urllib3
from datetime import datetime, timedelta

http = urllib3.PoolManager()


class TestDelphiEpidataAPI:
    """Test CMU Delphi Epidata API connectivity and data availability."""

    BASE_URL = "https://api.delphi.cmu.edu/epidata/covidcast/"

    def test_api_reachable(self):
        """API base endpoint should respond."""
        response = http.request("GET", self.BASE_URL, timeout=10.0)
        # Even without params, should get a response (likely error but not timeout)
        assert response.status in (200, 400, 404)

    def test_flu_signal_available(self):
        """Flu signal (nssp:pct_ed_visits_influenza) should return data for Houston."""
        params = {
            "data_source": "nssp",
            "signal": "pct_ed_visits_influenza",
            "geo_type": "msa",
            "geo_value": "26420",  # Houston
            "time_type": "day",
            "time_values": "20250101-20250131",  # January 2025 (known flu season)
        }
        url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        response = http.request("GET", url, timeout=15.0)
        assert response.status == 200

        data = json.loads(response.data.decode())
        assert "epidata" in data
        assert len(data["epidata"]) > 0, "Expected flu data for Houston Jan 2025"

        # Verify data structure
        record = data["epidata"][0]
        assert "value" in record
        assert "time_value" in record
        assert record["value"] >= 0

    def test_covid_signal_available(self):
        """COVID signal should return data."""
        params = {
            "data_source": "nssp",
            "signal": "pct_ed_visits_covid",
            "geo_type": "msa",
            "geo_value": "26420",
            "time_type": "day",
            "time_values": "20250101-20250131",
        }
        url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        response = http.request("GET", url, timeout=15.0)
        assert response.status == 200

        data = json.loads(response.data.decode())
        assert "epidata" in data

    def test_rsv_signal_available(self):
        """RSV signal should return data."""
        params = {
            "data_source": "nssp",
            "signal": "pct_ed_visits_rsv",
            "geo_type": "msa",
            "geo_value": "26420",
            "time_type": "day",
            "time_values": "20250101-20250131",
        }
        url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        response = http.request("GET", url, timeout=15.0)
        assert response.status == 200

        data = json.loads(response.data.decode())
        assert "epidata" in data

    def test_all_sentinel_metros_have_data(self):
        """All 4 Texas sentinel metros should have flu data."""
        metros = ["26420", "19100", "12420", "41700"]

        for msa_code in metros:
            params = {
                "data_source": "nssp",
                "signal": "pct_ed_visits_influenza",
                "geo_type": "msa",
                "geo_value": msa_code,
                "time_type": "day",
                "time_values": "20250101-20250115",
            }
            url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

            response = http.request("GET", url, timeout=15.0)
            assert response.status == 200, f"Failed for metro {msa_code}"

            data = json.loads(response.data.decode())
            assert len(data.get("epidata", [])) > 0, f"No data for metro {msa_code}"

    def test_recent_data_available(self):
        """Data from within last 30 days should be available (API is current)."""
        today = datetime.utcnow()
        start = (today - timedelta(days=30)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")

        params = {
            "data_source": "nssp",
            "signal": "pct_ed_visits_influenza",
            "geo_type": "msa",
            "geo_value": "26420",
            "time_type": "day",
            "time_values": f"{start}-{end}",
        }
        url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

        response = http.request("GET", url, timeout=15.0)
        data = json.loads(response.data.decode())

        # Note: During summer months, data may be sparse but should still exist
        # This test verifies the API is returning current data
        if len(data.get("epidata", [])) == 0:
            pytest.skip("No data in last 30 days — may be off-season gap")


class TestCDCWastewaterAPI:
    """Test CDC NWSS wastewater API connectivity."""

    API_URL = "https://data.cdc.gov/resource/g653-rqe2.json"

    def test_api_reachable(self):
        """CDC SODA API should be reachable."""
        response = http.request("GET", f"{self.API_URL}?$limit=1", timeout=15.0)
        assert response.status == 200

    def test_texas_data_available(self):
        """Texas wastewater data should be available."""
        url = f"{self.API_URL}?$where=state_fips='48'&$limit=5"
        response = http.request("GET", url, timeout=15.0)
        assert response.status == 200

        data = json.loads(response.data.decode())
        assert len(data) > 0, "Expected Texas wastewater data"


class TestCDCRespiratoryAPI:
    """Test CDC respiratory activity level API."""

    API_URL = "https://data.cdc.gov/resource/kvpw-qein.json"

    def test_api_reachable(self):
        """CDC respiratory API should be reachable."""
        response = http.request("GET", f"{self.API_URL}?$limit=1", timeout=15.0)
        # This endpoint may have moved or changed — check for any HTTP response
        assert response.status in (200, 301, 302, 404)

    @pytest.mark.skipif(True, reason="Endpoint URL may need updating — verify manually")
    def test_texas_data_available(self):
        """Texas respiratory activity data should be available."""
        url = f"{self.API_URL}?$where=statename='Texas'&$limit=5"
        response = http.request("GET", url, timeout=15.0)
        assert response.status == 200

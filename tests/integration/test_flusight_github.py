"""Integration test for FluSight GitHub repo connectivity."""
import pytest
import urllib3


FLUSIGHT_BASE = "https://raw.githubusercontent.com/cdcepi/FluSight-forecast-hub/main/model-output/FluSight-ensemble"


@pytest.fixture(scope="module")
def http():
    return urllib3.PoolManager()


class TestFluSightGitHub:
    def test_repo_accessible(self, http):
        """Can reach FluSight GitHub raw content."""
        # Just check the directory listing via API
        response = http.request(
            "GET",
            "https://api.github.com/repos/cdcepi/FluSight-forecast-hub/contents/model-output/FluSight-ensemble",
            timeout=15,
            headers={"User-Agent": "HealthSignals-Test"},
        )
        assert response.status == 200

    def test_csv_files_exist(self, http):
        """FluSight ensemble CSV files are present in the repo."""
        import json
        response = http.request(
            "GET",
            "https://api.github.com/repos/cdcepi/FluSight-forecast-hub/contents/model-output/FluSight-ensemble",
            timeout=15,
            headers={"User-Agent": "HealthSignals-Test"},
        )
        files = json.loads(response.data.decode())
        csv_files = [f for f in files if f["name"].endswith(".csv")]
        assert len(csv_files) > 0

    def test_latest_csv_downloadable(self, http):
        """Can download the most recent FluSight ensemble CSV."""
        import json
        response = http.request(
            "GET",
            "https://api.github.com/repos/cdcepi/FluSight-forecast-hub/contents/model-output/FluSight-ensemble",
            timeout=15,
            headers={"User-Agent": "HealthSignals-Test"},
        )
        files = json.loads(response.data.decode())
        csv_files = sorted(
            [f for f in files if f["name"].endswith(".csv")],
            key=lambda x: x["name"],
            reverse=True,
        )
        latest = csv_files[0]

        # Download the CSV
        dl_response = http.request("GET", latest["download_url"], timeout=30)
        assert dl_response.status == 200
        content = dl_response.data.decode()
        assert "reference_date" in content
        assert "quantile" in content

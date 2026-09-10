"""Unit tests for the weekly Drug Shortage Digest Lambda.

Covers ranking (top 10 NEW/WORSENING, top 5 recently RESOLVED), the sort
order (WORSENING above NEW, then supply-status severity), the empty-week
no-op, and that a shortage_digest SFN execution is started with the compiled
lists.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


@pytest.fixture(scope="module")
def handler():
    mock_dynamodb = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    return load_handler(
        "orchestration/shortage_digest",
        extra_patches={
            "boto3.client": MagicMock(),
            "boto3.resource": mock_dynamodb,
        },
    )


def _rec(pid, status, supply="CURRENTLY_IN_SHORTAGE", cat="antivirals", created="2026-09-01"):
    return {
        "product_id": pid,
        "product_name": f"Drug {pid}",
        "therapeutic_category": cat,
        "supply_status": supply,
        "shortage_status": status,
        "reason_for_shortage": "Demand increase",
        "week_timestamp": "2026-W37",
        "created_at": created,
    }


class TestRanking:
    def test_active_sort_worsening_above_new(self, handler):
        new = _rec("N", "NEW", supply="CURRENTLY_IN_SHORTAGE")
        worse = _rec("W", "WORSENING", supply="LIMITED_AVAILABILITY")
        # WORSENING outranks NEW even with lower supply severity
        assert handler._active_sort_key(worse) > handler._active_sort_key(new)

    def test_active_sort_supply_severity_within_status(self, handler):
        disc = _rec("D", "NEW", supply="DISCONTINUED")
        limited = _rec("L", "NEW", supply="LIMITED_AVAILABILITY")
        assert handler._active_sort_key(disc) > handler._active_sort_key(limited)

    def test_top_active_limited_to_10(self, handler):
        records = [_rec(f"P{i}", "NEW") for i in range(15)]
        records += [_rec(f"R{i}", "RESOLVED", created=f"2026-09-{i+1:02d}") for i in range(8)]
        with patch.object(handler, "_load_week_records", return_value=records), \
             patch.object(handler, "_start_digest", return_value="arn:exec:digest") as mstart:
            result = handler.lambda_handler({"week_timestamp": "2026-W37"}, None)
            payload = mstart.call_args[0][0]
            assert len(payload["top_active"]) == 10
            assert len(payload["recently_resolved"]) == 5
            assert payload["alert_type"] == "shortage_digest"
            assert result["dispatched"] is True

    def test_recently_resolved_sorted_by_recency(self, handler):
        records = [
            _rec("old", "RESOLVED", created="2026-08-01"),
            _rec("new", "RESOLVED", created="2026-09-05"),
            _rec("mid", "RESOLVED", created="2026-08-20"),
        ]
        with patch.object(handler, "_load_week_records", return_value=records), \
             patch.object(handler, "_start_digest", return_value="arn:x") as mstart:
            handler.lambda_handler({"week_timestamp": "2026-W37"}, None)
            resolved = mstart.call_args[0][0]["recently_resolved"]
            assert resolved[0]["product_name"] == "Drug new"

    def test_empty_week_is_noop(self, handler):
        with patch.object(handler, "_load_week_records", return_value=[]), \
             patch.object(handler, "_start_digest") as mstart:
            result = handler.lambda_handler({"week_timestamp": "2026-W37"}, None)
            assert result["dispatched"] is False
            assert result["reason"] == "no_shortage_activity"
            mstart.assert_not_called()

    def test_only_resolved_still_dispatches(self, handler):
        records = [_rec("R1", "RESOLVED", created="2026-09-05")]
        with patch.object(handler, "_load_week_records", return_value=records), \
             patch.object(handler, "_start_digest", return_value="arn:x") as mstart:
            result = handler.lambda_handler({"week_timestamp": "2026-W37"}, None)
            assert result["dispatched"] is True
            payload = mstart.call_args[0][0]
            assert len(payload["top_active"]) == 0
            assert len(payload["recently_resolved"]) == 1

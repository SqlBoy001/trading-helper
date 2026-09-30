"""Tests for limit_pool_tool: mode dispatch, validation, no-fallback error path.

All HTTP is mocked at :mod:`backtest.loaders.hithink_client`, so no test ever
reaches a live endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backtest.loaders import hithink_client
from src.tools.limit_pool_tool import LimitPoolTool


class TestAvailability:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        assert LimitPoolTool.check_available() is False

    def test_available_with_key(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        assert LimitPoolTool.check_available() is True


class TestValidation:
    def test_missing_pool(self):
        out = json.loads(LimitPoolTool().execute())
        assert out["ok"] is False

    def test_invalid_pool(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(LimitPoolTool().execute(pool="bogus"))
        assert out["ok"] is False

    def test_invalid_sort_field_for_pool(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(LimitPoolTool().execute(pool="limit_up", sort_field="open_times"))
        assert out["ok"] is False
        assert "sort_field" in out["error"]

    def test_invalid_sort_dir(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(LimitPoolTool().execute(pool="limit_up", sort_dir="bogus"))
        assert out["ok"] is False

    def test_invalid_date(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(LimitPoolTool().execute(pool="limit_up", date="not-a-date"))
        assert out["ok"] is False


class TestNoKey:
    def test_no_key_reports_clean_error_for_pool(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = json.loads(LimitPoolTool().execute(pool="limit_up"))
        assert out["ok"] is False
        assert "HITHINK_FINANCE_API_KEY" in out["error"]

    def test_no_key_reports_clean_error_for_ladder(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = json.loads(LimitPoolTool().execute(pool="ladder"))
        assert out["ok"] is False
        assert "HITHINK_FINANCE_API_KEY" in out["error"]


class TestPoolSuccess:
    def test_limit_up_pool_returns_items_and_pagination(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        payload = {
            "pagination": {"total": 31, "pages": 1, "size": 50, "page": 1},
            "item": [
                {"thscode": "603163.SH", "ticker": "603163", "name": "圣晖集成", "continue_day_cnt": 1},
            ],
        }
        with patch.object(hithink_client, "get", return_value=payload) as mock_get:
            out = json.loads(LimitPoolTool().execute(pool="limit_up", sort_field="continue_day_cnt"))

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["sort_field"] == "continue_day_cnt"
        assert kwargs["params"]["sort_dir"] == "desc"
        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["pool"] == "limit_up"
        assert out["data"]["pagination"]["total"] == 31
        assert out["data"]["items"][0]["name"] == "圣晖集成"

    def test_limit_up_pool_with_date_converts_to_shanghai_midnight_ms(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        with patch.object(hithink_client, "get", return_value={"item": [], "pagination": {}}) as mock_get:
            LimitPoolTool().execute(pool="limit_down", date="2026-08-28")

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["date_ms"] == hithink_client.shanghai_midnight_ms("2026-08-28")

    def test_hithink_failure_returns_error_no_fallback(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        with patch.object(hithink_client, "get", side_effect=RuntimeError("rate limited")):
            out = json.loads(LimitPoolTool().execute(pool="limit_break"))
        assert out["ok"] is False
        assert "rate limited" in out["error"]


class TestLadderSuccess:
    def test_ladder_returns_window_and_days(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        payload = {
            "window": {"length": 30, "date_list": ["2026-09-08"], "board_caps": {"two_board": 4}},
            "item": [{"date": "2026-09-08", "boards": {"two_board": []}}],
        }
        with patch.object(hithink_client, "get", return_value=payload) as mock_get:
            out = json.loads(LimitPoolTool().execute(pool="ladder"))

        mock_get.assert_called_once_with("/api/a-share/special-data/limit-up-ladder")
        assert out["ok"] is True
        assert out["pool"] == "ladder"
        assert out["data"]["window"]["length"] == 30
        assert len(out["data"]["days"]) == 1

    def test_ladder_default_trims_to_5_most_recent_days(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        days = [{"date": f"2026-09-{d:02d}", "boards": {}} for d in range(8, 0, -1)]  # 8 days, newest first
        payload = {"window": {"length": 30}, "item": days}
        with patch.object(hithink_client, "get", return_value=payload):
            out = json.loads(LimitPoolTool().execute(pool="ladder"))

        assert out["ok"] is True
        assert len(out["data"]["days"]) == 5
        assert out["data"]["days"][0]["date"] == "2026-09-08"

    def test_ladder_days_param_overrides_default(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        days = [{"date": f"2026-09-{d:02d}", "boards": {}} for d in range(8, 0, -1)]
        payload = {"window": {"length": 30}, "item": days}
        with patch.object(hithink_client, "get", return_value=payload):
            out = json.loads(LimitPoolTool().execute(pool="ladder", days=2))

        assert out["ok"] is True
        assert len(out["data"]["days"]) == 2

    def test_ladder_days_capped_at_30(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        days = [{"date": f"day{d}", "boards": {}} for d in range(30)]
        payload = {"window": {"length": 30}, "item": days}
        with patch.object(hithink_client, "get", return_value=payload):
            out = json.loads(LimitPoolTool().execute(pool="ladder", days=999))

        assert out["ok"] is True
        assert len(out["data"]["days"]) == 30

    def test_ladder_invalid_days_returns_error(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(LimitPoolTool().execute(pool="ladder", days=0))
        assert out["ok"] is False


class TestCharacterBudgetPaging:
    """A response that overflows the per-tool character budget pages instead
    of silently truncating — the real-world bug this suite guards against:
    the ladder's full 30-day window (~55K chars observed live) and a wide
    limit_up `size` both exceeded the fixed budget and were cut mid-JSON
    before this fix, which the model could not distinguish from a complete
    answer.
    """

    def test_large_pool_response_is_paged_not_truncated(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        # Build enough rows that the serialized envelope exceeds the tool
        # result budget (10_000 chars) at the default fields-per-row size.
        big_reason = "题材" * 200  # ~400 chars of filler per row
        items = [
            {
                "thscode": f"60{i:04d}.SH", "ticker": f"60{i:04d}", "name": f"标的{i}",
                "is_st": False, "is_new": False, "last_price": 10.0 + i,
                "price_change_ratio_pct": 10.0, "limit_up_time": "09:30",
                "limit_up_reason": big_reason, "continue_day_text": "首板",
                "continue_day_cnt": 1, "seal_money": 1_000_000.0, "max_seal_money": 2_000_000.0,
            }
            for i in range(60)
        ]
        payload = {"pagination": {"total": 60}, "item": items}
        with patch.object(hithink_client, "get", return_value=payload):
            out = json.loads(LimitPoolTool().execute(pool="limit_up", size=60))

        assert out["ok"] is True
        assert "paging" in out
        assert out["paging"]["total"] == 60
        # Every item actually returned must be a whole, uncut record.
        for row in out["data"]["items"]:
            assert row["thscode"].endswith(".SH")
            assert row["limit_up_reason"] == big_reason
        if not out["paging"]["complete"]:
            assert out["paging"]["next_offset"] is not None
            assert out["paging"]["returned"] < 60

    def test_offset_continues_from_next_offset(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        items = [{"thscode": f"60{i:04d}.SH", "ticker": str(i), "name": f"标的{i}"} for i in range(5)]
        payload = {"pagination": {"total": 5}, "item": items}
        with patch.object(hithink_client, "get", return_value=payload):
            out = json.loads(LimitPoolTool().execute(pool="limit_up", offset=2))

        assert out["ok"] is True
        assert out["paging"]["offset"] == 2
        assert out["data"]["items"][0]["name"] == "标的2"

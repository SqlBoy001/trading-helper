"""Tests for hot_stock_tool: view dispatch, validation, no-fallback error path.

All HTTP is mocked at :mod:`backtest.loaders.hithink_client`, so no test ever
reaches a live endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backtest.loaders import hithink_client
from src.tools.hot_stock_tool import HotStockTool


class TestAvailability:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        assert HotStockTool.check_available() is False

    def test_available_with_key(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        assert HotStockTool.check_available() is True


class TestValidation:
    def test_missing_view(self):
        out = json.loads(HotStockTool().execute())
        assert out["ok"] is False

    def test_invalid_view(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(HotStockTool().execute(view="bogus"))
        assert out["ok"] is False

    def test_invalid_period(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(HotStockTool().execute(view="skyrocket", period="week"))
        assert out["ok"] is False

    def test_history_requires_date(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(HotStockTool().execute(view="history"))
        assert out["ok"] is False
        assert "date" in out["error"]

    def test_rank_trend_requires_all_params(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(HotStockTool().execute(view="rank_trend", thscode="300034.SZ"))
        assert out["ok"] is False
        assert "start_date" in out["error"]


class TestNoKey:
    def test_no_key_reports_clean_error(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = json.loads(HotStockTool().execute(view="hot"))
        assert out["ok"] is False
        assert "HITHINK_FINANCE_API_KEY" in out["error"]


class TestBoardSuccess:
    def test_skyrocket_default_period(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        payload = {"item": [{"thscode": "603822.SH", "name": "嘉澳环保", "rank": 1, "heat": "1941909"}]}
        with patch.object(hithink_client, "get", return_value=payload) as mock_get:
            out = json.loads(HotStockTool().execute(view="skyrocket"))

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["period"] == "day"
        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["view"] == "skyrocket"
        assert out["data"]["items"][0]["name"] == "嘉澳环保"

    def test_hot_hour_period(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        with patch.object(hithink_client, "get", return_value={"item": []}) as mock_get:
            HotStockTool().execute(view="hot", period="hour")

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["period"] == "hour"

    def test_hithink_failure_returns_error_no_fallback(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        with patch.object(hithink_client, "get", side_effect=RuntimeError("down")):
            out = json.loads(HotStockTool().execute(view="hot"))
        assert out["ok"] is False
        assert "down" in out["error"]


class TestHistorySuccess:
    def test_history_returns_items(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        payload = {
            "date": "2026-06-21",
            "date_ms": 1781971200000,
            "item": [{"thscode": "000725.SZ", "name": "京东方A", "rank": 1}],
        }
        with patch.object(hithink_client, "get", return_value=payload) as mock_get:
            out = json.loads(HotStockTool().execute(view="history", date="2026-06-21"))

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["date"] == "2026-06-21"
        assert out["ok"] is True
        assert out["data"]["date"] == "2026-06-21"
        assert out["data"]["items"][0]["name"] == "京东方A"


class TestRankTrendSuccess:
    def test_rank_trend_returns_points(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        payload = {
            "item": [
                {"thscode": "300034.SZ", "date": "2026-06-21", "rank": 1740},
            ]
        }
        with patch.object(hithink_client, "get", return_value=payload) as mock_get:
            out = json.loads(
                HotStockTool().execute(
                    view="rank_trend", thscode="300034.SZ",
                    start_date="2026-06-21", end_date="2026-07-01",
                )
            )

        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {
            "thscode": "300034.SZ", "start_date": "2026-06-21", "end_date": "2026-07-01",
        }
        assert out["ok"] is True
        assert out["data"]["points"][0]["rank"] == 1740

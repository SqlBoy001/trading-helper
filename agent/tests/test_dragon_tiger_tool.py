"""Tests for the dragon-tiger (龙虎榜) tool.

No request leaves the process: the HTTP boundary
(:func:`backtest.loaders.eastmoney_client.throttled_get_json`) is mocked so the
real client + tool parsing runs offline.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from backtest.loaders import eastmoney_client, hithink_client
from src.tools.dragon_tiger_tool import DragonTigerTool, _bare_code, _compact_date


def _appearance_payload() -> dict[str, Any]:
    """A datacenter appearance payload with two board rows."""
    return {
        "result": {
            "data": [
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "CLOSE_PRICE": 1700.0,
                    "CHANGE_RATE": 5.2,
                    "BILLBOARD_NET_AMT": 1.2e8,
                    "BILLBOARD_BUY_AMT": 3.0e8,
                    "BILLBOARD_SELL_AMT": 1.8e8,
                    "ACCUM_AMOUNT": 9.0e8,
                    "EXPLANATION": "日涨幅偏离值达7%",
                },
                {
                    "SECURITY_CODE": "000001",
                    "SECURITY_NAME_ABBR": "平安银行",
                    "CLOSE_PRICE": 12.0,
                    "CHANGE_RATE": -4.1,
                    "BILLBOARD_NET_AMT": -5.0e7,
                    "BILLBOARD_BUY_AMT": 1.0e8,
                    "BILLBOARD_SELL_AMT": 1.5e8,
                    "ACCUM_AMOUNT": 4.0e8,
                    "EXPLANATION": "日跌幅偏离值达7%",
                },
            ]
        }
    }


def _seat_payload() -> dict[str, Any]:
    """A datacenter seat payload with one buy seat."""
    return {
        "result": {
            "data": [
                {
                    "OPERATEDEPT_NAME": "机构专用",
                    "SIDE": "BUY",
                    "BUY": 2.0e8,
                    "SELL": 0.0,
                    "NET": 2.0e8,
                    "RANK": 1,
                }
            ]
        }
    }


class TestHelpers:
    def test_compact_date_normalizes(self) -> None:
        assert _compact_date("20240102") == "2024-01-02"
        assert _compact_date(" 2024-01-02 ") == "2024-01-02"

    def test_compact_date_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            _compact_date("not-a-date")

    def test_bare_code_strips_suffix(self) -> None:
        assert _bare_code("600519.SH") == "600519"
        assert _bare_code("000001") == "000001"


class TestToolContract:
    def test_name_and_schema(self) -> None:
        tool = DragonTigerTool()
        assert tool.name == "get_dragon_tiger"
        assert tool.is_readonly is True
        assert tool.repeatable is True
        assert tool.parameters["required"] == ["date"]
        assert "code" in tool.parameters["properties"]


class TestExecuteSuccess:
    def test_full_market_list_no_code(self) -> None:
        tool = DragonTigerTool()
        with patch.object(
            eastmoney_client, "throttled_get_json", return_value=_appearance_payload()
        ) as http:
            out = json.loads(tool.execute(date="2024-01-02"))

        # Only the appearance report is queried when no code is supplied.
        http.assert_called_once()
        _, kwargs = http.call_args
        assert kwargs["host_key"] == "eastmoney"
        assert kwargs["params"]["reportName"] == "RPT_DAILYBILLBOARD_DETAILS"

        assert out["ok"] is True
        assert out["market"] == "a_share"
        assert out["source"] == "eastmoney"
        assert out["data"]["date"] == "2024-01-02"
        assert "seats" not in out["data"]
        assert len(out["data"]["appearances"]) == 2
        assert out["data"]["appearances"][0]["code"] == "600519"
        assert out["data"]["appearances"][0]["net_buy"] == pytest.approx(1.2e8)

    def test_with_code_adds_seats(self) -> None:
        tool = DragonTigerTool()
        payloads = [_appearance_payload(), _seat_payload()]
        with patch.object(
            eastmoney_client, "throttled_get_json", side_effect=payloads
        ) as http:
            out = json.loads(tool.execute(date="2024-01-02", code="600519.SH"))

        assert http.call_count == 2
        assert out["ok"] is True
        assert out["data"]["code"] == "600519"
        assert len(out["data"]["seats"]) == 1
        assert out["data"]["seats"][0]["seat"] == "机构专用"
        assert out["data"]["seats"][0]["net"] == pytest.approx(2.0e8)


class TestExecuteError:
    def test_missing_date_returns_error_envelope(self) -> None:
        out = json.loads(DragonTigerTool().execute())
        assert out["ok"] is False
        assert "date" in out["error"]

    def test_bad_date_returns_error_envelope(self) -> None:
        out = json.loads(DragonTigerTool().execute(date="nope"))
        assert out["ok"] is False
        assert "invalid date" in out["error"]

    def test_http_failure_returns_error_envelope(self) -> None:
        tool = DragonTigerTool()
        with patch.object(
            eastmoney_client,
            "throttled_get_json",
            side_effect=RuntimeError("eastmoney banned"),
        ), patch(
            "src.tools.dragon_tiger_tool.tushare_fallbacks.fetch_dragon_tiger",
            side_effect=RuntimeError("no fallback"),
        ):
            out = json.loads(tool.execute(date="2024-01-02"))

        assert out["ok"] is False
        assert "eastmoney banned" in out["error"]

    def test_http_failure_uses_tushare_fallback_when_available(self) -> None:
        fallback = {
            "date": "2024-01-02",
            "count": 1,
            "appearances": [{"code": "600519", "net_buy": 1.0}],
        }
        tool = DragonTigerTool()
        with patch.object(
            eastmoney_client,
            "throttled_get_json",
            side_effect=RuntimeError("eastmoney banned"),
        ), patch(
            "src.tools.dragon_tiger_tool.tushare_fallbacks.fetch_dragon_tiger",
            return_value=fallback,
        ) as fallback_fetch:
            out = json.loads(tool.execute(date="2024-01-02", code="600519.SH"))

        fallback_fetch.assert_called_once_with("2024-01-02", "600519")
        assert out["ok"] is True
        assert out["source"] == "tushare"
        assert out["data"]["appearances"][0]["code"] == "600519"
        assert "used tushare fallback" in out["warnings"][0]


def _hithink_dragon_tiger_data() -> dict:
    """A Hithink dragon-tiger-list ``data`` payload with two stock rows."""
    return {
        "timestamp": 1,
        "board_type": "all",
        "trade_date": "2024-01-02",
        "count": 2,
        "stock_count": 2,
        "stock_items": [
            {
                "thscode": "600519.SH",
                "ticker": "600519",
                "name": "贵州茅台",
                "change": 0.052,
                "net_value": 1.2e8,
                "net_rate": 0.1,
                "hot_rank": 1,
                "buy_value": 3.0e8,
                "sell_value": 1.8e8,
                "limit_reason": None,
            },
            {
                "thscode": "000001.SZ",
                "ticker": "000001",
                "name": "平安银行",
                "change": -0.041,
                "net_value": -5.0e7,
                "net_rate": -0.05,
                "hot_rank": 2,
                "buy_value": 1.0e8,
                "sell_value": 1.5e8,
                "limit_reason": None,
            },
        ],
        "hot_money_items": [],
    }


class TestHithinkPriority:
    """Hithink is tried first when configured; Eastmoney is the fallback."""

    def test_hithink_serves_full_market_list(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        tool = DragonTigerTool()
        with patch.object(
            hithink_client, "get", return_value=_hithink_dragon_tiger_data()
        ) as hithink_get, patch.object(
            eastmoney_client, "throttled_get_json"
        ) as em_get:
            out = json.loads(tool.execute(date="2024-01-02"))

        hithink_get.assert_called_once()
        em_get.assert_not_called()
        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["data"]["date"] == "2024-01-02"
        assert len(out["data"]["appearances"]) == 2
        assert out["data"]["appearances"][0]["code"] == "600519"
        assert out["data"]["appearances"][0]["change_pct"] == pytest.approx(5.2)
        assert "seats" not in out["data"]

    def test_hithink_with_code_layers_eastmoney_seats(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        tool = DragonTigerTool()
        with patch.object(
            hithink_client, "get", return_value=_hithink_dragon_tiger_data()
        ), patch.object(
            eastmoney_client, "throttled_get_json", return_value=_seat_payload()
        ) as em_get:
            out = json.loads(tool.execute(date="2024-01-02", code="600519.SH"))

        em_get.assert_called_once()
        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["data"]["code"] == "600519"
        assert len(out["data"]["appearances"]) == 1
        assert len(out["data"]["seats"]) == 1
        assert out["data"]["seats"][0]["seat"] == "机构专用"

    def test_hithink_failure_falls_back_to_eastmoney(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        tool = DragonTigerTool()
        with patch.object(
            hithink_client, "get", side_effect=RuntimeError("hithink down")
        ), patch.object(
            eastmoney_client, "throttled_get_json", return_value=_appearance_payload()
        ) as em_get:
            out = json.loads(tool.execute(date="2024-01-02"))

        em_get.assert_called_once()
        assert out["ok"] is True
        assert out["source"] == "eastmoney"

    def test_no_hithink_key_skips_straight_to_eastmoney(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        tool = DragonTigerTool()
        with patch.object(hithink_client, "get") as hithink_get, patch.object(
            eastmoney_client, "throttled_get_json", return_value=_appearance_payload()
        ):
            out = json.loads(tool.execute(date="2024-01-02"))

        hithink_get.assert_not_called()
        assert out["ok"] is True
        assert out["source"] == "eastmoney"


class TestAppearancesPaging:
    """A full-market list that overflows the result budget pages instead of
    silently truncating mid-JSON — the real-world bug this suite guards
    against: a busy trading day's board (56+ rows observed live) exceeded
    the fixed per-tool character budget and the truncated tail was
    indistinguishable from a complete answer.
    """

    def test_small_payload_reports_complete(self) -> None:
        tool = DragonTigerTool()
        with patch.object(
            eastmoney_client, "throttled_get_json", return_value=_appearance_payload()
        ):
            out = json.loads(tool.execute(date="2024-01-02"))

        assert out["ok"] is True
        assert out["paging"]["complete"] is True
        assert out["paging"]["total"] == 2

    def test_large_payload_pages_with_whole_records(self) -> None:
        big_reason = "偏离值达7%" * 300  # ~1800 chars of filler per row
        rows = [
            {
                "SECURITY_CODE": f"60{i:04d}",
                "SECURITY_NAME_ABBR": f"标的{i}",
                "CLOSE_PRICE": 10.0 + i,
                "CHANGE_RATE": 5.0,
                "BILLBOARD_NET_AMT": 1.0e8,
                "BILLBOARD_BUY_AMT": 2.0e8,
                "BILLBOARD_SELL_AMT": 1.0e8,
                "ACCUM_AMOUNT": 3.0e8,
                "EXPLANATION": big_reason,
            }
            for i in range(20)
        ]
        payload = {"result": {"data": rows}}
        tool = DragonTigerTool()
        with patch.object(eastmoney_client, "throttled_get_json", return_value=payload):
            out = json.loads(tool.execute(date="2024-01-02"))

        assert out["ok"] is True
        assert out["paging"]["total"] == 20
        for row in out["data"]["appearances"]:
            assert row["reason"] == big_reason  # every returned row is whole, never cut
        if not out["paging"]["complete"]:
            assert out["paging"]["next_offset"] is not None

    def test_offset_continues_from_next_offset(self) -> None:
        rows = [
            {"SECURITY_CODE": f"60000{i}", "SECURITY_NAME_ABBR": f"标的{i}"}
            for i in range(5)
        ]
        payload = {"result": {"data": rows}}
        tool = DragonTigerTool()
        with patch.object(eastmoney_client, "throttled_get_json", return_value=payload):
            out = json.loads(tool.execute(date="2024-01-02", offset=2))

        assert out["ok"] is True
        assert out["paging"]["offset"] == 2
        assert out["data"]["appearances"][0]["code"] == "600002"

    def test_invalid_offset_returns_error(self) -> None:
        out = json.loads(DragonTigerTool().execute(date="2024-01-02", offset="bogus"))
        assert out["ok"] is False

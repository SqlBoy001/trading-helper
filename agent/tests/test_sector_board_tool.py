"""Tests for sector_board_tool: mode dispatch, Hithink-first + Eastmoney fallback.

All HTTP is mocked at the module boundary (:mod:`backtest.loaders.hithink_client`
and the Eastmoney-backed :class:`~src.tools.sector_tool.SectorInfoTool`), so no
test ever reaches a live endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backtest.loaders import hithink_client
from src.tools.sector_board_tool import SectorBoardTool


class TestValidation:
    def test_missing_mode(self):
        out = json.loads(SectorBoardTool().execute())
        assert out["ok"] is False

    def test_invalid_mode(self):
        out = json.loads(SectorBoardTool().execute(mode="bogus"))
        assert out["ok"] is False

    def test_search_requires_query(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(SectorBoardTool().execute(mode="search"))
        assert out["ok"] is False
        assert "query" in out["error"]

    def test_constituents_requires_thscode(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(SectorBoardTool().execute(mode="constituents"))
        assert out["ok"] is False
        assert "thscode" in out["error"]

    def test_membership_requires_code(self):
        out = json.loads(SectorBoardTool().execute(mode="membership"))
        assert out["ok"] is False
        assert "code" in out["error"]

    def test_invalid_tag(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        out = json.loads(SectorBoardTool().execute(mode="catalog", tag="bogus"))
        assert out["ok"] is False


class TestSearchNoKey:
    def test_search_without_key_reports_clean_error(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = json.loads(SectorBoardTool().execute(mode="search", query="白酒"))
        assert out["ok"] is False
        assert "HITHINK_FINANCE_API_KEY" in out["error"]


class TestSearchSuccess:
    def test_search_returns_candidates(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        candidates = [
            {"thscode": "881273.TI", "ticker": "881273", "name": "白酒", "asset_type": "a-share-index"},
            {"thscode": "012414.OF", "ticker": "012414", "name": "白酒C", "asset_type": "fund-otc"},
        ]
        with patch.object(hithink_client, "get_items", return_value=candidates) as mock_get:
            out = json.loads(SectorBoardTool().execute(mode="search", query="白酒"))

        mock_get.assert_called_once()
        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["data"]["count"] == 2
        assert out["data"]["candidates"][0]["thscode"] == "881273.TI"


class TestCatalogSuccess:
    def test_catalog_default_tag(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        boards = [{"thscode": "886042.TI", "name": "白酒概念"}]
        with patch.object(hithink_client, "get_items", return_value=boards) as mock_get:
            out = json.loads(SectorBoardTool().execute(mode="catalog"))

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["tag"] == "cn_concept"
        assert out["ok"] is True
        assert out["data"]["boards"] == boards

    def test_catalog_without_key_reports_clean_error(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = json.loads(SectorBoardTool().execute(mode="catalog", tag="industry"))
        assert out["ok"] is False
        assert "HITHINK_FINANCE_API_KEY" in out["error"]


class TestConstituentsSuccess:
    def test_constituents_returns_stocks(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        stocks = [{"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"}]
        with patch.object(hithink_client, "get_items", return_value=stocks) as mock_get:
            out = json.loads(SectorBoardTool().execute(mode="constituents", thscode="886042.TI"))

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["thscode"] == "886042.TI"
        assert out["ok"] is True
        assert out["data"]["stocks"] == stocks


class TestRankingFallback:
    def test_ranking_prefers_hithink(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        catalog = [{"thscode": "881108.TI", "name": "化学原料"}]
        snapshot_data = {
            "item": [
                {"thscode": "881108.TI", "price_change_ratio_pct": 3.4, "last_price": 3398.0},
            ]
        }
        with patch.object(hithink_client, "get_items", return_value=catalog), patch.object(
            hithink_client, "get", return_value=snapshot_data
        ):
            out = json.loads(SectorBoardTool().execute(mode="ranking", limit=10))

        assert out["ok"] is True
        assert out["source"] == "hithink"
        assert out["data"]["boards"][0]["board_code"] == "881108.TI"
        assert out["data"]["boards"][0]["board_name"] == "化学原料"

    def test_ranking_falls_back_to_eastmoney_when_hithink_fails(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        eastmoney_payload = json.dumps(
            {
                "ok": True,
                "market": "stock",
                "source": "eastmoney",
                "mode": "ranking",
                "data": {"boards": [{"board_code": "BK0477", "board_name": "白酒"}]},
            },
            ensure_ascii=False,
        )
        with patch.object(
            hithink_client, "get_items", side_effect=RuntimeError("hithink down")
        ), patch(
            "src.tools.sector_board_tool.SectorInfoTool.execute",
            return_value=eastmoney_payload,
        ):
            out = json.loads(SectorBoardTool().execute(mode="ranking"))

        assert out["ok"] is True
        assert out["source"] == "eastmoney"
        assert "warnings" in out
        assert out["data"]["boards"][0]["board_code"] == "BK0477"

    def test_ranking_no_key_falls_back_to_eastmoney(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        eastmoney_payload = json.dumps(
            {
                "ok": True,
                "market": "stock",
                "source": "eastmoney",
                "mode": "ranking",
                "data": {"boards": []},
            },
            ensure_ascii=False,
        )
        with patch(
            "src.tools.sector_board_tool.SectorInfoTool.execute",
            return_value=eastmoney_payload,
        ) as mock_exec:
            out = json.loads(SectorBoardTool().execute(mode="ranking"))

        mock_exec.assert_called_once()
        assert out["ok"] is True
        assert out["source"] == "eastmoney"


class TestMembershipUsesEastmoney:
    def test_membership_delegates_to_eastmoney(self, monkeypatch):
        eastmoney_payload = json.dumps(
            {
                "ok": True,
                "market": "stock",
                "source": "eastmoney",
                "mode": "membership",
                "data": {"code": "600519.SH", "boards": [{"board_name": "白酒"}]},
            },
            ensure_ascii=False,
        )
        with patch(
            "src.tools.sector_board_tool.SectorInfoTool.execute",
            return_value=eastmoney_payload,
        ) as mock_exec:
            out = json.loads(SectorBoardTool().execute(mode="membership", code="600519.SH"))

        mock_exec.assert_called_once_with(mode="membership", code="600519.SH")
        assert out["ok"] is True
        assert out["source"] == "eastmoney"
        assert "warnings" in out

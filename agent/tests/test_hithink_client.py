"""Tests for the shared Hithink (同花顺) HTTP client.

All HTTP is mocked at :func:`backtest.loaders._http.throttled_get`, so no test
ever reaches the live ``fuyao.aicubes.cn`` endpoint.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backtest.loaders import hithink_client


def _response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestConfiguration:
    def test_is_configured_false_without_key(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        assert hithink_client.is_configured() is False

    def test_is_configured_true_with_key(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test-123")
        assert hithink_client.is_configured() is True

    def test_get_raises_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        with pytest.raises(hithink_client.HithinkUnavailable):
            hithink_client.get("/api/whatever")


class TestGetSuccess:
    def test_get_unwraps_data_and_sends_auth_header(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test-123")
        payload = {
            "code": 0,
            "message": "success",
            "request_id": "abc",
            "data": {"timestamp": 1, "item": [{"thscode": "600519.SH"}]},
        }
        with patch.object(
            hithink_client, "throttled_get", return_value=_response(payload)
        ) as http:
            data = hithink_client.get("/api/a-share/prices/snapshot", params={"thscodes": "600519.SH"})

        assert data == {"timestamp": 1, "item": [{"thscode": "600519.SH"}]}
        _, kwargs = http.call_args
        assert kwargs["headers"] == {"X-api-key": "sk-test-123"}
        assert kwargs["host_key"] == "hithink"
        assert kwargs["params"] == {"thscodes": "600519.SH"}

    def test_get_items_extracts_item_list(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test-123")
        payload = {
            "code": 0,
            "message": "success",
            "request_id": "abc",
            "data": {"timestamp": 1, "item": [{"a": 1}, {"a": 2}]},
        }
        with patch.object(hithink_client, "throttled_get", return_value=_response(payload)):
            items = hithink_client.get_items("/api/whatever")
        assert items == [{"a": 1}, {"a": 2}]

    def test_get_items_returns_empty_for_missing_item(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test-123")
        payload = {"code": 0, "message": "success", "request_id": "abc", "data": {"timestamp": 1}}
        with patch.object(hithink_client, "throttled_get", return_value=_response(payload)):
            items = hithink_client.get_items("/api/whatever")
        assert items == []


class TestGetBusinessError:
    def test_nonzero_code_raises_api_error(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test-123")
        payload = {
            "code": 1002,
            "message": "Unknown thscode: 600519",
            "request_id": "req-1",
            "data": None,
        }
        with patch.object(hithink_client, "throttled_get", return_value=_response(payload)):
            with pytest.raises(hithink_client.HithinkAPIError) as excinfo:
                hithink_client.get("/api/whatever")
        assert excinfo.value.code == 1002
        assert "Unknown thscode" in excinfo.value.api_message
        assert excinfo.value.request_id == "req-1"

    def test_auth_error_raises_api_error(self, monkeypatch):
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-bad-key")
        payload = {
            "code": 2003,
            "message": "Invalid or revoked API key",
            "request_id": "req-2",
            "data": None,
        }
        with patch.object(hithink_client, "throttled_get", return_value=_response(payload)):
            with pytest.raises(hithink_client.HithinkAPIError) as excinfo:
                hithink_client.get("/api/whatever")
        assert excinfo.value.code == 2003

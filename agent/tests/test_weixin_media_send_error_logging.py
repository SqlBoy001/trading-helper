"""Regression test: a network error while sending WeChat media must not crash
the error-logging path itself.

Real incident (2026-09-08): `send()`'s media-send branch caught
``httpx.TransportError`` and logged it via ``self.logger.opt(exception=True)``.
``self.logger`` is a stdlib ``logging.Logger`` (see ``BaseChannel.__init__``),
which has no ``.opt()`` — that method only exists on loguru's logger. Every
time this branch actually fired (a real network drop while sending a report
image), the error handler itself raised ``AttributeError: 'Logger' object has
no attribute 'opt'`` instead of logging a warning, masking the real network
error and aborting delivery outside the retry path that the outer ``except``
was supposed to hit.

This test drives that exact branch with the real stdlib ``self.logger`` (no
mocking the logger itself) and asserts the *original* network error still
propagates — not an ``AttributeError`` from the logging call that was
supposed to just log it and re-raise.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.channels.bus.events import OutboundMessage
from src.channels.weixin import WeixinChannel


class _FakeBus:
    """Minimal stand-in for the real MessageBus; unused by this test."""


def _make_channel() -> WeixinChannel:
    """Build a WeixinChannel with just enough state to reach the media-send branch."""
    channel = WeixinChannel({}, _FakeBus())
    channel._client = object()  # truthy sentinel; send() only checks it's not None
    channel._token = "fake-token"
    channel._context_tokens["chat-1"] = "ctx-token"
    return channel


class TestMediaSendNetworkErrorLogging:
    def test_transport_error_propagates_without_attribute_error(self, monkeypatch):
        channel = _make_channel()
        # self.logger must be a real stdlib Logger, exactly as BaseChannel
        # constructs it — this is the object that lacked .opt().
        assert isinstance(channel.logger, logging.Logger)

        async def _boom(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(channel, "_send_media_file", _boom)
        monkeypatch.setattr(channel, "_assert_session_active", lambda: None)
        monkeypatch.setattr(channel, "_stop_typing", _async_noop)
        monkeypatch.setattr(channel, "_flush_tool_hints", _async_noop)
        monkeypatch.setattr(channel, "_get_typing_ticket", _async_return(""))
        monkeypatch.setattr(
            channel, "_refresh_context_token_if_stale", _async_return("ctx-token")
        )

        msg = OutboundMessage(channel="weixin", chat_id="chat-1", content="", media=["/tmp/report.png"])

        with pytest.raises(httpx.ConnectError):
            import asyncio

            asyncio.run(channel.send(msg))

    def test_stdlib_logger_has_no_opt_method(self):
        """Documents the root cause directly: stdlib Logger lacks .opt()."""
        channel = _make_channel()
        assert not hasattr(channel.logger, "opt")


async def _async_noop(*args, **kwargs) -> None:
    return None


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner

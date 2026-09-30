"""Shared Hithink (同花顺 Financial-API) HTTP client.

Hithink (同花顺, Ths) publishes a paid, key-authenticated REST API at
``fuyao.aicubes.cn`` covering A-share sector/concept boards, special-data
(limit-up/down/break pools, dragon-tiger, hot lists, individual-stock
anomaly notes), valuations and financial indicators. It is configured as the
project's *first-priority* source for exactly the areas the free Eastmoney
feed cannot answer well (sector/board taxonomy, name/thscode resolution) —
callers try Hithink first and fall back to the existing free sources on any
failure, never the other way around.

Every request:

* Carries the required ``X-api-key`` header, read from
  ``HITHINK_FINANCE_API_KEY`` (:data:`src.config.env_schema.DataConfig`).
* Routes through :func:`backtest.loaders._http.throttled_get` for per-host
  spacing and session reuse (the same shared throttle every other direct-API
  loader in this project uses).
* Is wrapped in the documented response envelope
  ``{"code": int, "message": str, "request_id": str, "data": {...}}`` where
  ``code == 0`` is success and any non-zero ``code`` is a business error
  reported at HTTP 200 (see :class:`HithinkAPIError`).

This module knows nothing about any particular tool's output shape — it only
knows how to authenticate, throttle and unwrap the Hithink envelope. Callers
own their own field projection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backtest.loaders._http import resolve_min_interval, throttled_get
from src.config.accessor import get_env_config

logger = logging.getLogger(__name__)

# Several Hithink special-data endpoints (limit-up/down/break pools) address a
# trading day as a Unix-millisecond timestamp at Asia/Shanghai 00:00:00, not a
# plain YYYY-MM-DD string.
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

BASE_URL = "https://fuyao.aicubes.cn"

_HOST_KEY = "hithink"
_MIN_INTERVAL_ENV = "VIBE_TRADING_HITHINK_MIN_INTERVAL"
# Observed live: a burst of sequential calls without spacing returned
# code=429 ("request limit exceeded") on the 4th/5th request; 1.2s is a
# conservative default that leaves headroom under that ceiling.
_DEFAULT_MIN_INTERVAL = 1.2
_TIMEOUT_S = 15.0


class HithinkUnavailable(RuntimeError):
    """Raised when the Hithink source cannot be used at all (no key configured)."""


class HithinkAPIError(RuntimeError):
    """Raised when Hithink answers with a non-zero business ``code``.

    Attributes:
        code: The Hithink business error code (e.g. 1001-1004, 2001-2003,
            3001-3004, 4001, 5001-5003).
        api_message: The human-readable message Hithink returned.
        request_id: Hithink's request id, useful for support escalation.
    """

    def __init__(self, code: int, message: str, request_id: str | None) -> None:
        self.code = code
        self.api_message = message
        self.request_id = request_id
        super().__init__(f"Hithink API error {code}: {message}")


def api_key() -> str:
    """Return the configured Hithink API key, or ``""`` when unset."""
    return get_env_config().data.hithink_finance_api_key.strip()


def is_configured() -> bool:
    """Whether a Hithink API key is configured.

    Callers use this to decide whether Hithink is even worth trying before
    falling back to a free source — a request with no key would always fail,
    so skipping straight to the fallback avoids a pointless round trip.
    """
    return bool(api_key())


def _min_interval() -> float:
    """Resolve the per-call minimum Hithink request spacing in seconds."""
    return resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL)


def get(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """Issue a throttled, authenticated GET against a Hithink endpoint.

    Args:
        path: Endpoint path starting with ``/api/`` (e.g.
            ``"/api/a-share/prices/snapshot"``).
        params: Optional query parameters.

    Returns:
        The unwrapped ``data`` object from a successful envelope (may be
        ``None`` for an endpoint that legitimately returns no payload).

    Raises:
        HithinkUnavailable: No API key is configured.
        HithinkAPIError: The API responded with a non-zero business ``code``.
        requests.RequestException: Network failure.
        requests.HTTPError: Non-2xx HTTP status (Hithink normally returns 200
            even for business errors, so this signals a transport-level
            problem, not a business one).
        ValueError: Body is not valid JSON.
    """
    key = api_key()
    if not key:
        raise HithinkUnavailable("HITHINK_FINANCE_API_KEY is not configured")

    url = f"{BASE_URL}{path}"
    response = throttled_get(
        url,
        host_key=_HOST_KEY,
        min_interval=_min_interval(),
        params=params,
        headers={"X-api-key": key},
        timeout=_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise HithinkAPIError(-1, "malformed response: not a JSON object", None)

    code = payload.get("code")
    if code != 0:
        raise HithinkAPIError(
            int(code) if isinstance(code, (int, float)) else -1,
            str(payload.get("message") or "unknown error"),
            payload.get("request_id"),
        )
    return payload.get("data")


def shanghai_midnight_ms(date_str: str) -> int:
    """Convert a ``YYYY-MM-DD`` date to its Asia/Shanghai 00:00:00 Unix ms timestamp.

    Several Hithink special-data endpoints (limit-up/down/break pools) take a
    ``date_ms`` query parameter in exactly this form.

    Args:
        date_str: A date such as ``"2026-08-28"``.

    Returns:
        The millisecond Unix timestamp of that date's midnight in Shanghai.

    Raises:
        ValueError: ``date_str`` is not a valid ``YYYY-MM-DD`` date.
    """
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=_SHANGHAI_TZ)
    return int(dt.timestamp() * 1000)


def get_items(path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Convenience wrapper: GET an endpoint and return its ``data.item`` list.

    Nearly every Hithink read endpoint nests its row list under
    ``data.item``. This helper defensively extracts that list; endpoints with
    a differently-shaped payload (e.g. dragon-tiger's ``stock_items`` /
    ``hot_money_items``) should call :func:`get` directly instead.

    Args:
        path: Endpoint path starting with ``/api/``.
        params: Optional query parameters.

    Returns:
        The ``item`` list, or ``[]`` when absent/malformed.

    Raises:
        HithinkUnavailable: No API key is configured.
        HithinkAPIError: The API responded with a non-zero business ``code``.
        requests.RequestException: Network failure.
        requests.HTTPError: Non-2xx HTTP status.
        ValueError: Body is not valid JSON.
    """
    data = get(path, params=params)
    if not isinstance(data, dict):
        return []
    items = data.get("item")
    if not isinstance(items, list):
        return []
    return [row for row in items if isinstance(row, dict)]

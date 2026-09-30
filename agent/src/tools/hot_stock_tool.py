"""同花顺热榜 (Hithink hot-stock rank lists) — Hithink-only, no fallback source.

Hithink's hot-list surfaces same-day attention/heat rankings distinct from
price action: a stock can be climbing the hot list well before (or without)
hitting a limit-up, which is exactly the early-warning signal a "打板"
(limit-chase) workflow wants for tomorrow's watchlist. Neither the free
Eastmoney feed nor Tushare fallback used elsewhere in this project expose an
equivalent attention-ranking dataset, so this tool has no fallback and
reports a clean error when ``HITHINK_FINANCE_API_KEY`` is unset.

Four views, one tool:

* ``skyrocket`` — Top-30 "飙升榜" (rank-surge board): stocks climbing fastest
  in attention rank right now. The sharpest early-warning signal.
* ``hot`` — Top-30 "热股榜" (hot-stock board): stocks with the highest
  absolute attention right now, less noisy than the surge board.
* ``history`` — the hot-stock board as it stood on a past natural day
  (within the last year).
* ``rank_trend`` — one stock's hot-list rank over a date window, to see
  whether its attention has been building or fading.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders import hithink_client
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_KEY_ENV = "HITHINK_FINANCE_API_KEY"

_SKYROCKET_PATH = "/api/a-share/special-data/skyrocket-list"
_HOT_PATH = "/api/a-share/special-data/hot-stock-list"
_HISTORY_PATH = "/api/a-share/special-data/hot-stock-list-history"
_RANK_TREND_PATH = "/api/a-share/special-data/hot-stock-rank-trend"

_VALID_VIEWS = ("skyrocket", "hot", "history", "rank_trend")
_VALID_PERIODS = ("day", "hour")


def _error(message: str) -> str:
    """Build the failure envelope as a JSON string."""
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _no_key_error(what: str) -> str:
    return _error(
        f"Hithink API key not configured; {what} has no free-source equivalent "
        f"in this project — set {_KEY_ENV} to enable it"
    )


class HotStockTool(BaseTool):
    """Fetch Hithink's 同花顺热榜 (hot-stock attention rank) views."""

    name = "get_hot_stocks"
    description = (
        "Fetch Hithink (同花顺) hot-stock attention-rank lists — a same-day "
        "\"who is the market watching right now\" signal, distinct from price "
        "action, useful for spotting tomorrow's momentum/\"打板\" candidates "
        "before or without a limit-up print. Four views via 'view': "
        "(1) 'skyrocket' — Top-30 fastest attention-rank climbers right now "
        "(the sharpest early-warning signal); (2) 'hot' — Top-30 highest "
        "absolute attention right now (less noisy, more established names); "
        "(3) 'history' — the hot-stock board as it stood on a past natural "
        "day (requires 'date', within the last year); (4) 'rank_trend' — one "
        "stock's hot-list rank over a date window, to see if its attention is "
        "building or fading (requires 'thscode', 'start_date', 'end_date'). "
        "Requires HITHINK_FINANCE_API_KEY (no free-source equivalent exists in "
        'this project). Example: {"view": "skyrocket", "period": "hour"}.'
    )
    @classmethod
    def check_available(cls) -> bool:
        """Available only when a Hithink API key is configured.

        Returns:
            ``True`` when ``HITHINK_FINANCE_API_KEY`` is set; ``False``
            otherwise, which silently excludes the tool from the registry —
            there is no free-source fallback for this dataset, so a key-less
            install should never see a tool it cannot call.
        """
        return hithink_client.is_configured()

    parameters = {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "enum": list(_VALID_VIEWS),
                "description": (
                    "'skyrocket' (rank-surge Top-30), 'hot' (attention Top-30), "
                    "'history' (past-day snapshot, needs 'date'), 'rank_trend' "
                    "(one stock's rank over time, needs 'thscode'/'start_date'/"
                    "'end_date')."
                ),
            },
            "period": {
                "type": "string",
                "enum": list(_VALID_PERIODS),
                "description": (
                    "For 'skyrocket'/'hot' only: 'day' (default) or 'hour' "
                    "cadence board."
                ),
                "default": "day",
            },
            "date": {
                "type": "string",
                "description": (
                    "Natural day YYYY-MM-DD for view='history'. Only the last "
                    "year of history is available."
                ),
            },
            "thscode": {
                "type": "string",
                "description": (
                    "A-share symbol for view='rank_trend', e.g. '300034.SZ'."
                ),
            },
            "start_date": {
                "type": "string",
                "description": "Window start YYYY-MM-DD for view='rank_trend'.",
            },
            "end_date": {
                "type": "string",
                "description": (
                    "Window end YYYY-MM-DD for view='rank_trend'; must be >= "
                    "start_date. Window and dates are both limited to the last "
                    "year."
                ),
            },
        },
        "required": ["view"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to the requested view and return a JSON envelope.

        Args:
            **kwargs: ``view`` (required), plus view-specific params.

        Returns:
            A JSON envelope string. On success: ``{"ok": true, "market":
            "a_share", "source": "hithink", "view": ..., "data": {...}}``. On
            failure: ``{"ok": false, "error": "..."}``.
        """
        view = kwargs.get("view")
        if view not in _VALID_VIEWS:
            return _error(f"view must be one of {list(_VALID_VIEWS)}")

        if not hithink_client.is_configured():
            return _no_key_error(f"view='{view}'")

        if view in ("skyrocket", "hot"):
            return self._board(view, kwargs)
        if view == "history":
            return self._history(kwargs)
        return self._rank_trend(kwargs)

    def _board(self, view: str, kwargs: dict[str, Any]) -> str:
        period = kwargs.get("period", "day")
        if period not in _VALID_PERIODS:
            return _error(f"period must be one of {list(_VALID_PERIODS)}")

        path = _SKYROCKET_PATH if view == "skyrocket" else _HOT_PATH
        try:
            data = hithink_client.get(path, params={"period": period})
        except hithink_client.HithinkUnavailable:
            return _no_key_error(f"view='{view}'")
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope, no fallback source
            logger.warning("hithink %s list fetch failed: %s", view, exc)
            return _error(f"Hithink {view} list fetch failed: {exc}")

        items = data.get("item") if isinstance(data, dict) else None
        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "view": view,
                "data": {
                    "period": period,
                    "items": [row for row in (items or []) if isinstance(row, dict)],
                },
            },
            ensure_ascii=False,
        )

    def _history(self, kwargs: dict[str, Any]) -> str:
        date_arg = kwargs.get("date")
        if not isinstance(date_arg, str) or not date_arg.strip():
            return _error("date is required for view='history'")

        try:
            data = hithink_client.get(_HISTORY_PATH, params={"date": date_arg.strip()})
        except hithink_client.HithinkUnavailable:
            return _no_key_error("view='history'")
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope, no fallback source
            logger.warning("hithink hot-stock history fetch failed for %s: %s", date_arg, exc)
            return _error(f"Hithink hot-stock history fetch failed: {exc}")

        if not isinstance(data, dict):
            return _error("Hithink hot-stock history returned a malformed payload")
        items = data.get("item")
        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "view": "history",
                "data": {
                    "date": data.get("date"),
                    "items": [row for row in (items or []) if isinstance(row, dict)],
                },
            },
            ensure_ascii=False,
        )

    def _rank_trend(self, kwargs: dict[str, Any]) -> str:
        thscode = kwargs.get("thscode")
        start_date = kwargs.get("start_date")
        end_date = kwargs.get("end_date")
        if not isinstance(thscode, str) or not thscode.strip():
            return _error("thscode is required for view='rank_trend'")
        if not isinstance(start_date, str) or not start_date.strip():
            return _error("start_date is required for view='rank_trend'")
        if not isinstance(end_date, str) or not end_date.strip():
            return _error("end_date is required for view='rank_trend'")

        try:
            data = hithink_client.get(
                _RANK_TREND_PATH,
                params={
                    "thscode": thscode.strip(),
                    "start_date": start_date.strip(),
                    "end_date": end_date.strip(),
                },
            )
        except hithink_client.HithinkUnavailable:
            return _no_key_error("view='rank_trend'")
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope, no fallback source
            logger.warning("hithink hot-stock rank trend fetch failed for %s: %s", thscode, exc)
            return _error(f"Hithink hot-stock rank trend fetch failed: {exc}")

        if not isinstance(data, dict):
            return _error("Hithink hot-stock rank trend returned a malformed payload")
        items = data.get("item")
        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "view": "rank_trend",
                "data": {
                    "thscode": thscode.strip(),
                    "points": [row for row in (items or []) if isinstance(row, dict)],
                },
            },
            ensure_ascii=False,
        )

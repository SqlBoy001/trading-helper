"""A-share limit-up/limit-down/limit-break pools + limit-up ladder (Hithink-only).

Hithink (同花顺 Financial-API) is the only configured source for this data —
neither the free Eastmoney feed nor Tushare fallback used elsewhere in this
project expose a maintained limit-pool/ladder dataset, so this tool has no
fallback source and reports a clean error when ``HITHINK_FINANCE_API_KEY`` is
unset rather than silently returning nothing.

Four pool types, one tool:

* ``limit_up`` — today's limit-up / consecutive-board (连板) stocks, with the
  limit-up reason, time and seal (封单) amount. The core "打板" (limit-chase)
  scan input.
* ``limit_down`` — today's limit-down stocks.
* ``limit_break`` — today's "炸板" (opened after hitting the limit) stocks,
  with the open count — a sentiment-fade / risk signal.
* ``ladder`` — the 30-trading-day consecutive-board ladder matrix (two-board
  through seven-plus-board), the standard way to read market-wide "打板"
  sentiment strength and spot which names are climbing the ladder.

All four are read-only, single-day (except ``ladder``, which has no date
parameter and always returns its own 30-day window).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders import hithink_client
from src.agent.tools import BaseTool
from src.tools._result_paging import fit_records

logger = logging.getLogger(__name__)

_KEY_ENV = "HITHINK_FINANCE_API_KEY"

_POOL_PATHS: dict[str, str] = {
    "limit_up": "/api/a-share/special-data/limit-up-pool",
    "limit_down": "/api/a-share/special-data/limit-down-pool",
    "limit_break": "/api/a-share/special-data/limit-break-pool",
}
_LADDER_PATH = "/api/a-share/special-data/limit-up-ladder"

_VALID_POOLS = ("limit_up", "limit_down", "limit_break", "ladder")

# Per-pool sort_field whitelist, matching each endpoint's documented enum.
_SORT_FIELDS: dict[str, tuple[str, ...]] = {
    "limit_up": ("last_price", "continue_day_cnt", "seal_money", "limit_up_time"),
    "limit_down": (
        "last_limit_time", "first_limit_time", "last_price",
        "price_change_ratio_pct", "turnover_ratio_pct",
    ),
    "limit_break": (
        "price_change_ratio_pct", "open_times", "last_price",
        "turnover_ratio_pct", "turnover",
    ),
}
_DEFAULT_SORT_FIELD: dict[str, str] = {
    "limit_up": "last_price",
    "limit_down": "last_limit_time",
    "limit_break": "price_change_ratio_pct",
}
_VALID_SORT_DIRS = ("asc", "desc")

_MIN_SIZE = 1
_MAX_SIZE = 200
_DEFAULT_SIZE = 50

# The ladder endpoint always returns its full 30-trading-day window server
# side (no upstream paging parameter) — observed live at ~55K characters /
# ~427 stock rows, several times the per-tool result budget. Days are paged
# client-side (newest first) via fit_records so a request never gets a
# silently truncated tail; days_limit caps how many days are requested
# per call before the character-budget fit_records applies on top.
_MAX_LADDER_DAYS = 30
_DEFAULT_LADDER_DAYS = 5


def _error(message: str) -> str:
    """Build the failure envelope as a JSON string."""
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _no_key_error(what: str) -> str:
    return _error(
        f"Hithink API key not configured; {what} has no free-source equivalent "
        f"in this project — set {_KEY_ENV} to enable it"
    )


def _clamp_size(value: Any) -> int:
    """Coerce a requested page size into ``[1, 200]``, defaulting on bad input."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_SIZE
    return max(_MIN_SIZE, min(n, _MAX_SIZE))


class LimitPoolTool(BaseTool):
    """Fetch A-share limit-up/limit-down/limit-break pools or the limit-up ladder."""

    name = "get_limit_pools"
    description = (
        "Fetch Chinese A-share limit-up/limit-down/limit-break (涨停/跌停/炸板) "
        "pools and the 30-day consecutive-board (连板) ladder from Hithink "
        "(同花顺) — the standard \"打板\" (limit-chase) scan inputs for finding "
        "momentum/sentiment names and gauging market-wide risk appetite. Four "
        "pool types via 'pool': (1) 'limit_up' — today's limit-up/consecutive-"
        "board stocks with limit-up reason, time and seal amount, the core "
        "candidate list for tomorrow's watchlist; (2) 'limit_down' — today's "
        "limit-down stocks; (3) 'limit_break' — today's stocks that opened "
        "after hitting the limit (炸板), a fade/risk signal, ranked by open "
        "count; (4) 'ladder' — the 30-trading-day consecutive-board matrix "
        "(2-board through 7+-board), showing which names are climbing and how "
        "strong overall board sentiment is. Requires HITHINK_FINANCE_API_KEY "
        "(no free-source equivalent exists in this project). "
        'Example: {"pool": "limit_up", "sort_field": "continue_day_cnt", '
        '"sort_dir": "desc"}.'
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
            "pool": {
                "type": "string",
                "enum": list(_VALID_POOLS),
                "description": (
                    "'limit_up' (today's limit-up/board pool), 'limit_down' "
                    "(today's limit-down pool), 'limit_break' (today's opened-"
                    "after-limit pool), or 'ladder' (30-day consecutive-board "
                    "matrix, no date/paging params)."
                ),
            },
            "date": {
                "type": "string",
                "description": (
                    "Trading date YYYY-MM-DD for pool='limit_up'/'limit_down'/"
                    "'limit_break'. Omit for the current natural day. Ignored "
                    "for pool='ladder' (always returns its own 30-day window)."
                ),
            },
            "sort_field": {
                "type": "string",
                "description": (
                    "Sort column, pool-specific: limit_up accepts last_price/"
                    "continue_day_cnt/seal_money/limit_up_time; limit_down "
                    "accepts last_limit_time/first_limit_time/last_price/"
                    "price_change_ratio_pct/turnover_ratio_pct; limit_break "
                    "accepts price_change_ratio_pct/open_times/last_price/"
                    "turnover_ratio_pct/turnover. Ignored for pool='ladder'."
                ),
            },
            "sort_dir": {
                "type": "string",
                "enum": list(_VALID_SORT_DIRS),
                "description": "Sort direction. Default 'desc'. Ignored for pool='ladder'.",
                "default": "desc",
            },
            "page": {
                "type": "integer",
                "description": "Page number, >= 1. Default 1. Ignored for pool='ladder'.",
                "default": 1,
            },
            "size": {
                "type": "integer",
                "description": f"Page size, 1-{_MAX_SIZE}. Default {_DEFAULT_SIZE}. Ignored for pool='ladder'.",
                "default": _DEFAULT_SIZE,
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Result-page offset for a response whose 'items'/'days' "
                    "overflowed the character budget in one call — read "
                    "paging.next_offset in the response and call again with "
                    "that value to continue. Defaults to 0 (first page)."
                ),
            },
            "days": {
                "type": "integer",
                "description": (
                    f"For pool='ladder' only: number of most-recent trading "
                    f"days to return (1-{_MAX_LADDER_DAYS}, default "
                    f"{_DEFAULT_LADDER_DAYS}). The upstream endpoint always "
                    "computes the full 30-day window; this only trims how "
                    "much of it is returned. Ignored for the other three pools."
                ),
                "default": _DEFAULT_LADDER_DAYS,
            },
        },
        "required": ["pool"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Validate inputs, fetch the requested pool/ladder, and return an envelope.

        Args:
            **kwargs: ``pool`` (required), plus ``date``/``sort_field``/
                ``sort_dir``/``page``/``size`` for the three pool types.

        Returns:
            A JSON envelope string. On success: ``{"ok": true, "market":
            "a_share", "source": "hithink", "pool": ..., "data": {...}}``. On
            failure: ``{"ok": false, "error": "..."}``.
        """
        pool = kwargs.get("pool")
        if pool not in _VALID_POOLS:
            return _error(f"pool must be one of {list(_VALID_POOLS)}")

        if not hithink_client.is_configured():
            return _no_key_error(f"pool='{pool}'")

        if pool == "ladder":
            return self._ladder(kwargs)
        return self._pool(pool, kwargs)

    def _pool(self, pool: str, kwargs: dict[str, Any]) -> str:
        params: dict[str, Any] = {}

        date_arg = kwargs.get("date")
        if isinstance(date_arg, str) and date_arg.strip():
            try:
                params["date_ms"] = hithink_client.shanghai_midnight_ms(date_arg.strip())
            except ValueError as exc:
                return _error(str(exc))

        sort_field = kwargs.get("sort_field")
        if sort_field is not None:
            if sort_field not in _SORT_FIELDS[pool]:
                return _error(f"sort_field for pool='{pool}' must be one of {list(_SORT_FIELDS[pool])}")
            params["sort_field"] = sort_field
        else:
            params["sort_field"] = _DEFAULT_SORT_FIELD[pool]

        sort_dir = kwargs.get("sort_dir", "desc")
        if sort_dir not in _VALID_SORT_DIRS:
            return _error(f"sort_dir must be one of {list(_VALID_SORT_DIRS)}")
        params["sort_dir"] = sort_dir

        try:
            page = int(kwargs.get("page", 1))
        except (TypeError, ValueError, OverflowError):
            return _error("page must be a positive integer")
        if page < 1:
            return _error("page must be >= 1")
        params["page"] = page
        params["size"] = _clamp_size(kwargs.get("size", _DEFAULT_SIZE))

        try:
            data = hithink_client.get(_POOL_PATHS[pool], params=params)
        except hithink_client.HithinkUnavailable:
            return _no_key_error(f"pool='{pool}'")
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope, no fallback source
            logger.warning("hithink %s pool fetch failed: %s", pool, exc)
            return _error(f"Hithink {pool} pool fetch failed: {exc}")

        items = data.get("item") if isinstance(data, dict) else None
        pagination = data.get("pagination") if isinstance(data, dict) else None
        rows = [row for row in (items or []) if isinstance(row, dict)]

        try:
            offset = max(int(kwargs.get("offset") or 0), 0)
        except (TypeError, ValueError):
            return _error("offset must be an integer")

        # Server-side `size` already bounds the upstream page, but a wide
        # `size` (e.g. 200) can still overflow the per-tool character budget
        # on a busy trading day. Page client-side too so an overflow degrades
        # to a self-describing partial page (with `paging.next_offset`)
        # instead of a silent hard cut the model has no way to detect.
        def _build(page: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "pool": pool,
                "paging": meta,
                "data": {"pagination": pagination, "items": page},
            }

        return fit_records(rows, offset, _build)

    def _ladder(self, kwargs: dict[str, Any]) -> str:
        try:
            days_limit = int(kwargs.get("days", _DEFAULT_LADDER_DAYS))
        except (TypeError, ValueError, OverflowError):
            return _error("days must be a positive integer")
        if days_limit < 1:
            return _error("days must be >= 1")
        days_limit = min(days_limit, _MAX_LADDER_DAYS)

        try:
            data = hithink_client.get(_LADDER_PATH)
        except hithink_client.HithinkUnavailable:
            return _no_key_error("pool='ladder'")
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope, no fallback source
            logger.warning("hithink limit-up ladder fetch failed: %s", exc)
            return _error(f"Hithink limit-up ladder fetch failed: {exc}")

        if not isinstance(data, dict):
            return _error("Hithink limit-up ladder returned a malformed payload")
        all_days = [row for row in (data.get("item") or []) if isinstance(row, dict)]
        window = data.get("window")

        # The upstream endpoint always returns its full 30-day window
        # (~55K characters observed live, several times the per-tool result
        # budget) with no server-side paging parameter. `days` narrows the
        # request to the most recent N days (the ladder's own item order is
        # already newest-first) before the character-budget fit_records pass,
        # so a caller who only wants "today's ladder" never pays for — or
        # silently loses part of — the full 30-day payload.
        requested = all_days[:days_limit]

        try:
            offset = max(int(kwargs.get("offset") or 0), 0)
        except (TypeError, ValueError):
            return _error("offset must be an integer")

        def _build(page: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "pool": "ladder",
                "paging": meta,
                "data": {"window": window, "days": page},
            }

        return fit_records(requested, offset, _build)

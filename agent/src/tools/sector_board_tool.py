"""Sector / concept board tool backed primarily by Hithink (同花顺 Financial-API).

Addresses the "行业数据获取不到" gap: the free Eastmoney source
(:mod:`src.tools.sector_tool`) can only answer "what boards does this stock
belong to" and "rank today's industry boards" — it has no board *catalog*
(browse all concept/region/industry boards by tag) and no *constituents*
lookup (list every stock in a given board). Hithink's ``a-share-index`` API
adds both.

Per the project's configured source priority, Hithink is tried first for
every mode; a mode with a free equivalent (``ranking``, ``membership``) falls
back to the existing Eastmoney-backed :class:`~src.tools.sector_tool.SectorInfoTool`
on any Hithink failure (unconfigured key, network error, rate limit, business
error). ``catalog``, ``constituents`` and ``search`` have no free equivalent
in this project (board/fund thscodes like ``886042.TI`` / ``012414.OF`` are
outside the coverage of the existing Eastmoney-backed ``search_symbol``
tool), so those modes report a clean error when Hithink is unavailable
instead of silently returning nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders import hithink_client
from src.agent.tools import BaseTool
from src.tools.sector_tool import SectorInfoTool

logger = logging.getLogger(__name__)

_CATALOG_PATH = "/api/a-share-index/catalog/ths-index-list"
_CONSTITUENTS_PATH = "/api/a-share-index/constituents/ths-stock-list"
_INDEX_SNAPSHOT_PATH = "/api/a-share-index/prices/snapshot"
_META_SEARCH_PATH = "/api/meta/tickers/search"

_VALID_TAGS = ("cn_concept", "region", "tszs", "industry")
_VALID_MODES = ("catalog", "constituents", "ranking", "membership", "search")

_MAX_RANKING = 100
_DEFAULT_RANKING = 30
_MAX_SEARCH_LIMIT = 25
_DEFAULT_SEARCH_LIMIT = 10
# Hithink's index snapshot takes a comma-joined thscodes list; batching keeps
# any single request URL/response bounded even for the full industry catalog
# (~90-ish boards as observed live).
_SNAPSHOT_BATCH_SIZE = 60


def _error(message: str) -> str:
    """Build the failure envelope as a JSON string."""
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _fetch_catalog(tag: str) -> list[dict[str, Any]]:
    """Fetch the full board catalog for one tag from Hithink.

    Args:
        tag: One of :data:`_VALID_TAGS`.

    Returns:
        A list of ``{thscode, name}`` dicts.

    Raises:
        hithink_client.HithinkUnavailable: No API key configured.
        hithink_client.HithinkAPIError: Non-zero business error code.
        requests.RequestException: Network failure.
    """
    return hithink_client.get_items(_CATALOG_PATH, params={"tag": tag})


def _fetch_constituents(thscode: str) -> list[dict[str, Any]]:
    """Fetch the constituent stock list for one board/index thscode."""
    return hithink_client.get_items(
        _CONSTITUENTS_PATH, params={"thscode": thscode}
    )


def _fetch_ranking_via_hithink(limit: int) -> dict[str, Any]:
    """Rank industry boards by today's percent change using two Hithink calls.

    Fetches the full industry catalog, batches an index-snapshot request over
    every industry thscode, sorts by ``price_change_ratio_pct`` descending,
    and merges the board name back in (the snapshot endpoint does not return
    names).

    Args:
        limit: Number of top boards to keep.

    Returns:
        ``{"boards": [...]}`` shaped like the Eastmoney ranking payload
        (``board_code``, ``board_name``, ``change_pct``, ``index``), so a
        caller cannot tell which source answered from the row shape alone.

    Raises:
        hithink_client.HithinkUnavailable: No API key configured.
        hithink_client.HithinkAPIError: Non-zero business error code.
        requests.RequestException: Network failure.
    """
    boards = _fetch_catalog("industry")
    name_by_code = {
        str(row.get("thscode")): str(row.get("name") or "")
        for row in boards
        if row.get("thscode")
    }
    codes = list(name_by_code.keys())
    if not codes:
        return {"boards": []}

    snapshots: list[dict[str, Any]] = []
    for start in range(0, len(codes), _SNAPSHOT_BATCH_SIZE):
        batch = codes[start : start + _SNAPSHOT_BATCH_SIZE]
        data = hithink_client.get(
            _INDEX_SNAPSHOT_PATH, params={"thscodes": ",".join(batch)}
        )
        items = data.get("item") if isinstance(data, dict) else None
        if isinstance(items, list):
            snapshots.extend(row for row in items if isinstance(row, dict))

    ranked = sorted(
        snapshots,
        key=lambda row: row.get("price_change_ratio_pct")
        if isinstance(row.get("price_change_ratio_pct"), (int, float))
        else float("-inf"),
        reverse=True,
    )
    out = [
        {
            "board_code": str(row.get("thscode")),
            "board_name": name_by_code.get(str(row.get("thscode")), ""),
            "change_pct": row.get("price_change_ratio_pct"),
            "index": row.get("last_price"),
            "up_count": None,
            "down_count": None,
            "leader": None,
        }
        for row in ranked[:limit]
    ]
    return {"boards": out}


def _fetch_search(query: str, limit: int) -> list[dict[str, Any]]:
    """Resolve a free-text name/ticker to Hithink thscode candidates.

    Covers asset types ``search_symbol`` does not (board indices ``.TI``,
    OTC/exchange funds ``.OF``/.SZ/.SH, REITs), which is exactly what a caller
    needs before calling ``catalog``/``constituents`` with a board name like
    "白酒" instead of its thscode.

    Args:
        query: Free-text company/board/fund name or ticker fragment.
        limit: Max candidates to keep.

    Returns:
        A list of ``{thscode, ticker, name, exchange, asset_type, currency}``
        dicts, capped at ``limit``.
    """
    items = hithink_client.get_items(_META_SEARCH_PATH, params={"q": query})
    return items[:limit]


class SectorBoardTool(BaseTool):
    """Browse/search Hithink board taxonomy; rank boards; find board membership."""

    name = "get_sector_board"
    description = (
        "Look up Chinese A-share sector/concept/region board taxonomy. Primary "
        "source: Hithink (同花顺) Financial-API; falls back to the free "
        "Eastmoney-backed source for modes it can also serve. Five modes: "
        "(1) 'search' — resolve a free-text name (e.g. '白酒', '新能源车') to "
        "board/index/fund thscodes (covers .TI board codes and .OF/fund codes "
        "that search_symbol does not); (2) 'catalog' — list all boards under a "
        "tag ('cn_concept'=concept boards, 'region'=region boards, "
        "'tszs'=featured/thematic boards, 'industry'=industry boards); "
        "(3) 'constituents' — list every stock in one board/index thscode "
        "(e.g. 886042.TI or 000300.SH); (4) 'ranking' — rank industry boards by "
        "today's percent change; (5) 'membership' — given a stock 'code', list "
        "the boards it belongs to (Eastmoney only; Hithink does not yet expose "
        "this). Use 'search' then 'catalog'/'constituents' to solve \"which "
        "stocks are in sector X\" without knowing the board's thscode up front. "
        'Example: {"mode": "search", "query": "白酒"} then '
        '{"mode": "constituents", "thscode": "881273.TI"}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(_VALID_MODES),
                "description": (
                    "'search' resolves a name to thscodes (requires 'query'). "
                    "'catalog' lists all boards under 'tag'. 'constituents' "
                    "lists stocks in one board/index 'thscode'. 'ranking' "
                    "ranks industry boards by today's change (ignores other "
                    "params). 'membership' lists the boards a stock 'code' "
                    "belongs to."
                ),
            },
            "query": {
                "type": "string",
                "description": "Free-text name/ticker fragment. Required for mode='search'.",
            },
            "tag": {
                "type": "string",
                "enum": list(_VALID_TAGS),
                "description": (
                    "Board category for mode='catalog': 'cn_concept' (concept "
                    "boards), 'region' (region boards), 'tszs' (featured/"
                    "thematic boards), 'industry' (industry boards)."
                ),
                "default": "cn_concept",
            },
            "thscode": {
                "type": "string",
                "description": (
                    "Board/index thscode for mode='constituents', e.g. "
                    "'886042.TI' (Hithink board) or '000300.SH' (CSI 300)."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "A-share stock symbol for mode='membership', e.g. "
                    "'600519.SH'."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Result cap. mode='ranking': top boards (1-{_MAX_RANKING}, "
                    f"default {_DEFAULT_RANKING}). mode='search': candidates "
                    f"(1-{_MAX_SEARCH_LIMIT}, default {_DEFAULT_SEARCH_LIMIT})."
                ),
            },
        },
        "required": ["mode"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to the requested mode and return a JSON envelope.

        Args:
            **kwargs: ``mode`` (required), plus mode-specific ``query`` / ``tag``
                / ``thscode`` / ``code`` / ``limit``.

        Returns:
            A JSON envelope string. On success: ``{"ok": true, "market":
            "a_share", "source": "hithink"|"eastmoney", "mode": ..., "data":
            {...}}`` (an Eastmoney-served ``ranking``/``membership`` fallback
            also carries a ``warnings`` list). On failure: ``{"ok": false,
            "error": "..."}``.
        """
        mode = kwargs.get("mode")
        if mode not in _VALID_MODES:
            return _error(f"mode must be one of {list(_VALID_MODES)}")

        if mode == "search":
            return self._search(kwargs)
        if mode == "catalog":
            return self._catalog(kwargs)
        if mode == "constituents":
            return self._constituents(kwargs)
        if mode == "ranking":
            return self._ranking(kwargs)
        return self._membership(kwargs)

    def _search(self, kwargs: dict[str, Any]) -> str:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error("query is required and must be a non-empty string for mode='search'")
        limit = _clamp(kwargs.get("limit", _DEFAULT_SEARCH_LIMIT), _DEFAULT_SEARCH_LIMIT, _MAX_SEARCH_LIMIT)

        try:
            candidates = _fetch_search(query.strip(), limit)
        except hithink_client.HithinkUnavailable as exc:
            return _error(
                f"Hithink search unavailable ({exc}); this asset universe "
                "(board/fund thscodes) has no free-source equivalent in this "
                "project — configure HITHINK_FINANCE_API_KEY to enable it"
            )
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope
            logger.warning("hithink meta search failed for %r: %s", query, exc)
            return _error(f"Hithink search failed: {exc}")

        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "mode": "search",
                "data": {"query": query.strip(), "count": len(candidates), "candidates": candidates},
            },
            ensure_ascii=False,
        )

    def _catalog(self, kwargs: dict[str, Any]) -> str:
        tag = kwargs.get("tag", "cn_concept")
        if tag not in _VALID_TAGS:
            return _error(f"tag must be one of {list(_VALID_TAGS)}")

        try:
            boards = _fetch_catalog(tag)
        except hithink_client.HithinkUnavailable as exc:
            return _error(
                f"Hithink catalog unavailable ({exc}); the full board catalog "
                "has no free-source equivalent in this project — configure "
                "HITHINK_FINANCE_API_KEY to enable it"
            )
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope
            logger.warning("hithink catalog fetch failed for tag=%s: %s", tag, exc)
            return _error(f"Hithink catalog fetch failed: {exc}")

        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "mode": "catalog",
                "data": {"tag": tag, "count": len(boards), "boards": boards},
            },
            ensure_ascii=False,
        )

    def _constituents(self, kwargs: dict[str, Any]) -> str:
        thscode = kwargs.get("thscode")
        if not isinstance(thscode, str) or not thscode.strip():
            return _error("thscode is required for mode='constituents'")

        try:
            stocks = _fetch_constituents(thscode.strip())
        except hithink_client.HithinkUnavailable as exc:
            return _error(
                f"Hithink constituents unavailable ({exc}); board constituent "
                "lookups have no free-source equivalent in this project — "
                "configure HITHINK_FINANCE_API_KEY to enable it"
            )
        except Exception as exc:  # noqa: BLE001 - surface as a clean envelope
            logger.warning("hithink constituents fetch failed for %s: %s", thscode, exc)
            return _error(f"Hithink constituents fetch failed: {exc}")

        return json.dumps(
            {
                "ok": True,
                "market": "a_share",
                "source": "hithink",
                "mode": "constituents",
                "data": {"thscode": thscode.strip(), "count": len(stocks), "stocks": stocks},
            },
            ensure_ascii=False,
        )

    def _ranking(self, kwargs: dict[str, Any]) -> str:
        limit = _clamp(kwargs.get("limit", _DEFAULT_RANKING), _DEFAULT_RANKING, _MAX_RANKING)

        try:
            data = _fetch_ranking_via_hithink(limit)
            return json.dumps(
                {
                    "ok": True,
                    "market": "a_share",
                    "source": "hithink",
                    "mode": "ranking",
                    "data": data,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the free source
            logger.info("hithink ranking failed (%s); falling back to eastmoney", exc)

        fallback_raw = SectorInfoTool().execute(mode="ranking", limit=limit)
        try:
            fallback = json.loads(fallback_raw)
        except (TypeError, ValueError):
            return _error(f"Hithink ranking failed and eastmoney fallback returned malformed output")
        if not fallback.get("ok"):
            return _error(
                f"Hithink ranking failed; eastmoney fallback also failed: "
                f"{fallback.get('error')}"
            )
        fallback["mode"] = "ranking"
        fallback["warnings"] = ["hithink unavailable/failed; used eastmoney fallback"]
        return json.dumps(fallback, ensure_ascii=False)

    def _membership(self, kwargs: dict[str, Any]) -> str:
        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            return _error("code is required for mode='membership'")

        # Hithink's membership endpoint is documented as "coming soon" (敬请期待)
        # as of this integration, so this mode is served by Eastmoney directly
        # rather than attempting a call that would always fail.
        fallback_raw = SectorInfoTool().execute(mode="membership", code=code.strip())
        try:
            fallback = json.loads(fallback_raw)
        except (TypeError, ValueError):
            return _error("eastmoney membership lookup returned malformed output")
        if not fallback.get("ok"):
            return _error(str(fallback.get("error")))
        fallback["mode"] = "membership"
        fallback["warnings"] = [
            "Hithink does not yet expose board membership; served by eastmoney"
        ]
        return json.dumps(fallback, ensure_ascii=False)


def _clamp(value: Any, default: int, maximum: int) -> int:
    """Coerce a requested count into ``[1, maximum]``, defaulting on bad input."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(n, maximum))

"""Dragon-Tiger (龙虎榜) board tool backed by the Eastmoney datacenter API.

The Shanghai/Shenzhen exchanges publish a daily "dragon-tiger" (龙虎榜) board
listing every A-share that triggered an abnormal-trading disclosure, together
with the brokerage seats that drove the largest buys and sells. Eastmoney
re-serves this disclosure through its free, no-auth ``datacenter-web`` JSON
endpoint, which it rate-limits by source IP, so every request here routes
through the shared throttled Eastmoney client.

This tool is read-only: it returns the day's board appearances (optionally
narrowed to one A-share ``code``) and, for a specific security, the ranked
top buy/sell brokerage seats.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders import eastmoney_client, hithink_client
from src.agent.tools import BaseTool
from src.tools import tushare_fallbacks
from src.tools._result_paging import fit_records

logger = logging.getLogger(__name__)

# Eastmoney datacenter report endpoint and the two report names this tool reads.
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_APPEARANCE_REPORT = "RPT_DAILYBILLBOARD_DETAILS"
_SEAT_REPORT = "RPT_BILLBOARD_TRADEDETAIL"

# Hithink (同花顺) dragon-tiger endpoint — the project's configured
# first-priority source. It has no per-seat brokerage detail (only
# stock-level rows), so a ``code`` lookup still falls through to Eastmoney's
# seat report even on a successful Hithink appearance fetch.
_HITHINK_PATH = "/api/a-share/special-data/dragon-tiger-list"

# Per-symbol caps so a wide market day never returns an unbounded payload.
_MAX_APPEARANCES = 200
_MAX_SEATS = 30


def _compact_date(date: str) -> str:
    """Normalize a ``YYYY-MM-DD`` (or already-compact) date for the API filter.

    Args:
        date: A trade date such as ``"2024-01-02"`` or ``"20240102"``.

    Returns:
        The date in dashed ``YYYY-MM-DD`` form expected by the datacenter
        ``TRADE_DATE`` filter.

    Raises:
        ValueError: The string is not a recognizable 8-digit / dashed date.
    """
    cleaned = date.strip()
    digits = cleaned.replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"invalid date: {date!r}; expected YYYY-MM-DD")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _bare_code(code: str) -> str:
    """Strip any exchange suffix to the bare numeric A-share code.

    Args:
        code: A symbol such as ``"600519.SH"``, ``"000001.SZ"`` or ``"600519"``.

    Returns:
        The leading numeric code (e.g. ``"600519"``).
    """
    return code.strip().upper().split(".", 1)[0]


def _fetch_report(
    report_name: str, *, filter_expr: str, sort_columns: str, sort_types: str
) -> list[dict[str, Any]]:
    """Pull one page of datacenter rows for a report, tolerating empty results.

    Args:
        report_name: Eastmoney ``reportName`` (e.g. ``RPT_DAILYBILLBOARD_DETAILS``).
        filter_expr: The datacenter ``filter`` predicate string.
        sort_columns: Column name to sort by.
        sort_types: Sort direction (``"1"`` ascending, ``"-1"`` descending).

    Returns:
        The list of row dicts under ``result.data``; empty when none.

    Raises:
        requests.RequestException: Network failure, propagated to the caller.
        requests.HTTPError: Non-2xx response status.
        ValueError: Body is not valid JSON.
    """
    payload = eastmoney_client.get_json(
        _DATACENTER_URL,
        params={
            "reportName": report_name,
            "columns": "ALL",
            "filter": filter_expr,
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "pageNumber": "1",
            "pageSize": "500",
            "source": "WEB",
            "client": "WEB",
        },
    )
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _appearance_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw appearance row to a compact, named record.

    Args:
        raw: One ``RPT_DAILYBILLBOARD_DETAILS`` row.

    Returns:
        A flat dict of the fields a research caller cares about.
    """
    return {
        "code": raw.get("SECURITY_CODE"),
        "name": raw.get("SECURITY_NAME_ABBR"),
        "close": raw.get("CLOSE_PRICE"),
        "change_pct": raw.get("CHANGE_RATE"),
        "net_buy": raw.get("BILLBOARD_NET_AMT"),
        "buy_amount": raw.get("BILLBOARD_BUY_AMT"),
        "sell_amount": raw.get("BILLBOARD_SELL_AMT"),
        "turnover": raw.get("ACCUM_AMOUNT"),
        "reason": raw.get("EXPLANATION"),
    }


def _hithink_stock_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one Hithink ``stock_items`` row to the shared appearance shape.

    Args:
        raw: One element of Hithink's ``dragon-tiger-list`` ``stock_items``.

    Returns:
        A dict matching :func:`_appearance_row`'s field set, so a caller
        cannot tell which source answered from the row shape alone.
    """
    change = raw.get("change")
    return {
        "code": raw.get("ticker"),
        "name": raw.get("name"),
        "close": None,  # Hithink's board row carries no absolute close price.
        "change_pct": change * 100 if isinstance(change, (int, float)) else None,
        "net_buy": raw.get("net_value"),
        "buy_amount": raw.get("buy_value"),
        "sell_amount": raw.get("sell_value"),
        "turnover": raw.get("amount"),
        "reason": raw.get("limit_reason"),
    }


def _fetch_hithink(trade_date: str, code: str | None) -> dict[str, Any]:
    """Fetch the day's dragon-tiger appearances from Hithink (first-priority source).

    Hithink's board endpoint carries no per-seat brokerage detail, so this
    never populates ``seats`` — a ``code`` lookup still needs the Eastmoney
    seat report layered on top by the caller.

    Args:
        trade_date: Dashed ``YYYY-MM-DD`` trade date.
        code: Bare A-share code to filter to, or ``None`` for the full list.

    Returns:
        A dict with ``date``, ``count`` and ``appearances``, matching the
        Eastmoney-path shape (minus ``seats``).

    Raises:
        hithink_client.HithinkUnavailable: No API key configured.
        hithink_client.HithinkAPIError: Non-zero business error code (e.g. a
            non-trading-day date).
        requests.RequestException: Network failure.
    """
    data = hithink_client.get(
        _HITHINK_PATH, params={"board_type": "all", "date": trade_date}
    )
    stock_items = data.get("stock_items") if isinstance(data, dict) else None
    rows = [row for row in (stock_items or []) if isinstance(row, dict)]
    if code:
        rows = [row for row in rows if str(row.get("ticker")) == code]
    appearances = [_hithink_stock_row(row) for row in rows]
    return {
        "date": trade_date,
        "count": len(rows),
        "appearances": appearances,
    }


def _seat_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw seat row to a compact, named record.

    Args:
        raw: One ``RPT_BILLBOARD_TRADEDETAIL`` row.

    Returns:
        A flat dict describing one brokerage seat's buy/sell footprint.
    """
    return {
        "seat": raw.get("OPERATEDEPT_NAME"),
        "side": raw.get("SIDE"),
        "buy": raw.get("BUY"),
        "sell": raw.get("SELL"),
        "net": raw.get("NET"),
        "rank": raw.get("RANK"),
    }


class DragonTigerTool(BaseTool):
    """Query the Eastmoney A-share dragon-tiger (龙虎榜) disclosure board."""

    name = "get_dragon_tiger"
    description = (
        "Fetch the A-share dragon-tiger board (龙虎榜) for a given trade date from "
        "Eastmoney's free datacenter API. Markets: China A-share (SH/SZ). Omit "
        "'code' for the full-market list of every security that appeared on the "
        "board that day; supply 'code' to also get that security's ranked top "
        "buy/sell brokerage seats. Read-only, no auth. "
        'Example: {"date": "2024-01-02", "code": "600519.SH"}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Trade date in YYYY-MM-DD format (e.g. 2024-01-02).",
            },
            "code": {
                "type": "string",
                "description": (
                    "Optional A-share symbol or bare code (e.g. '600519.SH' or "
                    "'600519'). Omit to list the entire market's board for the "
                    "date; supply it to also fetch that security's seat detail."
                ),
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Index of the first appearance to return, for a full-"
                    "market list that overflowed the character budget in one "
                    "call — read paging.next_offset in the response and call "
                    "again with that value to continue. Defaults to 0."
                ),
            },
        },
        "required": ["date"],
    }
    # Each call can carry a different date and/or code (full-market list vs.
    # one security's seat detail) and should return a different answer; the
    # default non-repeatable dedup (keyed on tool name alone, ignoring args)
    # was silently blocking every per-code follow-up call after the first
    # successful no-code call in the same session.
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Fetch dragon-tiger appearances (and seats when ``code`` is given).

        Args:
            **kwargs: ``date`` (required, YYYY-MM-DD) and optional ``code``.

        Returns:
            A JSON string envelope. On success:
            ``{"ok": true, "market": "a_share", "source": "eastmoney",
            "data": {...}}``. On failure: ``{"ok": false, "error": "..."}``.
        """
        date_arg = kwargs.get("date")
        if not isinstance(date_arg, str) or not date_arg.strip():
            return self._error("missing required parameter: date")

        try:
            trade_date = _compact_date(date_arg)
        except ValueError as exc:
            return self._error(str(exc))

        try:
            offset = max(int(kwargs.get("offset") or 0), 0)
        except (TypeError, ValueError):
            return self._error("offset must be an integer")

        code_arg = kwargs.get("code")
        code = _bare_code(code_arg) if isinstance(code_arg, str) and code_arg.strip() else None

        # Priority order: Hithink (同花顺, first-priority per project config)
        # -> Eastmoney (free) -> Tushare (optional paid fallback). Any failure
        # (unconfigured key, network error, rate limit, business error) falls
        # through to the next source rather than surfacing to the caller.
        if hithink_client.is_configured():
            try:
                data = self._collect_hithink(trade_date, code)
            except Exception as exc:  # noqa: BLE001 - fall through to eastmoney
                logger.info(
                    "hithink dragon-tiger fetch failed for %s/%s: %s; falling back to eastmoney",
                    trade_date, code, exc,
                )
            else:
                return self._page_appearances(data, offset, source="hithink")

        try:
            data = self._collect(trade_date, code)
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure as an envelope
            logger.warning("dragon-tiger fetch failed for %s/%s: %s", trade_date, code, exc)
            try:
                data = tushare_fallbacks.fetch_dragon_tiger(trade_date, code)
            except Exception as fallback_exc:  # noqa: BLE001 - return both provider failures
                return self._error(
                    "eastmoney dragon-tiger fetch failed: "
                    f"{exc}; tushare fallback failed: {fallback_exc}"
                )
            return self._page_appearances(
                data, offset, source="tushare",
                warnings=[f"eastmoney failed ({exc}); used tushare fallback"],
            )

        return self._page_appearances(data, offset, source="eastmoney")

    @staticmethod
    def _page_appearances(
        data: dict[str, Any], offset: int, *, source: str, warnings: list[str] | None = None
    ) -> str:
        """Page ``data['appearances']`` so a busy trading day's full-market list
        (56+ rows observed live, several times the per-tool result budget)
        degrades to a self-describing partial page rather than a silent hard
        cut mid-JSON that the model cannot distinguish from a complete answer.

        ``seats`` (already capped at 30 rows) and every other top-level key
        ride along unpaged on every page of the response.

        Args:
            data: The collected dragon-tiger payload (``date``/``count``/
                ``appearances`` and, when ``code`` was given, ``code``/``seats``).
            offset: Index of the first appearance to return.
            source: Provider label for the envelope's ``source`` field.
            warnings: Optional warnings list (e.g. a fallback notice) carried
                through unchanged on every page.

        Returns:
            The serialized envelope string, paged to fit the result budget.
        """
        appearances = data.get("appearances") or []
        rest = {k: v for k, v in data.items() if k != "appearances"}

        def _build(page: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
            envelope: dict[str, Any] = {
                "ok": True,
                "market": "a_share",
                "source": source,
                "paging": meta,
                "data": {**rest, "appearances": page},
            }
            if warnings:
                envelope["warnings"] = warnings
            return envelope

        return fit_records(appearances, offset, _build)

    def _collect_hithink(self, trade_date: str, code: str | None) -> dict[str, Any]:
        """Fetch appearances from Hithink and, when ``code`` is given, seats from Eastmoney.

        Hithink has no per-seat brokerage detail, so a ``code`` lookup layers
        Eastmoney's seat report on top of the Hithink-sourced appearances. A
        seat-fetch failure degrades to an empty seat list rather than
        discarding the (already successful) Hithink appearances.

        Args:
            trade_date: Dashed ``YYYY-MM-DD`` trade date.
            code: Bare A-share code, or ``None`` for the full-market list.

        Returns:
            A dict with ``date``, ``count``, ``appearances`` and, when ``code``
            is given, ``code`` plus ``seats``.

        Raises:
            hithink_client.HithinkUnavailable: No API key configured.
            hithink_client.HithinkAPIError: Non-zero business error code.
            requests.RequestException: Network failure.
        """
        data = _fetch_hithink(trade_date, code)
        if code:
            data["code"] = code
            try:
                seats_raw = _fetch_report(
                    _SEAT_REPORT,
                    filter_expr=f"(TRADE_DATE='{trade_date}')(SECURITY_CODE=\"{code}\")",
                    sort_columns="NET",
                    sort_types="-1",
                )
                data["seats"] = [_seat_row(r) for r in seats_raw[:_MAX_SEATS]]
            except Exception as exc:  # noqa: BLE001 - seats are a bonus, not the primary answer
                logger.info("eastmoney seat fetch failed for %s/%s: %s", trade_date, code, exc)
                data["seats"] = []
        return data

    def _collect(self, trade_date: str, code: str | None) -> dict[str, Any]:
        """Assemble the appearances and (optionally) seat detail payload.

        Args:
            trade_date: Dashed ``YYYY-MM-DD`` trade date.
            code: Bare A-share code, or ``None`` for the full-market list.

        Returns:
            A dict with ``date``, capped ``appearances`` and, when ``code`` is
            given, capped ``seats`` plus the seat ``code``.
        """
        appear_filter = f"(TRADE_DATE='{trade_date}')"
        if code:
            appear_filter += f"(SECURITY_CODE=\"{code}\")"
        appearances_raw = _fetch_report(
            _APPEARANCE_REPORT,
            filter_expr=appear_filter,
            sort_columns="BILLBOARD_NET_AMT",
            sort_types="-1",
        )
        appearances = [_appearance_row(r) for r in appearances_raw[:_MAX_APPEARANCES]]

        data: dict[str, Any] = {
            "date": trade_date,
            "count": len(appearances_raw),
            "appearances": appearances,
        }
        if code:
            data["code"] = code
            seats_raw = _fetch_report(
                _SEAT_REPORT,
                filter_expr=f"(TRADE_DATE='{trade_date}')(SECURITY_CODE=\"{code}\")",
                sort_columns="NET",
                sort_types="-1",
            )
            data["seats"] = [_seat_row(r) for r in seats_raw[:_MAX_SEATS]]
        return data

    @staticmethod
    def _error(message: str) -> str:
        """Render a failure envelope as a JSON string.

        Args:
            message: Human-readable error text.

        Returns:
            ``{"ok": false, "error": message}`` as a JSON string.
        """
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

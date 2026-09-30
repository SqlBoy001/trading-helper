"""Append-only, versioned decision evidence stored in local SQLite.

Source attribution is caller-provided, not authenticated human approval.
Records never update positions or execute orders. Reads return original records
and explicit amendments, not a model-generated reconstruction of history.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


KINDS = ("plan", "intent", "reminder", "decision", "execution", "review", "correction", "snapshot", "account_activity")
STATUSES = ("plan_conflict", "plan_consistent", "needs_information", "thesis_review")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    request_key TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    parent_id TEXT,
    external_key TEXT UNIQUE,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_journal ON events(journal_id, seq);
CREATE INDEX IF NOT EXISTS events_parent ON events(parent_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'decision events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'decision events are append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string with timezone")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO string with timezone") from exc
    if stamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("record must contain finite JSON values") from exc


def _validate_data(kind: str, data: dict) -> None:
    required = {
        "plan": ("thesis",), "intent": ("action", "reason"),
        "reminder": ("status", "observation", "next_step"),
        "decision": ("action", "reason"), "execution": ("side", "symbol", "currency"),
        "review": ("assessment",), "correction": ("reason",), "snapshot": ("notes",), "account_activity": ("activity", "currency"),
    }
    for key in required[kind]:
        _text(data.get(key), key)
    if kind == "plan":
        rules = data.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("plan rules must be a non-empty list")
        for rule in rules:
            _text(rule, "rule")
        if type(data.get("confirmed")) is not bool:
            raise ValueError("plan confirmed must be an explicit boolean")
        if data["confirmed"]:
            _text(data.get("confirmation_quote"), "confirmation_quote")
    if kind in {"intent", "decision"} and data["action"] not in {
        "buy", "add", "reduce", "sell", "watch", "defer", "cancel",
    }:
        raise ValueError("unknown intent/decision action")
    if kind == "reminder":
        if data["status"] not in STATUSES:
            raise ValueError("unknown reminder status")
        if not isinstance(data.get("evidence_ids"), list) or not data["evidence_ids"]:
            raise ValueError("reminder requires evidence_ids")
    if kind == "execution":
        if data["side"] not in {"buy", "sell"}:
            raise ValueError("execution side must be buy or sell")
        for key in ("quantity", "price"):
            val = data.get(key)
            if type(val) not in (int, float) or not math.isfinite(val) or val <= 0:
                raise ValueError(f"{key} must be a positive finite number")
        if ("external_id" in data) != ("external_source" in data):
            raise ValueError("external_id and external_source must be supplied together")
        for key in ("external_id", "external_source"):
            if key in data:
                _text(data[key], key)
    if kind == "account_activity":
        amount = data.get("cash_change")
        if type(amount) not in (int, float) or not math.isfinite(amount):
            raise ValueError("cash_change must be finite")
        for key in ("external_source", "external_id"):
            _text(data.get(key), key)
    if kind == "snapshot":
        if type(data.get("complete")) is not bool:
            raise ValueError("snapshot complete must be an explicit boolean")
        if not isinstance(data.get("positions"), list):
            raise ValueError("snapshot positions must be a list")
        seen = set()
        for position in data["positions"]:
            if not isinstance(position, dict):
                raise ValueError("position must be an object")
            symbol = _text(position.get("symbol"), "symbol")
            if symbol in seen:
                raise ValueError("duplicate position symbol")
            seen.add(symbol)
            quantity = position.get("quantity")
            if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity < 0:
                raise ValueError("position quantity must be nonnegative")
        cash = data.get("cash")
        if cash is not None and (type(cash) not in (int, float) or not math.isfinite(cash) or cash < 0):
            raise ValueError("cash must be nonnegative or unknown")
    if kind == "correction" and not isinstance(data.get("replacement"), dict):
        raise ValueError("correction requires full replacement data")


class DecisionJournal:
    """Durable local event log, lazily opened; safe to instantiate on discovery."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from src.config.paths import get_runtime_root
            configured = os.environ.get("VIBE_TRADING_DECISION_JOURNAL_PATH", "").strip()
            path = Path(configured).expanduser() if configured else get_runtime_root() / "decision_journal.sqlite3"
            if configured and not path.is_absolute():
                raise ValueError("VIBE_TRADING_DECISION_JOURNAL_PATH must be absolute")
        self.path = path

    @contextmanager
    def _connect(self, *, write: bool = False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            conn = sqlite3.connect(self.path, timeout=10)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.executescript(_SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get(conn, event_id: str) -> dict:
        row = conn.execute("SELECT body FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("event not found")
        return json.loads(row["body"])

    @staticmethod
    def _active_plan(conn, journal_id: str, at: str) -> dict | None:
        rows = conn.execute(
            "SELECT body FROM events WHERE journal_id=? AND kind='plan' "
            "AND recorded_at<=? ORDER BY seq DESC", (journal_id, at),
        )
        for row in rows:
            event = json.loads(row["body"])
            if event["data"]["confirmed"] and event["effective_at"] <= at:
                return event
        return None

    def append(
        self, *, journal_id: str, kind: str, request_key: str, source: str,
        source_ref: str, data: dict, parent_id: str | None = None,
        occurred_at: str | None = None, effective_at: str | None = None,
    ) -> dict:
        """Append once; retries must reuse request_key and the exact input.

        Plan revisions take effect no earlier than their recording time.
        Historical imports can record executions, but cannot manufacture a
        plan that was verifiably available before it was recorded here.
        """
        for name, value in (("journal_id", journal_id), ("request_key", request_key),
                            ("source_ref", source_ref)):
            _text(value, name)
        if kind not in KINDS or source not in {"user", "assistant", "import"}:
            raise ValueError("unknown kind or source")
        if parent_id is not None:
            _text(parent_id, "parent_id")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        _validate_data(kind, data)
        if source == "assistant" and (kind in {"execution", "decision", "snapshot", "account_activity"} or
                                       (kind == "plan" and data["confirmed"])):
            raise ValueError("assistant inference cannot confirm a plan, decision or execution")
        if kind == "execution" and source == "import" and "external_id" not in data:
            raise ValueError("imported execution requires external_id and external_source")
        if kind != "plan" and effective_at is not None:
            raise ValueError("effective_at only applies to plans")
        request = dict(journal_id=journal_id, kind=kind, source=source, source_ref=source_ref,
                       data=data, parent_id=parent_id, occurred_at=occurred_at, effective_at=effective_at)
        request_json = _json(request)
        if len(request_json.encode("utf-8")) > 131072:
            raise ValueError("event exceeds 128 KiB; split records instead of truncating")
        with self._connect(write=True) as conn:
            prior = conn.execute("SELECT request_json, body FROM events WHERE request_key=?", (request_key,)).fetchone()
            if prior:
                if prior["request_json"] != request_json:
                    raise ValueError("request_key already used for different content")
                return {"event": json.loads(prior["body"]), "replayed": True}
            now = _now()
            occurred = _timestamp(occurred_at) if occurred_at is not None else now
            if occurred > now:
                raise ValueError("occurred_at cannot be in the future")
            parent = self._get(conn, parent_id) if parent_id else None
            if parent and parent["journal_id"] != journal_id:
                raise ValueError("parent belongs to another journal")
            if parent and parent["occurred_at"] > occurred:
                raise ValueError("event cannot precede its parent")
            event = dict(id="dj_" + uuid4().hex, journal_id=journal_id, kind=kind,
                         recorded_at=now, occurred_at=occurred, source=source, source_ref=source_ref,
                         parent_id=parent_id, data=data)
            if kind == "plan":
                if occurred_at is not None:
                    raise ValueError("plans are recorded now; do not backdate confirmation")
                latest = conn.execute(
                    "SELECT id FROM events WHERE journal_id=? AND kind='plan' ORDER BY seq DESC LIMIT 1",
                    (journal_id,),
                ).fetchone()
                if (latest["id"] if latest else None) != parent_id:
                    raise ValueError("plan revision must reference the latest plan version")
                effective = _timestamp(effective_at) if effective_at is not None else now
                if effective < now:
                    raise ValueError("plan effective_at cannot predate recording")
                event.update(effective_at=effective, version=parent["version"] + 1 if parent else 1)
            elif kind == "intent":
                plan = self._active_plan(conn, journal_id, occurred)
                if parent_id and (not plan or plan["id"] != parent_id):
                    raise ValueError("intent must reference the confirmed plan active at its time")
                event["parent_id"] = plan["id"] if plan else None
            elif kind == "correction":
                if not parent or parent["kind"] == "plan":
                    raise ValueError("correction needs an event; revise plans with a new plan version")
                if conn.execute("SELECT 1 FROM events WHERE kind='correction' AND parent_id=?", (parent_id,)).fetchone():
                    raise ValueError("correction must reference the latest amendment")
                base = parent
                while base["kind"] == "correction":
                    base = self._get(conn, base["parent_id"])
                if source == "assistant" and base["source"] != "assistant":
                    raise ValueError("assistant inference cannot correct user/import evidence")
                _validate_data(base["kind"], data["replacement"])
                if base["kind"] in {"execution", "account_activity"}:
                    for key in ("external_source", "external_id"):
                        if data["replacement"].get(key) != base["data"].get(key):
                            raise ValueError("corrections cannot change external execution identity")
                event["corrects_kind"] = base["kind"]
            elif kind in {"snapshot", "account_activity"}:
                if parent:
                    raise ValueError("snapshot is independent of trading intents")
            elif kind == "execution" and parent is None:
                event["intent_status"] = "not_recorded"
            elif not parent or parent["kind"] != "intent":
                raise ValueError("reminder/decision/execution/review must reference an intent")
            reminder_data = data if kind == "reminder" else (
                data["replacement"] if event.get("corrects_kind") == "reminder" else None
            )
            if reminder_data is not None:
                for ref in reminder_data["evidence_ids"]:
                    evidence = self._get(conn, _text(ref, "evidence_id"))
                    if evidence["journal_id"] != journal_id or evidence["recorded_at"] > occurred:
                        raise ValueError("evidence must belong to this journal and be known at reminder time")
            external_key = None
            if kind in {"execution", "account_activity"} and "external_id" in data:
                external_key = hashlib.sha256(_json([
                    journal_id, data["external_source"], data["external_id"],
                ]).encode()).hexdigest()
                if conn.execute("SELECT 1 FROM events WHERE external_key=?", (external_key,)).fetchone():
                    raise ValueError("duplicate external execution; use original request_key or append a correction")
            cursor = conn.execute(
                "INSERT INTO events(id,request_key,request_json,journal_id,kind,occurred_at,recorded_at,parent_id,external_key,body) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event["id"], request_key, request_json, journal_id, kind, occurred, now,
                 event["parent_id"], external_key, _json(event)),
            )
            return {"event": event, "sequence": cursor.lastrowid, "replayed": False}

    def get(self, event_id: str) -> dict:
        """Read the entire immutable event plus its linear correction history."""
        _text(event_id, "event_id")
        if not self.path.exists():
            raise ValueError("event not found")
        with self._connect() as conn:
            event = self._get(conn, event_id)
            corrections = []
            target = event_id
            while True:
                row = conn.execute(
                    "SELECT body FROM events WHERE kind='correction' AND parent_id=?", (target,),
                ).fetchone()
                if row is None:
                    break
                amendment = json.loads(row["body"])
                corrections.append(amendment)
                target = amendment["id"]
            return {"event": event, "corrections": corrections}

    def history(self, journal_id: str, *, after: int = 0, limit: int = 20, as_of: str | None = None) -> dict:
        """Page full records by sequence; as_of uses recording/knowledge time."""
        _text(journal_id, "journal_id")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("after must be nonnegative and limit must be 1..100")
        cutoff = _timestamp(as_of) if as_of is not None else _now()
        if not self.path.exists():
            return {"events": [], "next_after": None, "active_plan": None, "as_of": cutoff}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq,body FROM events WHERE journal_id=? AND seq>? AND recorded_at<=? "
                "ORDER BY seq LIMIT ?", (journal_id, after, cutoff, limit + 1),
            ).fetchall()
            return {"events": [dict(sequence=r["seq"], **json.loads(r["body"])) for r in rows[:limit]],
                    "next_after": rows[limit - 1]["seq"] if len(rows) > limit else None,
                    "active_plan": self._active_plan(conn, journal_id, cutoff), "as_of": cutoff}


    def latest_snapshot(self, journal_id: str, *, as_of: str | None = None) -> dict:
        """Read the latest observed snapshot and only corrections known at cutoff."""
        _text(journal_id, "journal_id")
        cutoff = _timestamp(as_of) if as_of is not None else _now()
        result = {"snapshot": None, "as_of": cutoff, "status": "missing"}
        if not self.path.exists():
            return result
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body FROM events WHERE journal_id=? AND kind='snapshot' "
                "AND recorded_at<=? AND occurred_at<=? ORDER BY occurred_at DESC,seq DESC LIMIT 1",
                (journal_id, cutoff, cutoff),
            ).fetchone()
            if row is None:
                return result
            event = json.loads(row["body"])
            data = event["data"]
            corrections = []
            target = event["id"]
            while True:
                row = conn.execute(
                    "SELECT body FROM events WHERE kind='correction' AND parent_id=? AND recorded_at<=?",
                    (target, cutoff),
                ).fetchone()
                if row is None:
                    break
                correction = json.loads(row["body"])
                corrections.append(correction)
                data = correction["data"]["replacement"]
                target = correction["id"]
            return {"status": "available" if data.get("complete") else "incomplete", "as_of": cutoff,
                    "snapshot": event, "corrections": corrections, "effective_data": data,
                    "warning": "Snapshot is observed holdings, not live positions; compare occurred_at with the analysis date. Never infer missing executions."}

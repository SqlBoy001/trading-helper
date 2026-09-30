"""Agent tools for decision evidence; independent of orders and holdings."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.agent.tools import BaseTool
from src.decision_journal import DecisionJournal
from src.decision_journal.store import KINDS


class RecordDecisionEventTool(BaseTool):
    """Append a sourced decision event using a retry-stable request key."""

    name = "record_decision_event"
    is_readonly = False
    repeatable = True
    description = (
        "Append a local plan, intent, reminder, decision, reported execution, review, snapshot, account_activity or correction. "
        "Never executes trades or updates portfolio balances. Preserve the user's original words in source_ref; "
        "source attribution is caller-reported, not independent verification. Do not mark a plan confirmed "
        "without explicit user confirmation. source=assistant cannot confirm plans or record decisions/executions/snapshots/account_activity. "
        "Reuse request_key on retries. Read exact plan IDs in full with read_decision_journal before advising. "
        "A plan needs thesis, rules (string list), confirmed (bool), and confirmation_quote when confirmed. "
        "Intent/decision need action and reason; reminder needs status, observation, next_step, evidence_ids; "
        "execution needs symbol, side, quantity, price, currency (imports also external_source/external_id); "
        "snapshot needs positions (symbol/quantity list), cash (number or null), complete (bool), notes; "
        "account_activity needs activity, currency, cash_change, external_source/external_id; "
        "review needs assessment; correction needs reason and full replacement data. "
        "Revisions reference the latest plan; reminders/decisions/reviews reference an intent; executions may omit an unknown intent; snapshots have no parent; corrections reference the latest amendment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "journal_id": {"type": "string", "description": "Stable record group, e.g. personal:600519.SH"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "request_key": {"type": "string", "description": "Unique logical write ID; reuse unchanged on retries"},
            "source": {"type": "string", "enum": ["user", "assistant", "import"]},
            "source_ref": {"type": "string", "description": "User's quote/message reference or import provenance"},
            "data": {"type": "object", "description": "Kind-specific data; do not include secrets"},
            "parent_id": {"type": "string", "description": "Previous plan, intent, or correction target ID"},
            "occurred_at": {"type": "string", "description": "ISO timestamp with zone; omit for plans"},
            "effective_at": {"type": "string", "description": "Plan only, now or future; cannot backdate"},
        },
        "required": ["journal_id", "kind", "request_key", "source", "source_ref", "data"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> str:
        """Append through the same validation used by the local CLI."""
        try:
            result = DecisionJournal().append(**kwargs)
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class ReadDecisionJournalTool(BaseTool):
    """Read exact full events or a paginated, point-in-time journal."""

    name = "read_decision_journal"
    repeatable = True
    description = (
        "Read full decision records without memory-snippet truncation. Supply event_id OR journal_id. "
        "An exact read returns the original and its correction chain; inspect corrections before relying on a value. "
        "Journal history returns full records, active confirmed plan, and next_after pagination. "
        "as_of filters by when records became known here, not an imported trade's earlier occurrence. "
        "Intents reference their original plan; newer plans never rewrite that link. Missing data is not evidence of safety."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "journal_id": {"type": "string"},
            "after": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "as_of": {"type": "string", "description": "History only: knowledge cutoff with timezone"},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> str:
        """Read only; an empty journal does not create a database."""
        try:
            if set(kwargs) - set(self.parameters["properties"]):
                raise ValueError("unknown read argument")
            event_id, journal_id = kwargs.get("event_id"), kwargs.get("journal_id")
            if bool(event_id) == bool(journal_id):
                raise ValueError("supply exactly one of event_id or journal_id")
            journal = DecisionJournal()
            if event_id:
                if any(key in kwargs for key in ("after", "limit", "as_of")):
                    raise ValueError("pagination/as_of apply only to journal history")
                result = journal.get(event_id)
            else:
                result = journal.history(**kwargs)
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class ReadCurrentHoldingsTool(BaseTool):
    """Read the shared confirmed-source snapshot without replaying every trade."""

    name = "read_current_holdings"
    repeatable = True
    description = (
        "Read latest recorded holdings snapshot from the shared personal decision journal, "
        "including original source, observation time, corrections and effective_data. "
        "Prefer this over current_portfolio_holdings memory. Missing/incomplete/stale data "
        "is not an empty account. Do not infer today's executions from position changes. "
        "Send only securities codes to market providers, never quantities, cash or trade records."
    )
    parameters = {"type": "object", "properties": {
        "journal_id": {"type": "string"},
        "as_of": {"type": "string", "description": "Knowledge cutoff with timezone"},
    }, "required": ["journal_id"], "additionalProperties": False}

    def execute(self, **kwargs: Any) -> str:
        try:
            return json.dumps(DecisionJournal().latest_snapshot(**kwargs), ensure_ascii=False)
        except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

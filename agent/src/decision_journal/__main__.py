"""No-model local entry point: python -m src.decision_journal --help."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .store import DecisionJournal


def main(argv: list[str] | None = None) -> int:
    """Read a JSON event from disk/stdin, or inspect complete persisted records."""
    parser = argparse.ArgumentParser(description="Local decision journal; never sends orders or calls a model")
    commands = parser.add_subparsers(dest="command", required=True)
    append = commands.add_parser("append", help="Append an event JSON object (use - for stdin)")
    append.add_argument("file")
    get = commands.add_parser("get", help="Read full event and corrections")
    get.add_argument("event_id")
    history = commands.add_parser("history", help="Read a journal, including active plan")
    history.add_argument("journal_id")
    history.add_argument("--after", type=int, default=0)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--as-of")
    args = parser.parse_args(argv)
    try:
        journal = DecisionJournal()
        if args.command == "append":
            import sys
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("event input must be an object")
            result = journal.append(**record)
        elif args.command == "get":
            result = journal.get(args.event_id)
        else:
            result = journal.history(args.journal_id, after=args.after, limit=args.limit, as_of=args.as_of)
        print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

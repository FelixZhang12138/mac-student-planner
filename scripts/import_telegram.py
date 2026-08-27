#!/usr/bin/env python3
"""Normalize Telegram Desktop JSON exports into provenance-preserving records."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(flatten_text(part) for part in value)
    if isinstance(value, dict):
        return flatten_text(value.get("text", ""))
    return ""


def chats_from_export(raw: Any) -> Iterable[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("messages"), list):
        yield raw
    if isinstance(raw, dict):
        chats = raw.get("chats")
        if isinstance(chats, dict):
            chats = chats.get("list")
        if isinstance(chats, list):
            for chat in chats:
                if isinstance(chat, dict) and isinstance(chat.get("messages"), list):
                    yield chat


def message_date(message: dict[str, Any]) -> date | None:
    value = str(message.get("date", ""))
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def normalize(raw: Any, since: date | None, until: date | None, chat_filter: str | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chat in chats_from_export(raw):
        label = str(chat.get("name") or chat.get("title") or "Telegram chat")
        if chat_filter and chat_filter.casefold() not in label.casefold():
            continue
        chat_id = str(chat.get("id", label))
        for message in chat["messages"]:
            if not isinstance(message, dict) or message.get("type") not in (None, "message"):
                continue
            observed_date = message_date(message)
            if not observed_date:
                continue
            if since and observed_date < since:
                continue
            if until and observed_date > until:
                continue
            text = flatten_text(message.get("text", "")).strip()
            if not text:
                continue
            message_id = str(message.get("id", "unknown"))
            records.append(
                {
                    "record_type": "message",
                    "text": text,
                    "author": str(message.get("from") or message.get("actor") or ""),
                    "observed_at": message.get("date"),
                    "source": {
                        "provider": "telegram",
                        "label": label,
                        "external_id": f"{chat_id}:{message_id}",
                    },
                }
            )
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--until", type=date.fromisoformat)
    parser.add_argument("--chat", help="Case-insensitive substring filter for a chat name")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.since and args.until and args.until < args.since:
        print(json.dumps({"ok": False, "error": "--until must not be earlier than --since"}), file=sys.stderr)
        return 1
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
        records = normalize(raw, args.since, args.until, args.chat)
        payload = {"schema_version": 1, "source_file": str(args.input), "records": records}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Normalize VEVENT and VTODO records from an iCalendar export into JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATE_RE = re.compile(r"^\d{8}$")
DATETIME_RE = re.compile(r"^(\d{8})T(\d{6})(Z?)$")


def default_timezone() -> str:
    return os.environ.get("MAC_STUDENT_PLANNER_TIMEZONE", "").strip() or "UTC"


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def decode_text(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            following = value[index + 1]
            output.append("\n" if following in ("n", "N") else following)
            index += 2
        else:
            output.append(value[index])
            index += 1
    return "".join(output).strip()


def parse_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    pieces = head.split(";")
    name = pieces[0].upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, param_value = piece.split("=", 1)
            params[key.upper()] = param_value.strip('"')
    return name, params, value


def parse_temporal(value: str, params: dict[str, str], default_timezone: str) -> tuple[str, bool]:
    if params.get("VALUE", "").upper() == "DATE" or DATE_RE.match(value):
        parsed_date = datetime.strptime(value[:8], "%Y%m%d").date()
        zone = ZoneInfo(default_timezone)
        return datetime.combine(parsed_date, time(9, 0), tzinfo=zone).isoformat(), True

    match = DATETIME_RE.match(value)
    if not match:
        raise ValueError(f"unsupported iCalendar date: {value}")
    parsed = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    if match.group(3) == "Z":
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    else:
        timezone_name = params.get("TZID", default_timezone)
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            parsed = parsed.replace(tzinfo=ZoneInfo(default_timezone))
    return parsed.isoformat(), False


def first(properties: dict[str, list[tuple[dict[str, str], str]]], name: str) -> tuple[dict[str, str], str] | None:
    values = properties.get(name, [])
    return values[0] if values else None


def temporal(properties: dict[str, list[tuple[dict[str, str], str]]], name: str, timezone: str) -> tuple[str | None, bool]:
    item = first(properties, name)
    if not item:
        return None, False
    try:
        return parse_temporal(item[1], item[0], timezone)
    except ValueError:
        return None, False


def normalize_component(
    kind: str,
    properties: dict[str, list[tuple[dict[str, str], str]]],
    provider: str,
    source_label: str,
    timezone: str,
) -> dict[str, Any]:
    def text(name: str) -> str:
        item = first(properties, name)
        return decode_text(item[1]) if item else ""

    uid = text("UID")
    title = text("SUMMARY") or "Untitled calendar item"
    fallback = hashlib.sha256(f"{kind}|{title}|{text('DTSTART')}|{text('DUE')}".encode()).hexdigest()[:20]
    external_id = uid or fallback
    start_at, start_all_day = temporal(properties, "DTSTART", timezone)
    end_at, _ = temporal(properties, "DTEND", timezone)
    due_at, due_all_day = temporal(properties, "DUE", timezone)
    url = text("URL")
    source: dict[str, str] = {
        "provider": provider,
        "label": source_label,
        "external_id": external_id,
    }
    if url:
        source["url"] = url
    record: dict[str, Any] = {
        "record_type": "task" if kind == "VTODO" else "event",
        "title": title,
        "description": text("DESCRIPTION"),
        "location": text("LOCATION"),
        "start_at": start_at,
        "end_at": end_at,
        "due_at": due_at,
        "all_day": due_all_day if due_at else start_all_day,
        "status": text("STATUS"),
        "recurrence_rule": text("RRULE"),
        "source": source,
    }
    return record


def parse_ics(text: str, provider: str, source_label: str, timezone: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    active_kind: str | None = None
    properties: dict[str, list[tuple[dict[str, str], str]]] = {}
    for line in unfold(text):
        parsed = parse_property(line)
        if not parsed:
            continue
        name, params, value = parsed
        upper_value = value.upper()
        if name == "BEGIN" and upper_value in {"VEVENT", "VTODO"}:
            active_kind = upper_value
            properties = {}
            continue
        if name == "END" and active_kind and upper_value == active_kind:
            records.append(normalize_component(active_kind, properties, provider, source_label, timezone))
            active_kind = None
            properties = {}
            continue
        if active_kind:
            properties.setdefault(name, []).append((params, value))
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--provider", default="calendar")
    parser.add_argument("--source-label")
    parser.add_argument("--timezone", default=default_timezone())
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ZoneInfo(args.timezone)
        records = parse_ics(
            args.input.read_text(encoding="utf-8-sig"),
            provider=args.provider.strip().lower(),
            source_label=args.source_label or args.input.name,
            timezone=args.timezone,
        )
        payload = {
            "schema_version": 1,
            "source_file": str(args.input),
            "timezone": args.timezone,
            "records": records,
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, UnicodeError, ValueError, ZoneInfoNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

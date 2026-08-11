#!/usr/bin/env python3
"""Read, synchronize, and roll over Apple Reminders without third-party packages."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


MARKER_PREFIX = "[mac-student-planner:id="
VALID_PRIORITIES = {0, 1, 5, 9}


JXA = r'''
const input = __PAYLOAD__;
const app = Application("Reminders");

function value(getter, fallback) {
  try {
    const result = getter();
    return result === undefined || result === null ? fallback : result;
  } catch (_) {
    return fallback;
  }
}

function findList(name) {
  const lists = app.lists();
  for (let i = 0; i < lists.length; i += 1) {
    if (value(() => lists[i].name(), "") === name) return lists[i];
  }
  return null;
}

function marker(id) {
  return "[mac-student-planner:id=" + id + "]";
}

function managedId(body) {
  const match = String(body || "").match(/\[mac-student-planner:id=([^\]]+)\]/);
  return match ? match[1] : null;
}

function sourceLine(task) {
  const source = task.source || {};
  const parts = [source.provider || "manual"];
  if (source.label) parts.push(source.label);
  if (source.external_id) parts.push(source.external_id);
  if (source.url) parts.push(source.url);
  return "Source: " + parts.join(" | ");
}

function taskBody(task) {
  const lines = [marker(task.id), sourceLine(task)];
  if (task.notes) lines.push(String(task.notes));
  return lines.join("\n");
}

function iso(valueToFormat) {
  if (!valueToFormat) return null;
  try { return new Date(valueToFormat).toISOString(); } catch (_) { return null; }
}

function listMode() {
  const list = findList(input.list);
  if (!list) return {list: input.list, reminders: [], missing_list: true};
  const cutoff = input.due_on_or_before ? new Date(input.due_on_or_before) : null;
  const reminders = [];
  const items = list.reminders();
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    const completed = Boolean(value(() => item.completed(), false));
    if (!input.include_completed && completed) continue;
    const due = value(() => item.dueDate(), null);
    if (cutoff && (!due || new Date(due) > cutoff)) continue;
    const body = String(value(() => item.body(), ""));
    reminders.push({
      apple_id: String(value(() => item.id(), "")),
      title: String(value(() => item.name(), "")),
      notes: body,
      due_at: iso(due),
      completed: completed,
      priority: Number(value(() => item.priority(), 0)),
      managed_id: managedId(body)
    });
  }
  return {list: input.list, reminders: reminders, missing_list: false};
}

function syncMode() {
  let list = findList(input.list);
  const listMissing = !list;
  if (!list && !input.dry_run) {
    list = app.List({name: input.list});
    app.lists.push(list);
  }

  const existing = {};
  if (list) {
    const items = list.reminders();
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      const body = String(value(() => item.body(), ""));
      const id = managedId(body);
      if (id && !existing[id]) existing[id] = item;
    }
  }

  const result = {list: input.list, created: 0, updated: 0, skipped: 0, dry_run: input.dry_run, list_would_be_created: listMissing};
  for (let i = 0; i < input.tasks.length; i += 1) {
    const task = input.tasks[i];
    const due = new Date(task.due_at);
    if (Number.isNaN(due.getTime())) {
      result.skipped += 1;
      continue;
    }
    const body = taskBody(task);
    const current = existing[task.id];
    if (current) {
      result.updated += 1;
      if (!input.dry_run) {
        current.name = task.title;
        current.body = body;
        current.dueDate = due;
        current.priority = task.priority;
      }
    } else {
      result.created += 1;
      if (!input.dry_run) {
        const reminder = app.Reminder({
          name: task.title,
          body: body,
          dueDate: due,
          priority: task.priority
        });
        list.reminders.push(reminder);
      }
    }
  }
  return result;
}

function rolloverMode() {
  const list = findList(input.list);
  const result = {list: input.list, moved: 0, skipped_unmanaged: 0, dry_run: input.dry_run, missing_list: !list, items: []};
  if (!list) return result;

  const cutoff = new Date(input.cutoff);
  const target = input.target_date.split("-").map(Number);
  const items = list.reminders();
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (Boolean(value(() => item.completed(), false))) continue;
    const dueValue = value(() => item.dueDate(), null);
    if (!dueValue) continue;
    const due = new Date(dueValue);
    if (due > cutoff) continue;

    const body = String(value(() => item.body(), ""));
    const id = managedId(body);
    if (!id && !input.include_unmanaged) {
      result.skipped_unmanaged += 1;
      continue;
    }

    const nextDue = new Date(due);
    nextDue.setFullYear(target[0], target[1] - 1, target[2]);
    const title = String(value(() => item.name(), ""));
    result.moved += 1;
    result.items.push({title: title, from: iso(due), to: iso(nextDue), managed_id: id});
    if (!input.dry_run) {
      item.dueDate = nextDue;
      if (id) {
        const rolloverNote = "Rolled over: " + input.source_date + " -> " + input.target_date;
        if (body.indexOf(rolloverNote) === -1) item.body = body + "\n" + rolloverNote;
      }
    }
  }
  return result;
}

let output;
if (input.mode === "list") output = listMode();
else if (input.mode === "sync") output = syncMode();
else if (input.mode === "rollover") output = rolloverMode();
else throw new Error("Unsupported mode: " + input.mode);

JSON.stringify(output);
'''


def parse_iso(value: str, field: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp: {value}") from exc
    return parsed


def validate_plan(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("plan must be an object with schema_version 1")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("plan.tasks must be an array")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"tasks[{index}] must be an object")
        task_id = str(task.get("id", "")).strip()
        title = str(task.get("title", "")).strip()
        due_at = str(task.get("due_at", "")).strip()
        if not task_id or "]" in task_id or "\n" in task_id:
            raise ValueError(f"tasks[{index}].id is missing or contains a forbidden character")
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        if not title:
            raise ValueError(f"tasks[{index}].title is required")
        parse_iso(due_at, f"tasks[{index}].due_at")
        priority = int(task.get("priority", 0))
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"tasks[{index}].priority must be one of {sorted(VALID_PRIORITIES)}")
        source = task.get("source") or {"provider": "manual"}
        if not isinstance(source, dict) or not str(source.get("provider", "")).strip():
            raise ValueError(f"tasks[{index}].source.provider is required")
        normalized.append(
            {
                "id": task_id,
                "title": title,
                "due_at": due_at,
                "notes": str(task.get("notes", "")).strip(),
                "priority": priority,
                "source": {
                    key: str(source[key]).strip()
                    for key in ("provider", "label", "external_id", "url")
                    if source.get(key) is not None and str(source[key]).strip()
                },
            }
        )
        seen.add(task_id)
    return normalized


def run_jxa(payload: dict[str, Any]) -> dict[str, Any]:
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError("Apple Reminders integration requires macOS and /usr/bin/osascript")
    script = JXA.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    completed = subprocess.run(
        ["osascript", "-l", "JavaScript", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown osascript error"
        raise RuntimeError(f"Reminders automation failed: {message}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Reminders returned invalid JSON: {completed.stdout!r}") from exc


def end_of_day(day: date) -> str:
    return datetime.combine(day, time(23, 59, 59)).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List reminders as JSON")
    list_parser.add_argument("--list", default="Student Plan", dest="list_name")
    list_parser.add_argument("--due-on-or-before", type=date.fromisoformat)
    list_parser.add_argument("--include-completed", action="store_true")

    sync_parser = subparsers.add_parser("sync", help="Create or update reminders from a plan JSON file")
    sync_parser.add_argument("--plan", required=True, type=Path)
    sync_parser.add_argument("--list", default="Student Plan", dest="list_name")
    sync_parser.add_argument("--dry-run", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="Validate plan JSON without opening Reminders")
    validate_parser.add_argument("--plan", required=True, type=Path)

    rollover_parser = subparsers.add_parser("rollover", help="Move incomplete due reminders to another day")
    rollover_parser.add_argument("--list", default="Student Plan", dest="list_name")
    rollover_parser.add_argument("--from-date", type=date.fromisoformat)
    rollover_parser.add_argument("--to-date", type=date.fromisoformat)
    rollover_parser.add_argument("--dry-run", action="store_true")
    rollover_parser.add_argument("--include-unmanaged", action="store_true")

    subparsers.add_parser("doctor", help="Check local prerequisites without opening Reminders")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = {
                "macos": platform.system() == "Darwin",
                "osascript": shutil.which("osascript"),
                "ready": platform.system() == "Darwin" and bool(shutil.which("osascript")),
                "note": "The first real Reminders access may request Automation permission.",
            }
        elif args.command == "validate":
            raw = json.loads(args.plan.read_text(encoding="utf-8"))
            tasks = validate_plan(raw)
            result = {"valid": True, "plan": str(args.plan), "task_count": len(tasks)}
        elif args.command == "list":
            result = run_jxa(
                {
                    "mode": "list",
                    "list": args.list_name,
                    "include_completed": args.include_completed,
                    "due_on_or_before": end_of_day(args.due_on_or_before) if args.due_on_or_before else None,
                }
            )
        elif args.command == "sync":
            raw = json.loads(args.plan.read_text(encoding="utf-8"))
            tasks = validate_plan(raw)
            result = run_jxa(
                {
                    "mode": "sync",
                    "list": args.list_name,
                    "tasks": tasks,
                    "dry_run": args.dry_run,
                }
            )
        else:
            source_date = args.from_date or date.today()
            target_date = args.to_date or (source_date + timedelta(days=1))
            if target_date <= source_date:
                raise ValueError("--to-date must be later than --from-date")
            result = run_jxa(
                {
                    "mode": "rollover",
                    "list": args.list_name,
                    "source_date": source_date.isoformat(),
                    "target_date": target_date.isoformat(),
                    "cutoff": end_of_day(source_date),
                    "dry_run": args.dry_run,
                    "include_unmanaged": args.include_unmanaged,
                }
            )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

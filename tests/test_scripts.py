from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reminders = load("reminders")
import_ics = load("import_ics")
import_telegram = load("import_telegram")
import_local = load("import_local")
launchd = load("launchd")


class ReminderTests(unittest.TestCase):
    def test_validate_plan_normalizes_task(self):
        tasks = reminders.validate_plan(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "ntulearn-1",
                        "title": " Submit quiz ",
                        "due_at": "2026-08-12T18:00:00+08:00",
                        "priority": 1,
                        "rollover": False,
                        "source": {"provider": "ntulearn", "label": "CZ1001"},
                    }
                ],
            }
        )
        self.assertEqual(tasks[0]["title"], "Submit quiz")
        self.assertEqual(tasks[0]["source"]["provider"], "ntulearn")
        self.assertFalse(tasks[0]["rollover"])

    def test_validate_plan_rejects_duplicate_ids(self):
        task = {
            "id": "same",
            "title": "Task",
            "due_at": "2026-08-12T09:00:00+08:00",
            "source": {"provider": "manual"},
        }
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            reminders.validate_plan({"schema_version": 1, "tasks": [task, task]})

    def test_validate_plan_rejects_non_boolean_rollover(self):
        task = {
            "id": "manual-1",
            "title": "Submit report",
            "due_at": "2026-08-12T23:59:00+08:00",
            "rollover": "false",
            "source": {"provider": "manual"},
        }
        with self.assertRaisesRegex(ValueError, "rollover must be a boolean"):
            reminders.validate_plan({"schema_version": 1, "tasks": [task]})

    def test_run_jxa_parses_json(self):
        completed = type("Result", (), {"returncode": 0, "stdout": '{"created":1}', "stderr": ""})()
        with patch.object(reminders.platform, "system", return_value="Darwin"), patch.object(
            reminders.shutil, "which", return_value="/usr/bin/osascript"
        ), patch.object(reminders.subprocess, "run", return_value=completed) as run:
            result = reminders.run_jxa({"mode": "sync", "list": "Student Plan", "tasks": []})
        self.assertEqual(result["created"], 1)
        self.assertIn("mac-student-planner", run.call_args.kwargs["input"])

    def test_validate_command_is_fully_offline(self):
        plan = {
            "schema_version": 1,
            "tasks": [
                {
                    "id": "manual-1",
                    "title": "Review notes",
                    "due_at": "2026-08-12T09:00:00+08:00",
                    "source": {"provider": "manual"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            output = StringIO()
            with patch.object(reminders, "run_jxa") as run, redirect_stdout(output):
                code = reminders.main(["validate", "--plan", str(path)])
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["task_count"], 1)


class IcsTests(unittest.TestCase):
    def test_parses_event_and_todo_with_provenance(self):
        data = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:lecture-1
SUMMARY:CZ1001 Lecture
DTSTART;TZID=Asia/Singapore:20260812T100000
DTEND;TZID=Asia/Singapore:20260812T120000
LOCATION:LT1
END:VEVENT
BEGIN:VTODO
UID:quiz-1
SUMMARY:Submit quiz
DUE;VALUE=DATE:20260813
DESCRIPTION:Upload to NTULearn
END:VTODO
END:VCALENDAR
"""
        records = import_ics.parse_ics(data, "ntulearn", "NTU calendar", "Asia/Singapore")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["record_type"], "event")
        self.assertEqual(records[0]["source"]["external_id"], "lecture-1")
        self.assertEqual(records[1]["record_type"], "task")
        self.assertTrue(records[1]["all_day"])
        self.assertIn("+08:00", records[1]["due_at"])


class TelegramTests(unittest.TestCase):
    def test_flattens_export_and_filters_date(self):
        raw = {
            "name": "CZ1001 Group",
            "id": 42,
            "messages": [
                {
                    "id": 7,
                    "type": "message",
                    "date": "2026-08-10T09:00:00",
                    "from": "Tutor",
                    "text": ["Quiz due ", {"type": "bold", "text": "Friday"}],
                },
                {
                    "id": 8,
                    "type": "message",
                    "date": "2026-07-01T09:00:00",
                    "text": "Old message",
                },
            ],
        }
        records = import_telegram.normalize(raw, import_telegram.date(2026, 8, 1), None, "CZ1001")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "Quiz due Friday")
        self.assertEqual(records[0]["source"]["external_id"], "42:7")


class LocalSourceTests(unittest.TestCase):
    def test_imports_markdown_and_csv_with_local_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "week.md").write_text("# Week\n- Finish lab report", encoding="utf-8")
            (root / "deadlines.csv").write_text("course,due\nCZ1001,2026-08-20\n", encoding="utf-8")
            records, skipped = import_local.scan(root, recursive=True, max_bytes=100_000)
        self.assertEqual(skipped, [])
        self.assertEqual({record["record_type"] for record in records}, {"document", "table_row"})
        self.assertTrue(all(record["source"]["provider"] == "local" for record in records))
        self.assertTrue(all("path" in record["source"] for record in records))

    def test_local_id_is_stable_across_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "week.md"
            path.write_text("First version", encoding="utf-8")
            first = import_local.normalize_file(path, root, 100_000)[0]
            path.write_text("Second version with more detail", encoding="utf-8")
            second = import_local.normalize_file(path, root, 100_000)[0]
        self.assertEqual(first["source"]["external_id"], second["source"]["external_id"])

    def test_reads_root_and_recursion_from_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sources"
            root.mkdir()
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps({"providers": {"local": {"root": str(root), "recursive": True}}}),
                encoding="utf-8",
            )
            configured_root, recursive = import_local.settings(None, config, None)
        self.assertEqual(configured_root, root)
        self.assertTrue(recursive)

    def test_csv_explicit_id_survives_row_reordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tasks.csv"
            path.write_text("id,title\na,Read\nb,Write\n", encoding="utf-8")
            first = import_local.normalize_file(path, root, 100_000)
            path.write_text("id,title\nb,Write\na,Read\n", encoding="utf-8")
            second = import_local.normalize_file(path, root, 100_000)
        first_ids = {record["data"]["id"]: record["source"]["external_id"] for record in first}
        second_ids = {record["data"]["id"]: record["source"]["external_id"] for record in second}
        self.assertEqual(first_ids, second_ids)


class LaunchdTests(unittest.TestCase):
    def test_preview_plist_uses_rollover(self):
        parser = launchd.build_parser()
        args = parser.parse_args(["print", "--hour", "22", "--minute", "5", "--list", "Study"])
        plist = launchd.build_plist(args)
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 22, "Minute": 5})
        self.assertIn("rollover", plist["ProgramArguments"])
        self.assertIn("Study", plist["ProgramArguments"])

    def test_default_schedule_is_2100(self):
        args = launchd.build_parser().parse_args(["print"])
        plist = launchd.build_plist(args)
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 21, "Minute": 0})


if __name__ == "__main__":
    unittest.main()

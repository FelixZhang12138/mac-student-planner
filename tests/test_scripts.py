from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def test_sync_command_routes_normalized_task_and_dry_run(self):
        plan = {
            "schema_version": 1,
            "tasks": [
                {
                    "id": "ntulearn-hard-deadline",
                    "title": "Submit report",
                    "due_at": "2026-08-12T23:59:00+08:00",
                    "rollover": False,
                    "source": {
                        "provider": "ntulearn",
                        "path": "/private/course/report.md",
                    },
                }
            ],
        }
        result = {
            "list": "Study",
            "created": 1,
            "updated": 0,
            "skipped": 0,
            "dry_run": True,
            "list_would_be_created": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            output = StringIO()
            with patch.object(reminders, "run_jxa", return_value=result) as run, redirect_stdout(output):
                code = reminders.main(
                    ["sync", "--plan", str(path), "--list", "Study", "--dry-run"]
                )
        self.assertEqual(code, 0)
        payload = run.call_args.args[0]
        self.assertEqual(payload["mode"], "sync")
        self.assertEqual(payload["list"], "Study")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["tasks"][0]["rollover"])
        self.assertEqual(payload["tasks"][0]["source"]["path"], "/private/course/report.md")
        self.assertEqual(json.loads(output.getvalue())["created"], 1)

    def test_rollover_command_rejects_non_forward_target_date(self):
        error = StringIO()
        with patch.object(reminders, "run_jxa") as run, redirect_stderr(error):
            code = reminders.main(
                [
                    "rollover",
                    "--from-date",
                    "2026-08-12",
                    "--to-date",
                    "2026-08-12",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("--to-date must be later than --from-date", json.loads(error.getvalue())["error"])


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

    def test_unfolds_escaped_text_and_parses_utc_datetime(self):
        data = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:lecture-utc\r
SUMMARY:Long course\r
 title\r
DESCRIPTION:Line 1\\nLine 2\r
DTSTART:20260812T010203Z\r
URL:https://example.invalid/event/lecture-utc\r
END:VEVENT\r
END:VCALENDAR\r
"""
        records = import_ics.parse_ics(data, "outlook", "Calendar", "Asia/Singapore")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "Long coursetitle")
        self.assertEqual(records[0]["description"], "Line 1\nLine 2")
        self.assertEqual(records[0]["start_at"], "2026-08-12T01:02:03+00:00")
        self.assertEqual(records[0]["source"]["url"], "https://example.invalid/event/lecture-utc")


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

    def test_reads_nested_export_and_skips_invalid_message_dates(self):
        raw = {
            "chats": {
                "list": [
                    {
                        "name": "Course Group",
                        "id": 42,
                        "messages": [
                            {
                                "id": 7,
                                "type": "message",
                                "date": "2026-08-10T09:00:00",
                                "text": "Bring the lab kit",
                            },
                            {
                                "id": 8,
                                "type": "message",
                                "date": "not-a-date",
                                "text": "Unusable timestamp",
                            },
                        ],
                    },
                    {
                        "name": "Unrelated Chat",
                        "id": 99,
                        "messages": [
                            {
                                "id": 1,
                                "type": "message",
                                "date": "2026-08-10T10:00:00",
                                "text": "Ignore me",
                            }
                        ],
                    },
                ]
            }
        }
        records = import_telegram.normalize(raw, None, None, "course")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "Bring the lab kit")
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

    def test_scan_skips_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "week.md"
            target.write_text("# Week", encoding="utf-8")
            (root / "alias.md").symlink_to(target)
            records, skipped = import_local.scan(root, recursive=True, max_bytes=100_000)
        self.assertEqual(skipped, [])
        self.assertEqual([record["source"]["label"] for record in records], ["week.md"])

    def test_scan_records_oversized_and_malformed_files_as_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_text("x" * 200, encoding="utf-8")
            (root / "broken.json").write_text("{invalid", encoding="utf-8")
            records, skipped = import_local.scan(root, recursive=True, max_bytes=100)
        self.assertEqual(records, [])
        reasons = {Path(item["path"]).name: item["reason"] for item in skipped}
        self.assertIn("file exceeds 100 bytes", reasons["large.txt"])
        self.assertIn("Expecting property name", reasons["broken.json"])

    def test_json_explicit_id_survives_task_reordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tasks.json"
            first_tasks = [
                {"id": "read", "title": "Read"},
                {"id": "write", "title": "Write"},
            ]
            path.write_text(json.dumps({"tasks": first_tasks}), encoding="utf-8")
            first = import_local.normalize_file(path, root, 100_000)
            path.write_text(json.dumps({"tasks": list(reversed(first_tasks))}), encoding="utf-8")
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

    def test_install_writes_plist_and_bootstraps_launch_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plist_path = root / "LaunchAgents" / "com.example.plist"
            log_dir = root / "logs"
            reminders_script = root / "reminders.py"
            python_path = root / "python3"
            reminders_script.write_text("# test", encoding="utf-8")
            python_path.write_text("# test", encoding="utf-8")
            args = launchd.build_parser().parse_args(
                ["install", "--python", str(python_path), "--list", "Study"]
            )
            with patch.object(
                launchd, "paths", return_value=(plist_path, log_dir, reminders_script)
            ), patch.object(launchd.sys, "platform", "darwin"), patch.object(
                launchd.shutil, "which", return_value="/bin/launchctl"
            ), patch.object(launchd.os, "getuid", return_value=501), patch.object(
                launchd, "run_launchctl"
            ) as run:
                result = launchd.install(args)
            plist = launchd.plistlib.loads(plist_path.read_bytes())
        self.assertEqual(result["action"], "installed")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 21, "Minute": 0})
        self.assertIn("Study", plist["ProgramArguments"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, ("bootout", "gui/501", str(plist_path)))
        self.assertTrue(run.call_args_list[0].kwargs["allow_failure"])
        self.assertEqual(run.call_args_list[1].args, ("bootstrap", "gui/501", str(plist_path)))

    def test_status_reports_installed_and_loaded_agent(self):
        completed = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "service is loaded", "stderr": ""},
        )()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plist_path = root / "com.example.plist"
            plist_path.write_text("test", encoding="utf-8")
            with patch.object(
                launchd, "paths", return_value=(plist_path, root / "logs", root / "reminders.py")
            ), patch.object(launchd.sys, "platform", "darwin"), patch.object(
                launchd.shutil, "which", return_value="/bin/launchctl"
            ), patch.object(launchd.os, "getuid", return_value=501), patch.object(
                launchd, "run_launchctl", return_value=completed
            ):
                result = launchd.status()
        self.assertTrue(result["installed"])
        self.assertTrue(result["loaded"])
        self.assertEqual(result["detail"], "service is loaded")


if __name__ == "__main__":
    unittest.main()

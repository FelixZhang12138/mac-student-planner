#!/usr/bin/env python3
"""Preview, install, inspect, or remove the macOS evening rollover LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "com.openai.mac-student-planner.rollover"


def paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log_dir = home / "Library" / "Logs" / "mac-student-planner"
    reminders_script = Path(__file__).resolve().with_name("reminders.py")
    return plist_path, log_dir, reminders_script


def build_plist(args: argparse.Namespace) -> dict[str, object]:
    _, log_dir, reminders_script = paths()
    program_args = [
        str(Path(args.python).expanduser().resolve()),
        str(reminders_script),
        "rollover",
        "--list",
        args.list_name,
    ]
    if args.include_unmanaged:
        program_args.append("--include-unmanaged")
    return {
        "Label": LABEL,
        "ProgramArguments": program_args,
        "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "rollover.log"),
        "StandardErrorPath": str(log_dir / "rollover-error.log"),
        "ProcessType": "Background",
    }


def print_result(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_launchctl(*arguments: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def install(args: argparse.Namespace) -> dict[str, object]:
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise RuntimeError("LaunchAgent installation requires macOS and launchctl")
    plist_path, log_dir, reminders_script = paths()
    if not reminders_script.exists():
        raise RuntimeError(f"missing reminders script: {reminders_script}")
    python_path = Path(args.python).expanduser().resolve()
    if not python_path.exists():
        raise RuntimeError(f"Python executable does not exist: {python_path}")

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    temporary = plist_path.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(build_plist(args), sort_keys=True))
    temporary.replace(plist_path)

    domain = f"gui/{os.getuid()}"
    run_launchctl("bootout", domain, str(plist_path), allow_failure=True)
    run_launchctl("bootstrap", domain, str(plist_path))
    return {
        "ok": True,
        "action": "installed",
        "label": LABEL,
        "plist": str(plist_path),
        "hour": args.hour,
        "minute": args.minute,
        "list": args.list_name,
    }


def uninstall() -> dict[str, object]:
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise RuntimeError("LaunchAgent removal requires macOS and launchctl")
    plist_path, _, _ = paths()
    domain = f"gui/{os.getuid()}"
    if plist_path.exists():
        run_launchctl("bootout", domain, str(plist_path), allow_failure=True)
        plist_path.unlink()
        removed = True
    else:
        removed = False
    return {"ok": True, "action": "uninstalled", "label": LABEL, "removed": removed}


def status() -> dict[str, object]:
    plist_path, _, _ = paths()
    loaded = False
    detail = ""
    if sys.platform == "darwin" and shutil.which("launchctl"):
        result = run_launchctl("print", f"gui/{os.getuid()}/{LABEL}", allow_failure=True)
        loaded = result.returncode == 0
        detail = (result.stderr or result.stdout).strip()[:1000]
    return {
        "ok": True,
        "action": "status",
        "label": LABEL,
        "plist": str(plist_path),
        "installed": plist_path.exists(),
        "loaded": loaded,
        "detail": detail,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("print", "install"):
        child = subparsers.add_parser(command)
        child.add_argument("--hour", type=int, default=21, choices=range(0, 24), metavar="0-23")
        child.add_argument("--minute", type=int, default=30, choices=range(0, 60), metavar="0-59")
        child.add_argument("--list", default="Student Plan", dest="list_name")
        child.add_argument("--python", default=sys.executable)
        child.add_argument("--include-unmanaged", action="store_true")
    subparsers.add_parser("status")
    subparsers.add_parser("uninstall")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "print":
            plist_path, _, _ = paths()
            xml = plistlib.dumps(build_plist(args), sort_keys=True).decode("utf-8")
            print(xml, end="")
            print_result({"ok": True, "action": "preview", "target": str(plist_path)})
            return 0
        if args.command == "install":
            result = install(args)
        elif args.command == "uninstall":
            result = uninstall()
        else:
            result = status()
    except (OSError, RuntimeError) as exc:
        print_result({"ok": False, "error": str(exc)})
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

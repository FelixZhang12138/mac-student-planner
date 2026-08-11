#!/usr/bin/env python3
"""Normalize a private local study-source directory into provenance-preserving JSON."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import import_ics


SUPPORTED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".tsv", ".ics"}
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.json"


def stable_id(path: Path, suffix: str = "") -> str:
    material = f"{path.resolve()}|{suffix}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def source_for(path: Path, relative: str, modified_at: str, external_id: str) -> dict[str, str]:
    return {
        "provider": "local",
        "label": relative,
        "external_id": external_id,
        "path": str(path.resolve()),
        "modified_at": modified_at,
    }


def iter_files(root: Path, recursive: bool) -> Iterable[Path]:
    candidates = root.rglob("*") if recursive else root.glob("*")
    for path in sorted(candidates):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def read_text(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes")
    return path.read_text(encoding="utf-8-sig")


def normalize_file(path: Path, root: Path, max_bytes: int) -> list[dict[str, Any]]:
    stat = path.stat()
    relative = str(path.relative_to(root))
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    suffix = path.suffix.lower()
    text = read_text(path, max_bytes)

    if suffix == ".ics":
        records = import_ics.parse_ics(text, "local", relative, "Asia/Singapore")
        for index, record in enumerate(records, start=1):
            record_source = record.setdefault("source", {})
            record_source["provider"] = "local"
            record_source["label"] = relative
            record_source["path"] = str(path.resolve())
            record_source["modified_at"] = modified_at
            record_source.setdefault("external_id", stable_id(path, str(index)))
        return records

    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        records: list[dict[str, Any]] = []
        for row_number, row in enumerate(csv.DictReader(text.splitlines(), delimiter=delimiter), start=2):
            explicit_id = next(
                (str(row[key]).strip() for key in ("id", "uid", "task_id") if row.get(key) and str(row[key]).strip()),
                str(row_number),
            )
            external_id = stable_id(path, explicit_id)
            records.append(
                {
                    "record_type": "table_row",
                    "data": {str(key): value for key, value in row.items() if key is not None},
                    "source": source_for(path, relative, modified_at, external_id),
                }
            )
        return records

    if suffix == ".json":
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
            records = []
            for index, task in enumerate(parsed["tasks"], start=1):
                explicit_id = (
                    str(task.get("id")).strip()
                    if isinstance(task, dict) and task.get("id") and str(task.get("id")).strip()
                    else str(index)
                )
                external_id = stable_id(path, explicit_id)
                records.append(
                    {
                        "record_type": "task_candidate",
                        "data": task,
                        "source": source_for(path, relative, modified_at, external_id),
                    }
                )
            return records
        rendered = json.dumps(parsed, ensure_ascii=False, indent=2)
    else:
        rendered = text

    external_id = stable_id(path)
    return [
        {
            "record_type": "document",
            "text": rendered,
            "source": source_for(path, relative, modified_at, external_id),
        }
    ]


def scan(root: Path, recursive: bool, max_bytes: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"local source root is not a directory: {resolved}")
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in iter_files(resolved, recursive):
        try:
            records.extend(normalize_file(path, resolved, max_bytes))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, csv.Error) as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
    return records, skipped


def settings(root: Path | None, config_path: Path, recursive: bool | None) -> tuple[Path, bool]:
    if root is not None:
        return root, bool(recursive)
    try:
        config = json.loads(config_path.expanduser().read_text(encoding="utf-8"))
        local = config["providers"]["local"]
        configured_root = Path(str(local["root"])).expanduser()
        configured_recursive = bool(local.get("recursive", True)) if recursive is None else recursive
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"--root is required unless {config_path.expanduser()} defines providers.local.root"
        ) from exc
    return configured_root, configured_recursive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--max-bytes", type=int, default=1_000_000)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_bytes <= 0:
        print(json.dumps({"ok": False, "error": "--max-bytes must be positive"}), file=sys.stderr)
        return 1
    try:
        source_root, recursive = settings(args.root, args.config, args.recursive)
        records, skipped = scan(source_root, recursive, args.max_bytes)
        payload = {
            "schema_version": 1,
            "source_root": str(source_root.expanduser().resolve()),
            "records": records,
            "skipped": skipped,
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

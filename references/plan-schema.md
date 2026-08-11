# Plan schema

Use UTF-8 JSON. The root object must contain `schema_version`, `timezone`, `period`, and `tasks`.

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-11T20:00:00+08:00",
  "timezone": "Asia/Singapore",
  "period": {
    "kind": "daily",
    "start": "2026-08-12",
    "end": "2026-08-12"
  },
  "tasks": [
    {
      "id": "ntulearn-cz3005-assignment-2",
      "title": "Submit CZ3005 Assignment 2",
      "due_at": "2026-08-12T18:00:00+08:00",
      "notes": "Final PDF upload",
      "priority": 1,
      "source": {
        "provider": "ntulearn",
        "label": "CZ3005",
        "external_id": "assignment-2",
        "url": "https://example.invalid/course/assignment-2"
      }
    }
  ]
}
```

## Task fields

- `id`: Required stable identifier. Prefer `<provider>-<external-id>`. Do not include secrets.
- `title`: Required non-empty reminder title.
- `due_at`: Required ISO 8601 timestamp. Include an offset when known.
- `notes`: Optional plain text.
- `priority`: Optional Apple Reminders priority: `0` none, `1` high, `5` medium, `9` low.
- `source.provider`: Required short name such as `reminders`, `ntulearn`, `outlook`, `telegram`, or `manual`.
- `source.label`: Optional human-readable course, calendar, chat, or list name.
- `source.external_id`: Optional source identifier.
- `source.url`: Optional source URL. Never store an authentication token in it.

The Reminders synchronizer embeds `[mac-student-planner:id=<id>]` in the reminder notes. Reusing the ID updates the existing reminder instead of creating a duplicate.

## Normalized source records

Import scripts emit candidate records rather than final tasks. Convert only actionable candidates into `tasks` after checking dates, ambiguity, and duplicates. Preserve the entire `source` object during conversion.

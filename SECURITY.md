# Security Policy

## Scope

Mac Student Planner processes potentially sensitive academic and personal planning data. The public repository contains code, schemas, and sanitized examples only. Real plans, course exports, Telegram exports, local-source indexes, logs, and private configuration must remain outside version control.

The security boundary covers:

- Apple Reminders automation performed through JXA and `osascript`;
- local parsing of ICS, Telegram JSON, and study files;
- provenance stored in normalized records and reminder notes; and
- instructions for read-only NTULearn and Outlook browser access.

NTULearn and Outlook authentication is handled by the user's browser. This project does not request, read, or persist passwords, MFA codes, cookies, or browser session tokens.

## Credential and configuration handling

- Keep local settings in the ignored `config.json` file or environment variables.
- Use `MAC_STUDENT_PLANNER_TIMEZONE` only for a timezone name; do not place secrets in it.
- Never commit `.env` files, private keys, certificates, tokens, credentials files, real exports, or generated plans.
- Treat paths and filenames in normalized local-source output as private metadata.
- Use the sanitized files under `assets/` when writing tests or documentation.

If a credential is committed accidentally, revoke or rotate it immediately. Removing it from the latest commit is not sufficient because the value remains in Git history.

## Reporting a vulnerability

Do not open a public issue for a vulnerability or accidental data exposure. Use the repository's private GitHub security advisory form:

<https://github.com/FelixZhang12138/mac-student-planner/security/advisories/new>

Include the affected component, reproduction steps, expected impact, and any suggested mitigation. Do not include real credentials, session data, or private course material in the report.

## Supported version

Security fixes are applied to the latest version on the `main` branch. Older commits and local forks are not maintained separately.

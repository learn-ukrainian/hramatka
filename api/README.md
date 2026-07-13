# Hramatka bake API

The frozen teacher-pilot implementation contract is checked in alongside the current
mock-first skeleton:

- `openapi.yaml` — exact same-origin `/api/*` wire contract
- `teacher-access-contract.md` — invite, cookie, CSRF, ownership, and privacy rules
- `persistence-contract.md` — SQLite aggregate, transaction, catalog, and revision rules

The code below still describes the pre-pilot skeleton. Implementations must migrate it
to the frozen contract above; they must not infer the pilot interface from current route
behavior.

This private API is a thin local skeleton for the Step 5 asynchronous bake
workflow. It is poll-first and requires one `Authorization: Bearer` teacher
token from `HRAMATKA_TEACHER_TOKEN`; it stores no student records.

`POST /lessons` requires `{id, anchor, duration, focus}`. The caller-supplied
`id` is the lesson ID and idempotency key: identical inputs return the existing
job; different inputs with the same ID return `409`.

The available endpoints are:

- `POST /lessons`
- `GET /lessons/{id}/status`
- `GET /lessons/{id}`
- `POST /lessons/{id}/blocks/{block_id}/accept` (explicitly acknowledge a visible warning)
- `POST /lessons/{id}/accept`
- `POST /lessons/{id}/draft` (clear final acceptance while retaining the ready artifact)

Status reports only actual stages: `текст отримано`, `завдання складено`,
`перевірка`, and `готово`. A new process marks an unfinished bake failed rather
than pretending to resume or leaving it eternally `baking`. The service runs
one bake worker; if an in-process adapter exceeds the hard timeout, its watchdog
quarantines that worker and fails queued/new bakes honestly until the API is
restarted. This avoids claiming that Python can safely cancel a blocked engine
thread.

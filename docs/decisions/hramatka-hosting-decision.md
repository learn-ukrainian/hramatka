# Hramatka Hosting Decision

## Status

`GO` as of 2026-07-10 — user's explicit build go («go with changes») recorded on
public epic learn-ukrainian.github.io#4542 and this repo's #12.

## Decision

**Host = Scaleway, `DEV1-M` instance, start-cheap-upgrade-later.** One shared
server for the Hramatka backend AND the atlas/practice backend (same stack and
tools; user decision 2026-07-07/08, reaffirmed 2026-07-10).

## History (this decision flip-flopped three times — pinned here to stop that)

| Date | State | Where recorded |
| --- | --- | --- |
| 2025-12 (vibe era) | Supabase | vibe project docs (superseded) |
| 2026-07-07 | Private HF Space | `hramatka/design-v0.md` §v1-decisions (superseded) |
| 2026-07-08 ~01:00 | HF PRO $9/mo, "NO Scaleway" | `hramatka/app-spec-v1.md` hosting section (superseded) |
| 2026-07-08 morning | **Scaleway dev1-m** ("start with a cheap one and upgrade if needed", "maybe dev1-m to start with?") | conversation 8c54d642 + `hramatka/scaleway-setup.md` (08:32, latest artifact) |
| 2026-07-10 | **PINNED: Scaleway dev1-m** | this document |

## Why Scaleway over the (verified, cheaper-looking) HF option

- Latest explicit user direction, twice (2026-07-08 morning, 2026-07-10 go).
- EU data residency for the teacher-account/lesson store (the app-spec itself
  noted "EU residency still favors Scaleway" even while recommending HF).
- One box co-hosting atlas/practice backend + Hramatka — the existing task
  ladder (tasks/001-006) and runbooks are already Scaleway-shaped.
- No Space-restart/ephemeral-disk semantics; boring durable disk beats
  object-mounted SQLite (the fleet's top ops risk in the HF design).

HF Space stays documented in `hramatka/app-spec-v1.md` as the verified fallback
(≈$9/mo) if Scaleway costs surprise us.

## Scope of this decision

Provider + instance class + co-location only. Database choice per workload,
secrets, and backup cadence live in the task ladder (tasks/001-006) and
`hramatka/scaleway-setup.md`.

# Implementation ledger — Pudge iPhone P0

Plan: `pudge-iphone-implementation-plan.md` (03.10.2026), first audiobook release.

## Decisions

- Existing written design and implementation plan are the approved basis of this reconstruction.
- Native execution; no delegation, commits, publishing or changes to external accounts.
- Extracted copy under `work/pudge` is isolated from the user's Mac checkout.
- New HTTPS v2 service remains separate from legacy v1 Companion; v1 behavior is retained.
- P0 only: audiobooks, pairing, bidirectional files, archive receipts, storage, progress, bookmarks, handoff. Other content/Jiten follows the supplied plan later.
- Reusable types use `assetId`, `editionId`, `timelineRevision`; milliseconds are integers.
- Pair retry is bound to an invitation plus durable device and attempt IDs; revoked devices cannot retry into active access.
- Progress is an event log with optimistic revisions; independent stale branches become conflicts. Replayed events are recognized before edition validation.
- Original Mac audio is never overwritten. Prepared AAC variants are separate resources with a verified duration map; native format quality/seek remains a device acceptance gate.
- Checkpoints are full source ZIPs plus progress reports, saved every 30 minutes and after meaningful milestones.

## Pre-flight

- T02 → T05/T16: manifests and checkpoints share UUIDs, SHA-256, timeline coordinates.
- T04 → T06/T07: foreground and background transfers must use the same certificate pin and bearer credential.
- T07 → T08: an HTTP chunk response is not an archive receipt; generation and retention must be rechecked before deletion.
- T15 → T16A/T16B: callbacks bind to session and base revision; regular sync never seeks a running player.
- T00: source restored, portable Python dependencies pinned to lock where available; initial collection required installing pywebview on Linux.

## Status

T00–T16C: reconstruction in progress; see progress.md and verification report for completed code and execution evidence.

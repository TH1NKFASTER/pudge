# Algorithms and state model

This document describes the decisions behind what Pudge shows. It intentionally
stays at product level: exact thresholds and implementation details belong in
tests and source code.

Pudge generally prefers to leave an uncertain item waiting instead of showing a
confident but wrong match.

## Episode readiness

An anime episode moves through a small set of useful states: found locally,
preparing subtitles, waiting for text subtitles, Ready, and Watched. Dropped is
explicit user intent and is not changed by a normal scan.

**Ready means usable now**, not merely "a subtitle used to exist". Pudge checks
that the video still exists and that the selected subtitle file or embedded text
track is still available. If a prepared subtitle disappears, Refresh can move
the episode back to preparation and queue a repair.

A normal scan does not erase watched progress. Resetting watched progress is an
explicit action.

## Subtitle preparation and repair

Pudge tries several kinds of subtitle evidence in increasing order of cost:

1. local and embedded tracks;
2. downloaded subtitle candidates such as Jimaku;
3. ordinary timing checks and alignment;
4. speech recognition only when simpler evidence is not enough.

Candidates are checked for language, title/episode identity, usable text, and
reasonable timing. Pudge keeps the evidence behind a selection so Diagnostics
can explain what happened later.

Accepted prepared subtitles are copied to Pudge's persistent data rather than
left only in a disposable macOS cache. If an older Ready entry points to a
missing cached file, Pudge treats that as a real repair case and rebuilds the
subtitle even when the available candidate list has not changed.

When many episodes need repair, each title's next unwatched or earliest blocked
episode is treated as its frontier. Frontier episodes are handled before middle
or later episodes so the library becomes watchable as soon as possible.

CPU-heavy subtitle work may pause while media is actively playing. The queued
work stays persisted and resumes afterward.

## Downloads and release choice

Pudge is local-first. Before searching the network it checks the database and
local media folders so an existing episode is not downloaded again.

Release ranking considers the requested title and episode, release group,
resolution/source, Japanese audio evidence, size, seeders, and user preferences.
Automatic download requires enough evidence to be confident. An existing
completed download is kept unless a replacement is clearly better under the
configured upgrade rules.

Torrent metadata is provenance, not episode identity. If two completed download
records point to the same local episode, they must not make that episode bounce
between states or repeatedly recreate subtitle jobs.

## Light Novels, manga, and audiobook series

Libraries are grouped by a normalized series identity and volume number. Pudge
uses explicit volume metadata when available and can also infer common forms
such as `Volume 2`, `Vol. 2`, or Japanese volume labels.

For audiobooks, a linked Light Novel can supply the missing volume identity.
Inside a recognized series the UI uses simple `Volume N` labels even when the
original filenames are inconsistent.

Automatic LN/audiobook linking is conservative. A clear shared identity and
compatible volume can produce a link; ambiguous candidates are left for manual
selection. Existing manual links are never replaced automatically.

## Paired reading and highlighting

Paired Light Novel/audiobook reading is based on text/audio anchors. Strong word
or phrase anchors are preferred. Between them, Pudge interpolates progress and
uses speech activity so highlighting does not race through silence.

Near chapter starts or sparse passages, Pudge can use finer speech recognition
and reading information to add more anchors. If the evidence is not precise
enough, it falls back to a smoother coarse mapping rather than inventing exact
word timings.

## Dictionary readings and pitch accent

Pudge prefers reading and accent information supplied for the exact token or
surface form. When only dictionary-form information is available, it may show a
derived display fallback. Derived information is marked as such and is not
presented as a separately verified dictionary entry.

## Background work

Long-running imports, OCR, transcription, subtitle repair, and similar work is
stored as resumable jobs. Jobs retain progress and retry information across UI
refreshes and app restarts. Cancellation happens at safe boundaries rather than
leaving half-written library state.

## Companion sync

The desktop remains the source of truth for local files and completed anime
progress. Companion devices exchange progress events instead of opening the
library database directly. Older offline progress cannot reopen an episode that
was already completed on the desktop.

The wire-level details are documented in [MOBILE_SYNC_PROTOCOL.md](../MOBILE_SYNC_PROTOCOL.md).

# Pudge — LLM handoff (v0.6.73)

This document is intended to let another LLM continue development without rediscovering the non-obvious project state. Update it for **every release** and send the updated standalone Markdown file to the user together with the release updater/patch. This requirement itself is permanent project context.

## Project / workflow

- Repository: `TH1NKFASTER/pudge` (public GitHub).
- User checkout: `~/Downloads/pudge` on macOS.
- Current target release in this handoff: **v0.6.73**, based on v0.6.72 commit `ac683525577c89e838d733446fa5a7056a7e5c1c`.
- The user runs release commands locally. Do not silently push through a connector during ordinary work.
- Release command must run all four local test batches before commit/push/tag/install:
  `python scripts/run_test_batch.py --batch N --batches 4` using `.venv-test/bin/python`.
- `.venv-test/` must remain gitignored. A previous release accidentally committed the whole test venv because of `git add -A`.
- Do not print a kilometer-long `git diff` in Terminal. Show `git diff --stat` and `git status --short` only.
- Never put logs on Desktop. Use `/tmp`, `~/Downloads`, or project/cache folders. When asking for a debug log/archive, the command must automatically open its containing folder in Finder.
- Release updater/patch must **fail hard on baseline mismatch**. Do not treat “patch failed to apply” as “maybe already applied; continue”. This previously caused tag/version mismatches.

## What Pudge does

macOS anime + local-media + Light Novel application. Major pieces:
- AniList library/progress and relation/watch-order graph.
- Nyaa/qBittorrent/aria2 download flow.
- Jimaku Japanese subtitle discovery.
- Subtitle preparation/synchronization (ALASS/ffsubsync/constant-offset validation/LLM validation), caches and playback-cleaned SRT generation.
- mpv playback/progress tracking.
- Light Novel EPUB/TXT reader with Jiten/JPDB study integration and optional LLM translation.
- WKWebView UI in `anime_mpv/web/index.html`.

## Important current architecture / gotchas

### Subtitle jobs are subprocesses

`AnimeManager.process_subtitle_jobs()` launches:

```text
python -m anime_mpv.cli --prepare-only --no-anilist-progress ...
```

Each preparation job is a separate process. Therefore rate-limit state that must be shared between jobs cannot live only in Python memory. Cross-process state belongs in SQLite or a cache file.

Background preparation can be CPU-heavy for minutes because sync/constant-offset validation is computationally expensive. The main Pudge WKWebView process can be idle while a child prepare process consumes most of a CPU core.

### Jimaku rate limiting

Jimaku sometimes returns HTTP 429 in normal use. v0.6.72 treated it as a generic `JimakuError`, displayed the full httpx error/URL in `Preparation job`, and did not share a cooldown across prepare subprocesses.

v0.6.73 design:
- global cooldown file: `<cache>/jimaku-api/rate-limit.json`;
- honor numeric `Retry-After`, default 10 minutes;
- stale cached non-empty Jimaku payload may be used during 429/cooldown;
- when no cache exists, show concise `Jimaku rate limited (429); retry in N min`;
- a rate-limit defer does **not increment subtitle failure attempts**;
- manual refresh may make a job due, but the Jimaku client still respects the shared cooldown and therefore does not immediately hammer the API again.

### Subtitle retry attempts

`Database.postpone_subtitle_job()` increments `attempts`. Temporary infrastructure conditions (Jimaku 429 and energy-throttled unstarted migration jobs) should use a separate `defer_subtitle_job()` that returns the job to `pending` without incrementing attempts.

### High-energy background behavior

Energy debug after v0.6.72 showed:
- main Pudge process: ~0% CPU / sleeping;
- child `--prepare-only` Bleach job: ~76% CPU for minutes;
- qBittorrent had some load during one-time recheck;
- no evidence of a JS/WKWebView render loop.

qBittorrent legacy-path repair succeeded for the user, so do not redesign it without evidence of a new problem.

v0.6.73 limits automatic migration-requeued subtitle work so only one job marked `Повторная подготовка после обновления синхронизации субтитров` is allowed per background `process_subtitle_jobs()` invocation; extra claimed migration jobs are deferred without increasing attempts. User-requested/preferred-path jobs remain unaffected.

### Bleach stale subtitle case

Problem video:
`Bleach.2004.S17E43.REPACK.1080p.DSNP.WEB-DL.AAC2.0.H.264-AnoZu.mkv`, AniList media 185874.

A stale `playback-srt/v12-...srt` survived earlier sync algorithm changes. Updating the low-level syncing fingerprint alone did not invalidate the DB/final-pipeline selection. v0.6.72 generation 16 requeues stale playback SRT by comparing subtitle-history candidate lineage against the direct playback-cleaned filename. This finally forced actual resynchronization.

Do not “fix” this by invalidating every local/manual subtitle indiscriminately.

### qBittorrent rename repair

After `Anime MPV -> Pudge`, old qBittorrent torrents pointed at `~/Movies/Anime MPV/...` while data existed under `~/Movies/pudge/...`, causing `missingFiles`.

v0.6.72 added real WebUI API `setLocation` + `recheck` and automatic path repair only when the mapped Pudge target exists. The user confirmed the Error/missingFiles problem is fixed. Treat this as resolved unless new logs show otherwise.

## Light Novel implementation

Primary files:
- `anime_mpv/light_novels.py`
- `anime_mpv/web_app.py`
- `anime_mpv/web/index.html`

### Reading position already existed in backend

`ln_books` already has:
- `current_chapter`
- `current_offset`

`LightNovelService.update_position()` and Web API `light_novel_position()` already existed before v0.6.73. v0.6.72 frontend saved scroll position with a debounce, but `openLightNovel()` / `loadLightNovelChapter()` always reset scroll to zero. The bug was frontend restore/flush logic, not missing storage schema.

v0.6.73 behavior:
- restore `current_offset` when reopening a book;
- flush position before closing reader or switching chapters;
- support both vertical scrolling and page/horizontal mode when computing normalized offset.

### Reader width

Before v0.6.73 the width setting could technically reach 1600, but `.ln-reader-scroll` used fixed centering padding based on 900px:

```css
padding:42px max(24px,calc((100vw - 900px)/2)) 100px
```

This made large values appear capped around half the user’s wide display. v0.6.73 removes that fixed 900px centering assumption and raises the setting max to 2400px; `.ln-reader`’s `--ln-content-width` becomes authoritative.

### Jiten popup

Backend `_chapter_payload()` already computes `normalizedState` (`new`, `learning`, `due`, `known`, `blacklisted`). v0.6.72 frontend ignored it and rendered raw Jiten `cardState`, producing meaningless `State: 0`.

v0.6.73 renders the normalized human-readable state instead. Jiten bracket-reading notation such as `組[く]み替[か]える` is rendered as ruby/furigana instead of raw brackets.

### Existing LN requirements already implemented before v0.6.73

- Local LN card Nyaa button removed; global Auto Nyaa remains for missing volumes.
- Actions align at the bottom of LN cards.
- Clicking an AniList-bound LN card opens its AniList manga/novel page.
- AniList “Authentication guide” removed from ordinary settings (onboarding may still contain the one auth guide link).
- LLM settings describe actual subtitle semantic validation + selected-text LN translation.

## User-facing current wants / pending validation

Validate after v0.6.73:
1. LN reader can use substantially more than 900px on a wide display (up to 2400px setting).
2. Reopening a book returns to the same chapter and approximate scroll/page position.
3. Jiten popup displays a human state such as `New`, not `State: 0`, and bracket readings are readable.
4. Jimaku 429 appears as temporary rate limiting, not a scary generic Preparation error, and does not inflate attempts.
5. Background migration subtitle jobs no longer run several heavy sync jobs back-to-back and heat the Mac for a long stretch.
6. Bleach synchronization quality still needs real-world validation after the migration finally triggered a fresh sync.

## Durable product/UI preferences

- Interface should stay compact; avoid noisy diagnostic text in normal UI.
- “Waiting for subs” means temporarily waiting for suitable subtitles, including rate limits/network availability; temporary service throttling should not look like a permanent media error.
- Automatic subtitle polling target is 10 minutes unless there is a genuine transient-service backoff.
- Expensive background work should yield to playback/user actions.
- Logs and diagnostics should make source/retry/backoff reasons visible without forcing those details into the main cards.

## Permanent handoff requirement

**Every future Pudge version must update this handoff document and send a standalone downloadable copy to the user alongside the release patch/updater.** Include new non-obvious implementation details, latest bugs/log conclusions, resolved issues that should not be accidentally reworked, and the user’s current feature requests.

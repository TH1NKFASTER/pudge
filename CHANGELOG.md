# Pudge changelog

## Unreleased

## v0.7.15

- Release validation update for the exact-process in-app restart fix.


## v0.7.14

- Fixed update restarts to terminate the exact running Pudge process before installing and reopening the app.


## v0.7.13

- Release validation update for the improved in-app restart flow.


## v0.7.12

- Fixed app updates leaving the previous Pudge window running after the new version opened.


## v0.7.11

- Fixed automatic updates launched from the macOS app failing to find an existing Homebrew installation.


## v0.7.10

- Made app updates retry transient download failures automatically instead of requiring repeated manual attempts.
- Made update restarts wait for the previous Pudge process to exit before reopening the updated app, preventing duplicate windows.
- Removed the native WebView update confirmation dialog that appeared with the Python host icon.


## v0.7.9

- Fixed the macOS application icon when Pudge runs through the managed Python environment, avoiding the default Python launcher icon during application startup.


## v0.7.8

- Fixed release updates so extracted installers do not depend on executable file permissions.
- Isolated post-install package verification from the current working directory and Python environment, preventing valid updates from being mistaken for stale installations.


## v0.7.7

- Fixed in-app updates for the native managed-environment launcher so the running Pudge process is stopped correctly and the updated app can reopen automatically.


## v0.7.6

- Replaced the frozen PyInstaller application runtime with a lightweight native macOS launcher that always executes the managed Pudge environment, so in-app updates immediately run the newly installed package.
- Preserved fast updates without reinstalling MangaOCR, Torch or other heavy dependencies and kept package/app rollback on failed updates.
- Added native notification handling directly to the macOS launcher so notification identity remains Pudge while the main application continues to run from the managed Python environment.
- Removed the duplicated bundled Pudge runtime, reducing the installed app bundle to a lightweight launcher while keeping the full application and dependencies in the managed environment.

## v0.7.5

- Reworked in-app updates to preserve the existing runtime environment, safely roll back failed package/app changes, sanitize inherited Python/Tcl/Tk environment variables and reuse a versioned native launcher runtime.
- Made repeat updates much faster by replacing the Pudge wheel without rebuilding the native PyInstaller runtime or reinstalling heavy dependencies such as MangaOCR and Torch when the launcher runtime has not changed.
- Fixed ready RELEASING anime so available episodes appear under New episodes ready rather than Completed and ready, including titles outside the AniList CURRENT list.

## v0.7.4

- Added batch-first anime download selection, stricter episode/season identity checks, saturated seeder scoring and short qBittorrent candidate races for fresher, safer release selection.
- Hardened Jimaku subtitle identity matching so explicit AniList mismatches and season markers such as `S3 - 13` cannot be mistaken for the requested episode, while preserving exact-title special overrides.
- Expanded the Light Novel reader with native Jiten study controls, persisted deck selection, configurable study triggers, state-filtered furigana/underlines, word-color vs underline modes and local-LLM reader CSS generation.
- Improved Light Novel furigana handling for inflected words and kept ruby annotations out of selection translation and copied text.
- Fixed Light Novel reader UI regressions around inline JavaScript parsing, state-menu visibility/stacking, trackpad secondary-click handling, menu dismissal and filtered underline rendering.
- Hid redundant Planning download actions once an episode is complete or already downloading, while keeping stalled/error states retryable.

## v0.7.3

- Added Jiten filters and sorts to Planning; enriched compact and full Light Novel cards with progress, AniList and Jiten facts; and restored wide audiobook playback cards with linked covers.
- Kept reader color controls and custom CSS in sync, added a separate pitch-accent color, rendered matching-kana pitch directly in the word, and limited pitch accent to horizontal reading.
- Moved character-name cues to the Light Novel context menu, aligned chapter selection beside the title, numbered dictionary definitions, and disabled unavailable dependent controls.
- Changed Light Novel Nyaa searches to Literature / Raw title searches, recognizes volume ranges, selects only the requested volume from batch torrents and removes the completed torrent while keeping that volume.
- Reworked Planning per-episode downloads as a persistent background job: local files are checked first, user-requested searches use the complete alias budget, and results appear only after the whole run finishes.
- Fixed Planning episode controls so the automatic-download action and per-episode status labels use stable, non-overlapping rows.
- Added an optional Jimaku key from GitHub Actions for the first 48 hours; it is not persisted to user config, and personal keys take priority.
- Added a persistent Job Center with cancellation, retry and history for Nyaa episode runs, OCR, audiobook STT and imports.
- Formalized video/subtitle/OCR/Ready transitions and records accepted state changes so scans cannot demote validated or user-controlled states.
- Added inflected-form pitch diagrams in the Light Novel reader, two-click Finish Volume, and a local Remove Finished action that does not reduce AniList progress.
- Added conservative LN/audiobook auto-linking, AniList identity propagation and Find LN on Nyaa from an audiobook.
- Added detailed user-scenario and algorithm documentation, including Jimaku trial and state-machine behavior.
- Made bitmap-subtitle states deterministic across library scans and exposes Enable OCR immediately when OCR is disabled.
- Audiobook Stop now pauses mpv before reading the final position, and the forward seek control is consistently +15 seconds.
- Added full-text inline Jiten pitch-accent diagrams to the Light Novel reader with an independent appearance toggle.
- Exposed automatic per-episode downloads directly on Planning anime cards when released episodes are available.
- Added cached acoustic speech timing using waveform energy and FFT spectral flux so paired-reading highlights pause between spoken phrases.
- Made Light Novel word-color choices visible as persistent swatches with their exact hex values.
- Added shared Jiten word-color presets and custom state colors for Light Novels, Manga and Visual Novels, plus mora-level pitch-accent diagrams in word cards.
- Simplified audiobook-analysis status to a percentage, isolated each STT run's temporary files, refreshed AniList automatically after credential changes, and moved playback controls into Advanced settings.
- Corrected the Jimaku account link, removed the technical API-reference shortcut, and omitted hours from countdowns longer than one day.
- Preserved expanded audiobook chapter lists during live polling, passed the configured ffmpeg location to MLX Whisper, and resumed incomplete audiobook STT immediately at app startup.
- Moved Planning search suggestions below existing entries, fixed the in-reader Names editor stacking, and consolidated paired-audio controls into the Light Novel toolbar.
- Audiobook STT now exposes live percentage progress in both the audiobook library and paired reader.
- Added step-by-step Jimaku and AniList credential guides with direct registration, account, developer and OAuth links.
- Added a manual in-app updater with app-bundle rollback: release installs download the matching GitHub Release ZIP and verify SHA-256, while development checkouts allow only clean fast-forward updates from the official origin.
- Enriched finished Planning cards with lazily loaded Jiten length, difficulty and, when a Jiten API key is configured, known-word coverage.
- Expanded the AniList character glossary with unambiguous Japanese first/last-name variants so selection translation preserves short character references more reliably.
- Clicking a linked manga cover now opens its AniList page while the rest of the series card continues to open the reader.
- Added cached Japanese STT alignment for linked Light Novels and audiobooks, including real chapter boundaries, word-level reader highlighting and synchronized seek/speed controls in the reader tray.
- EPUB reindexing now removes copyright, colophon and short author-metadata sections that are not reading chapters.
- Expanded manga text-region detection for vertical/stylized text and fixed empty hover stickers remaining after the pointer leaves.

## v0.7.2

- Reworked manga preparation into bubble-sized Apple Vision regions followed by MangaOCR crops, with persistent overlays and background Jiten/JPDB parsing.
- Stabilized bubble overlays across every page, close-on-leave behavior, zoomed-page scrolling and cached background preparation; manga actions and volume OCR now share the standard context menu.
- Added manga and Light Novel scoring from the cover context menu, plus AniList-backed Planning search suggestions.
- Expanded audiobooks with a scrubber, bookmarks, sleep timers, persisted speed, smart rewind, completion controls and lower-frequency position writes.
- Added chapter-aligned Light Novel/audiobook paired reading with audio playback, proportional passage highlighting and optional auto-scroll.
- AniList character names now form a cached translation glossary for online translation and the optional local LLM.
- Fixed context menus on Continue Watching cards while playback is starting/already open, and removed duplicate manga reader event registration.
- Re-selecting the active Light Novels or Manga tab no longer reloads and flashes the page.
- Added replaceable metadata caches for ffprobe and AniList lookups plus the isolated VN reader architecture for the 0.8 series.

## v0.7.1

- Split subtitle preparation into explicit discovery, normalization, alignment, validation and selection stages with persisted worker progress and leases.
- Subtitle upgrades now compare final validated alignment quality; container chapters help anchor opening/transition edits, and cached tiny Japanese STT is available only as a last resort.
- Local LLM subtitle checks are off by default. Backups redact credentials and retain current secrets during restore.
- Added categorized Settings, a shorter profile-based onboarding flow and a focused Home section for genuine user-action blockers. Activity remains intentionally hidden.
- Added initial CBZ/ZIP manga reading with lazy MangaOCR and audiobook playback with chapters and saved mpv position.
- Added versioned SQLite migrations, external UI modules, integration tests and public project/security/contribution documentation.

## v0.6.68



## Development and GitHub

Source development instructions are in [`DEVELOPMENT.md`](DEVELOPMENT.md).
Release/tag workflow is in [`RELEASING.md`](RELEASING.md).

GitHub Actions runs the full test suite in four deterministic macOS batches. Pushing a version tag such as `v0.6.68` builds and publishes the matching macOS release ZIP automatically.

## v0.6.68

- Restored constant-offset subtitle onset/activity hypotheses between rejected embedded-reference ALASS and audio/FFT fallback. Each plausible global shift is semantically rechecked at the shifted timestamps before it can be used.
- Exact AniList/Jimaku episode identity no longer overrides severe local timing failures such as full-range oscillation or large multi-window jumps. This prevents a known BLEACH absolute E43 DSNP/TVA case from accepting a globally wrong ffsubsync clock.
- Added regression coverage for BLEACH E43 and for constant-offset recovery before audio fallback.

## v0.6.62

- Fixed Jiten API routing to the current `/api/reader/*` endpoints and fail-fast behavior for deterministic 4xx responses.
- Light Novel import now supports multiple EPUB/TXT files and extracts embedded EPUB covers for WKWebView.
- Imported novels can search the full AniList NOVEL catalog and bind titles that were not already in the user's Planning list.
- AniList MANGA/NOVEL Planning entries are shown together with anime Planning.
- Moved all Light Novel configuration into Settings; renamed Watching to Anime and placed Light Novels directly below it.
- Kept Jiten chapter parsing cached by text hash while reducing unnecessary retry delays.

## v0.6.61

- Added the first Light Novels module: EPUB/TXT library, chapter reader, reading progress and persistent Jiten parse cache.
- Jiten `reader/parse` is used as the single tokenizer/parser; current + next chapter is the default prefetch policy with throttling/backoff.
- Added direct Jiten and JPDB API-token study actions, card-state CSS classes, furigana toggle and custom reader CSS.
- Added AniList NOVEL linking/status: opening Planning moves it to Current; finishing a volume updates `progressVolumes` and can complete the title. Strong title matches bind automatically.
- Added Nyaa literature search and optional next-volume auto-download to qBittorrent category `pudge-ln`; completed EPUB/TXT files are discovered automatically.


- Manual Refresh now waits for active maintenance instead of silently skipping, and searches missing Nyaa releases before long subtitle preparation.
- Restores prepared subtitle selections whose cache paths moved during the Anime MPV -> pudge rename.
- Fixes false `aligned_too_long` rejection when a movie subtitle already contains long sign/SFX cues.

## v0.6.58

- Renamed the product to **pudge** and added migration of the old default app/config/data/cache/library paths.
- Preserved the existing macOS bundle identifier so notification/folder permissions survive the visible rename; old Dock pins get a hidden compatibility app link and Dock refresh.
- Migrates legacy qBittorrent `anime-mpv` torrents to the `pudge` category without touching unrelated torrents.
- Repairs stale `catmahjong.mp4` → `Mahoutsukai no Yoru` associations even when an obsolete torrent hash survived in the episode row.
- Refresh/cleanup now recursively removes empty directories below the managed anime folder.

## v0.6.56

- Configurable mpv shortcuts now use key capture; app navigation stays standard/dynamic.
- Centralized product branding in `pudge/brand.env` with `rename_brand.py`.
- Fixed false local movie matches, relation alternative previews, conditional subtitle-upgrade settings, Library duration, and energy diagnostics scoping.

- Polychrome one-shot animation is 1.5x slower, no longer double-starts, and hover triggering survives card rerenders.
- Waiting-for-subtitles cards now resolve split-cour absolute episode numbers before choosing their status.
- Automatic watch completion is enabled by default and groups threshold + max-minutes controls under one toggle.
- Added configurable pudge and mpv shortcuts.
- Library uses singular `Episode:` for one local episode and displays relative split-cour numbering.
- Added opt-in low-overhead energy diagnostics logs under `~/Library/Logs`.
- Preferred resolution is now a standard-resolution selector with `Higher is better`.

## v0.6.54

- Removed the 15-second full UI-state rebuild that caused measured idle CPU spikes on both Watching and Settings.
- The existing lightweight ready watcher now checks two tiny SQLite invalidation counters in one call and only requests `get_state_fast()` when rendered data actually changed.
- UI-relevant database changes bump a cross-process `ui_state_version`; playback heartbeat updates are deliberately excluded so active mpv playback does not cause constant UI rebuilds.
- Ready notifications remain near-instant because the 1-second watcher is preserved, now without a separate 15-second polling loop.

## v0.6.53

- Reduced idle energy use on the Watching page: polychrome covers keep their foil appearance but no longer run permanent CSS animations. Motion is now a short one-shot when Watching becomes active, the window regains focus, or a cover is hovered.
- Background UI polling now uses cached storage data instead of recursively rescanning the video library every 15 seconds. Full storage usage is still refreshed during startup/manual Refresh.

## v0.6.52

- Why not ready / Preparation job diagnostics now follow the selected UI language. English UI translates legacy Russian prepare-job status lines at display time, including already persisted jobs, while technical markers such as `PREPARE_STATUS` remain unchanged.

## v0.6.51

- Continue Watching now identifies AniList movies explicitly and shows **Movie • resume at …** instead of **Episode ?**.

## v0.6.49

- Nyaa settings now include **Only trusted groups for automatic downloads**. It is off by default; when enabled, both automatic missing-episode downloads and automatic upgrades reject every uploader outside the trusted-groups list. Manual Find episode downloads remain available.
- mpv no longer shows the startup OSD announcing when AniList will count the episode; tracking still works silently and manual/status messages remain unchanged.
- Automatic Nyaa downloads now accept confirmed absolute-episode aliases even when torrent season numbering differs from AniList (for example BLEACH local episode 3 = absolute S17E43/E43).
- Exceptionally strong exact episode matches can auto-download from an unlisted uploader when the title/episode/size/seed evidence is strong enough, while weak untrusted matches remain blocked.
- Automatic refresh now searches the same five title aliases as Find episode, while retaining its wall-clock search budget.

## v0.6.47

- Opening the app now runs the same full local maintenance pipeline as manual Refresh: unresolved subtitle jobs are force-requeued and processed before missing-release and upgrade searches.
- AniList automatic progress now requires both the watched-percentage threshold and a configurable maximum number of minutes remaining. The default cap is 10 minutes, preventing long movies from being counted too early.

## v0.6.44

- OCR legacy provenance now recognizes cleaned playback SRTs from v10/v11/v12 and old final-pipeline manifests.
- When OCR is disabled, startup/maintenance immediately invalidates legacy OCR-derived SRTs instead of waiting for the setting toggle to happen again.
- Apple Vision OCR filters probable furigana/ruby rows spatially: small kana readings above larger base text are removed while normal same-size multiline dialogue is preserved.
- OCR cache generation was bumped, so previously OCRed bitmap subtitles are rebuilt with the new furigana cleanup when OCR is enabled.

## v0.6.42

- OCR-generated SRTs now keep explicit provenance. Turning OCR off immediately invalidates them and moves affected unwatched videos to `Waiting for text subs` when a bitmap source is still present.
- Settings changes that affect subtitle readiness return a reconciled UI state immediately and queue high-priority subtitle work without waiting for Refresh.
- Jimaku key/subtitle-folder changes immediately requeue unresolved subtitle jobs; watched-folder changes trigger an immediate library reconciliation.
- Foreground polling is faster near torrent completion (2 s at 98%+) and immediately processes high-priority subtitle jobs.
- Legacy OCR results from older releases are recognized from OCR/playback cache lineage and invalidated safely.


## v0.6.41

- Revalidate watched-folder auto-imports even after they reached `ready`, while preserving watched/resumable/torrent-managed rows and skipping destructive cleanup on AniList network errors.
- Future / `NOT_YET_RELEASED` anime can never appear in Ready/Waiting home sections; confirmed local files remain visible in Library.
- Library marks files imported from watched media folders explicitly.
- Regression guard for `catmahjong.mp4` falsely matching `Mahoutsukai no Yoru` through the short synonym `Mahoyo` under the historical 58% fuzzy fallback.

## v0.6.40

- Strict watched-folder matching rejects arbitrary local videos and removes unresolved false imports.
- External scans no longer fall back to permissive legacy fuzzy title matching.

- Hide `Why not ready` in the **Caught up** section.

## v0.6.37

- Settings: removed the long integration hint and compacted Watch queues so they no longer overflow narrow windows.
- Library: added multiple watched media folders and multiple subtitle folders. External video files are matched to AniList from the filename (including season numbers) and imported into Library automatically.
- AniList: added a setting to add imported anime when watched progress is recorded.
- Jimaku: transient DNS/connect failures are retried three times and can fall back to the last positive cached API response.
- Nyaa: releases matching the preferred resolution receive +10 additional score.
- Watch queues: hidden for not-yet-released anime, hidden when no next local episode exists, and the action shows the actual number of available episodes.
- Planned: removed `Why not ready`.
- Relation graph: recap/compilation/alternative movies are collapsed into a small `Alternative` shelf above their main adaptation instead of occupying full-size graph nodes.
- Rating roulette: changed to a bad→good red/orange→green/blue scale, so high scores are no longer red.
- Cleanup: removes the immediate anime directory after auto-delete when it is completely empty.
- Hyakkano S03E05: duplicated overlapping English SFX cues are ignored as cold-open anchors; the first Japanese dialogue is anchored to the first unique English dialogue while the post-opening timeline stays unchanged.
- Final pipeline cache bumped to v9; playback SRT generation bumped to v12 / validation generation 13.

## v0.6.36

- Piecewise-синхронизация больше не интерполирует cold-open offset через длинный разрыв без реплик; после opening/title card сразу используется следующий стабильный clock.
- Исправлен Hyakkano S03E05: первая реплика после 106-секундного разрыва получает +0.35 с вместо ложных +2.26 с.
- Старые playback-SRT автоматически переподготавливаются.


- Исправлена групповая синхронизация `1–3 ↔ 1–3`: английская длинная реплика больше не растягивает и не сжимает внутренние границы японских SRT-cues.
- Split/merge-группа теперь передаёт только общий локальный сдвиг, сохраняя исходные длительности и паузы японских субтитров.
- Добавлен quality gate: коррекция отклоняется, если почти не улучшает timing activity или заметно меняет длительности cues.
- Ранее подготовленные playback-SRT автоматически ставятся на повторную проверку.

## v0.6.34

- Increased the native SRT format bonus from 12 to 16 points.
- SRT now has a 10-point advantage over ASS and an 11-point advantage over SSA before timing-quality evaluation.
- Materially better ASS/SSA candidates still win through the embedded-reference activity check; invalid SRT files remain rejected.


## v0.6.33

- SRT now wins embedded-reference candidate selection when activity differs by no more than 0.005.
- ASS/SSA still wins when it is materially better or the SRT structure is invalid.
- Final pipeline cache bumped to v6 so existing episodes are re-evaluated with the new rule.

## v0.6.32

- Added group-aware embedded-reference timing refinement for subtitle tracks whose translations split dialogue differently (`1–3 Japanese cues ↔ 1–3 English cues`).
- The refinement uses the stable pre-piecewise ALASS clock for matching, so a false local piecewise shift cannot hide the correct cue groups.
- Small cue boundaries are distributed proportionally inside the matched reference phrase while preserving cue order and limiting every correction to 1.6 seconds.
- Grand Blue S03E05 regression data matches 202 dialogue groups, including 90 split/merge groups, covering about 96% of dialogue cues.
- Existing generated playback subtitles are automatically requeued once after upgrading.

## v0.6.31

- Continue Watching is rendered above New episodes ready and Completed & ready.
- Added a regression test that locks the home-page section order.
- Activity now uses anime titles for torrent downloads, subtitle jobs, and release-upgrade history; technical release filenames are secondary details instead of AniList IDs.

## v0.6.30

- Continue Watching now has priority over Ready sections whenever a mid-episode position exists.
- Closing MPV mid-episode immediately refreshes the home page and exposes the saved resume point.
- Normal completion and AniList tracking behavior are unchanged.

## v0.6.29

- Added a managed aria2c torrent backend. qBittorrent remains preferred when enabled; otherwise pudge starts a private local aria2c RPC process and keeps automatic downloads, progress monitoring, completion detection, cleanup, and release upgrades working.
- The macOS installer installs aria2 automatically, records its absolute path for Finder and LaunchAgent environments, and enables the fallback for existing installations.
- Settings and Activity now describe the active torrent backend and provide a direct aria2 connection test.
- First Experience now explicitly explains that qBittorrent is optional: aria2 still downloads automatically, while qBittorrent provides richer categories, tags, and torrent-management controls.
- Release upgrades are now configurable and visible in Activity, with manual checks, score thresholds, cooldowns, and retained upgrade history.
- Japanese subtitle selections now have history and safe automatic upgrades: the current subtitle is backed up and replaced only when the new candidate clears the configured score gain. Manual subtitle selections are protected.
- Added smart Watch queues for the next local episodes or an entire ready franchise in watch order. The next item launches only after the current episode is actually marked watched.
- Added full application backup and restore for settings, database state, mappings, graphs, queues, histories, and generated cached subtitles. Video and torrent payloads are intentionally excluded.
- Keeps the startup polychrome and compact/full graph refresh fixes from v0.6.26.
## Changes

- Subtitle preparation now always retries after the configured subtitle-check interval for missing subtitles, rejected candidates, missing 7-Zip, OCR/synchronization failures, and other local conditions. Progressive one-hour/six-hour backoff is reserved for DNS, connection, HTTP 429/5xx, and other external network failures. Existing long pending delays are reset once after upgrading.
- Fixed the installer version check: it now derives the expected version from the bundled wheel instead of using a hard-coded release number.
- Includes the experimental Activity, diagnostics, Subtitle Inbox, manual subtitle selection, OCR quality checks, and Repair Library features from v0.6.17.

- Optional background OCR for image subtitles. Text subtitles always have priority; when only PGS/SUP is available, Apple Vision converts it to cached SRT during subtitle preparation, before playback.
- OCR-generated SRT is handled as a normal ready subtitle, so the episode moves to Ready and receives the polychrome card effect.
- Cached Watch Order remains fully hidden until its graph and cover images are ready for the first visible frame. Diagnostic timing events are written to the application log.
- AniList `RELATED` relations are excluded from graph construction, compact relations, and old cached graph rendering.
- Library episode columns expand per entry so labels such as `Episode 11` do not wrap.
- Clicking a Library card opens its AniList page; episode controls keep their existing actions.
- Polychrome animations are force-restarted when the app or Watching tab becomes active.
- `Cmd+1`, `Cmd+2`, `Cmd+3`, and `Cmd+4` switch to Watching, Planning, Library, and Settings.
- The random score picker is now a one-second decelerating fortune wheel with larger middle-score sectors and no extra cooldown.
- Includes the complete source tree and test suite.

## Install

```bash
cd ~/Downloads
rm -rf anime-mpv
unzip anime-mpv-macos-v0.6.44.zip
cd anime-mpv
./install.sh
```

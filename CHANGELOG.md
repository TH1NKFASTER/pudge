# Pudge changelog

## Unreleased

## v0.7.28 — Statistics, study workflow, library scheduling, and Manga OCR

Range: [v0.7.27 → v0.7.28](https://github.com/TH1NKFASTER/pudge/compare/v0.7.27...v0.7.28).

These notes describe the source changes relative to v0.7.27. They do not imply that every feature has been validated on a release build.

### Statistics and cross-device history
- Added a Statistics page with overview, per-work summaries, an activity calendar, and a journal. Filter by period, content type, or work; edit recorded activity, add manual entries, export CSV, and reset statistics history.
- Introduced an append-only consumption ledger with stable media identities, sessions, activity events, correction records, and device-scoped event sequencing. Anime, manga, light novels, audiobooks, and visual novels contribute supported activity observations.
- Report elapsed active time with overlap-aware aggregation; distinguish first-time and repeat consumption where native volume data is available. Manga page counts and light-novel text ranges are recorded separately from time. Native volume coverage begins when tracking is enabled; historical progress is not represented as measured consumption.
- Extended mobile synchronization to replay consumption records alongside the existing progress protocol, including conflict detection and history-reset epochs.

### Reading and review
- Added an optional per-episode review gate for playback, with configurable review count, a dedicated review-card overlay, progress persisted per account and episode, and prefetching of existing Jiten review cards. It is disabled by default.
- Require evidence of an existing reviewable card before strict-gate submission; never silently count or retry a review whose remote outcome is unknown. JPDB capability discovery is present, but strict playback gating is not enabled for JPDB because the required review-enumeration and reconciliation guarantees are unproven.
- Refresh Jiten card/deck state independently of cached text parsing, prefetch state for the parsed local corpus, and offer optional highlighting of eligible N+1 words in the readers.
- When reviewing a word absent from the selected Jiten study decks, support adding it to the selected deck with a contextual sentence; manga study can also attach the source page. Revalidate membership before mutation to avoid duplicate additions.
- Improved reader text and ruby interactions, study-card context selection, and recovery of chapter images/covers.

### Library, playback, and personal scheduling
- Group related manga and light-novel series into franchise shelves with expand/collapse and preserved scroll position. Improve local cover previews, including source resolution and retry behavior.
- Added personal weekly episode-release schedules with explicit time zones and rewatch support. Future episodes stay locked until their scheduled release; notifications and retention respect the personal schedule.
- Adjusted playback admission, session tracking, and UI presentation for the review gate and personal schedule.

### Power and background performance
- Added manual energy-saving mode and automatic low-battery policy with hysteresis. Background work reports its reason for waiting and cooperates with the policy.
- Reduced redundant refresh/poll activity, refined task admission/retirement, and improved runtime diagnostics around application sessions and background work.

### Manga OCR and quality tools
- Extended layout-aware OCR recovery for short/partial columns, missed main text alongside ruby, trailing glyphs, punctuation, bold or stylized text, and large sound effects. Added bounded cross-candidate consensus and geometry/artwork guards to limit invented text, duplicate regions, and unintended merges.
- Improved word/ruby hitboxes and reader integration. Added regression tests for missing characters, short text near furigana, and text-region boundary recovery.
- Added benchmarking tools and documentation for JMangaBench and Manga109, a review-diff utility, and a 40-page One Piece golden-text test corpus with comparison tooling. Review the golden-data licensing and redistribution scope before publishing it in a public repository.

### Tests and release preparation
- Expanded unit, integration, and regression coverage across OCR, the study workflow, scheduling, statistics, synchronization, energy policy, and reader UI. Added benchmark Makefile targets and improved test-batch handling.
- This is a source-diff summary, not a test-pass report; complete local and macOS release validation must be run separately.

## v0.7.27 — Manga OCR, runtime hardening, and more accurate synchronization

Range: [v0.7.26 → v0.7.27](https://github.com/TH1NKFASTER/pudge/compare/v0.7.26...v0.7.27).

This changelog is derived from the actual Git diff against tag `v0.7.26`, not from chat memory.

### Manga OCR and text geometry
- Significantly expanded layout-aware OCR with candidates from binary components, vertical-line structure, raw layout, and dark text blocks, merged with OCR output.
- Vertical columns and complex text blocks now use stripe geometry, gaps, expected glyph counts, kinsoku boundaries, and neighboring layout candidates instead of treating one OCR rectangle as one line.
- Added multiple recovery stages for missed text: raw-gap augmentation, contextual recall, post-cluster recall, and bounded local consensus for weak regions.
- Tightened filters for ruby/noise/art regions, duplicate text, and accidental merges between neighboring columns, including dedicated handling for small kana, punctuation, wide/cropped SFX, and short dark rectangles.
- Apple Vision now exposes range-level geometry. Segments marked `accurate-range-boxes-v2` use actual character bounds instead of evenly splitting the whole OCR box.
- The OCR artifact moved to schema `pudge-manga-ocr-v3` with explicit status and geometry-source metadata; old v1/v2 artifacts are upgraded when read.
- Repeated Latin page headings can be repaired by cross-page consensus while preserving existing segment geometry rather than inventing glyph boxes.
- The region-cache key moved to `v59-layout-token-geometry` so incompatible older results are not reused as current output.

### Manga runtime and reader
- Volume OCR now has generation/revision ownership plus a source fingerprint, preventing stale workers from publishing after rebuilds, source replacement, or a newer OCR generation.
- Page status, region cache, and OCR artifacts are written atomically and incrementally; a completed page can be published without unsafe whole-state rewrites.
- Added explicit volume OCR progress/state, including current page, prepared/processed pages, failures, and yielding to higher-priority interactive work.
- Merely opening a volume no longer has to start full-volume batch OCR. Dedicated `OCR volume` / `Rebuild OCR` actions control full processing.
- The currently visible page can receive foreground OCR without waiting for the background volume pass; background OCR yields through the shared scheduler.
- The reader now has a selectable text layer. Word hitboxes come from OCR segments and accurate geometry, while text selection and whole-bubble selection are separate from study-card clicks.
- Added region/word/hitbox debug overlays plus `scripts/replay_manga_surface_map.js` for replaying surface maps without reproducing the whole reader session manually.
- Added quick page selection, improved cursor/viewport anchoring during zoom, and stabilized page virtualization.
- `Reset reading progress` resets the reading position without deleting AniList linkage or library metadata and is available for both individual volumes and series.

### Subtitles and timeline alignment
- Timeline alignment now handles weak false opening excursions: a short clock outlier can be removed when stronger clocks on both sides and independent long-gap evidence agree.
- After that repair, the transition between the two clocks can be anchored inside the proven opening gap so monotonic repair cannot drag the post-OP offset backward through real dialogue.
- Added a narrow holdout-based recenter for the pre-OP plateau: high-quality independent windows may select the neighboring whole-second clock while leaving a stable post-OP plateau untouched.
- Expanded the embedded opening scaffold for cases where a large gap-supported ALASS transition collapses the opening gap; the relative ALASS clock is now handled separately from local refinement.
- Added more guards against degrading a strong embedded timeline and richer diagnostics for opening-clock recovery and rejection decisions.
- The timeline algorithm key is now `timeline-v6.14-opening-preclock-holdout`, so older cached timing results are not treated as equivalent.
- Subtitle jobs gained `generation` and `owner_token`; defer/reset/postpone/ready/delete operations and state publication can be bound to the owning attempt.
- A stale worker after Fresh/rebuild or a new claim can no longer overwrite the state of the newer attempt; the manager validates ownership before publishing results.
- Cross-path subtitle-history recovery now requires a sampled content fingerprint of the video. Legacy history without a fingerprint is accepted only for the exact original path.
- Manual Fresh is scheduled as priority user work and can preempt/yield heavy background OCR.

### Visual Novel reader
- macOS selected-window capture now uses explicit ScreenCaptureKit (`SCShareableContent` / `SCScreenshotManager`) instead of desktop-capture behavior.
- Real ScreenCaptureKit errors are authoritative for permission/backend state; a false TCC preflight result alone no longer blocks capture attempts.
- Added configurable dialogue ROI plus a separate speaker region above it, with several ready-made dialogue-area presets in the UI.
- OCR scores the dialogue ROI first and uses a bounded full-frame fallback only when it produces better text; speaker OCR runs separately and only with sufficient contrast.
- Frame fingerprints are computed over the working ROIs, reducing unnecessary full OCR on unchanged frames while still allowing retries after empty results.
- Capture/session generation prevents late callbacks from an old run from mutating the current transcript/UI; vanished windows and permission/backend failures receive distinct recovery codes/actions.
- Asynchronous text parsing in the web UI is also bound to the current render/session generation so stale responses cannot replace newer dialogue.

### Light Novels and audiobooks
- Jiten parsing is single-flight by text hash: identical concurrent requests share one execution and result/cache entry.
- Parse admission limits concurrency, prioritizes interactive reading over background parsing, and spaces requests over time.
- Light Novel SQLite access gained an explicit context-managed connection lifecycle with commit/rollback/close while keeping raw `_connect()` compatibility for existing callers.
- Paired-reading alignment now removes short rejoining local-rate outliers when they imply unrealistic local speed and then return to the established clock.
- Audiobook playback gained a unique session identity and per-run IPC socket. A stale monitor can no longer stop playback, delete the socket, or persist position for a newer session.
- Audiobook-service `close()` tracks worker threads/processes, cancels background work, and reports workers that fail to terminate instead of silently exiting.
- LN↔audiobook alignment generation now has generation/cancellation ownership and unique temporary outputs, preventing stale attempts from publishing after a newer run or cancellation.

### State, backups, shutdown, and downloads
- Backup restore now fully extracts and validates into staging first, including SQLite integrity checks, before committing live files.
- Rollback copies of the database, config, and restored cache files are created before replacement; commit failures restore them.
- During restore, the web runtime stops owned OCR/audiobook/supervisor work, blocks conflicting maintenance threads, and recreates runtime services after commit or rollback.
- `TaskSupervisor` gained suspend/resume/quiesce/shutdown with waiting for owned tasks/processes; clean shutdown is not reported while owned work remains alive.
- Download intents now use atomic read-modify-write mutation with revision semantics so late completion from an older generation cannot close a newer intent.
- Added shared `process_utils.pid_alive` to avoid divergent PID-liveness logic across subsystems.
- Disabling global torrent traffic now presents unfinished downloads as paused; re-enabling first resumes existing backend jobs instead of requiring a new torrent.
- Home/compact status distinguishes paused downloads from active traffic, so ETA/status no longer imply progress while torrent traffic is disabled.
- Full application shutdown waits for manga OCR, audiobook workers, and TaskSupervisor. Red close/Cmd+W still hides the window, while detached playback can continue independently of the GUI process session.

### Test runner and CI
- Reworked the batch runner so every test file executes in an isolated subprocess with its own `PUDGE_HOME`, temp directory, and pytest basetemp.
- A failing file no longer hides results from later files in the batch; stdout/stderr, JUnit, runtime log, and JSON summary are retained per test file.
- Added per-file timeouts and machine-readable totals for passed/failed/skipped/xfail/errors/timeouts.
- Batch-result fingerprints now include source/test-tree contents, dependency versions, Git commit, and tracked working-tree state, preventing result reuse across different implementations.
- CI always uploads batch-result artifacts, and the Makefile creates distinct result directories for parallel batches.
- The diff adds a large regression suite for Manga OCR/geometry, subtitle timing, lifecycle ownership, backup restore, torrent state, LN/audio alignment, and Visual Novel capture. The presence of tests in the diff is not a claim that they passed on a particular machine.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.26...v0.7.27).


## v0.7.26 — Durable subtitles, audiobooks, and paired-reading accuracy

Range: [v0.7.25 → v0.7.26](https://github.com/TH1NKFASTER/pudge/compare/v0.7.25...v0.7.26). Status: published GitHub Release.

### Ready state and subtitle queue
- Accepted prepared text subtitles are persisted in a managed durable directory outside disposable macOS caches; existing valid files migrate into the new storage.
- Refresh and integrity checks detect missing selected-subtitle files, clear false Ready state, and start recovery. History is used only when a usable prepared result still exists.
- Losing a previously accepted result forces a full rebuild even when the candidate set is unchanged; legacy records without the new force-processing marker can still recover.
- The queue prefers each title’s next unwatched or first blocking episode rather than later episodes with a higher internal priority.
- A later downloaded episode no longer hides a problem on the nearest unwatched episode in Ready/Completed. Heavy work waits for active playback and resumes afterward.
- Duplicate completed torrent records no longer repeatedly register the same local video and create endless subtitle jobs.
- Added a per-title setting for requiring Japanese subtitles without deleting already present files. Resetting progress no longer automatically moves a new/planned title into Watching.

### Synchronization and sources
- Expanded episode-start refinement, sparse pre-OP lines, and post-OP timing reacquisition using agreement between STT text, acoustic onsets, and the embedded track.
- Strong embedded timelines are protected from degrading STT results; weak isolated late transitions and unsupported edge jumps are suppressed.
- Real simultaneous cues are preserved when preparing playback SRTs; touching sequential cues still account for mpv rendering behavior.
- Added a narrow correction for the known incomplete Nanako source for Bleach S01E45. It requires exact identity and a verified embedded-reference structure; this is source-specific repair, not general missing-text generation.
- Improved episode-number recognition in BDSUP archives and recovery of prepared results from Jimaku history.
- STT selects Japanese audio and rejects ambiguous dual-audio; caches depend less on filesystem location for identical content. Added MLX memory limits and transient-error handling.
- Nyaa search gained stricter season, related-title, title-plausibility, and size checks, plus broader proxy handling and torrent-file metadata retrieval.

### LN and paired listening
- Reworked chapter-start recovery using spoken chapter number/title, introductory boilerplate, weak early anchors, and regions where narration has not yet reached the main text.
- Added precise local chapter-onset recognition, readings from parsed text, and phonetic anchors. When precise evidence is insufficient, bounded transitions across Japanese reading units are used.
- Fixed highlight movement while paused, recovery after start/seek, premature highlighting of opening words, and jumps in sparse regions.
- Reworked runtime position indices and active-fragment scrolling; refinement parsing can update alignment without repeating compatible work from scratch.
- Furigana and word cards now cooperate with paired reading, vertical mode, and native selection. Text without a Jiten result gets a generic fallback card and reading interpolation.
- EPUB import better handles tables of contents, fragment links, repeated headings, technical sections, and image pages, including ttsu-like markup.
- Images no longer add false textual length to the audio scale. Added inline rendering, blur, and opening images without unintentionally changing reading position.
- Refined Read up to here, Play from here, card sizing/re-closing, search, Escape behavior, selection, and bulk library actions.

### Audiobook library and downloads
- Audiobooks are grouped by series and volume with Volume N labels; added series/volume selection for bulk deletion and display of the nearest unfinished volumes.
- Collection import distinguishes volume, disc, and chapter directories and no longer lets a nested chapter number replace the volume number.
- Added audiobook search for LN, including works inside compilation Nyaa torrents; file selection uses series path and volume number.
- Selective volume downloads support qBittorrent and aria2. Selection manifests allow import recovery after restart and prevent linking a volume to the wrong series.
- Search and download views use local information first and enrich it with network metadata afterward. Cover and audio-file metadata extraction runs in the background.
- Audiobook STT is now a queued process with partial checkpoints and reuse of completed fragments; stale workers and leftover audio players are cleaned up.

### Audiobook generation and LLM
- Added optional audiobook generation from LN using managed Irodori-TTS or a configured compatible service, including Irodori install/check support and logs.
- Long chapters are split into bounded requests and parts are merged; progress is based on processed text and persisted in a manifest.
- Added pause, resume, cancel, and regenerate actions while preserving visible failed state for retry.
- Added speaker annotation and character voices with stable profiles by series and AniList character identity.
- Annotation can be exported as an archive across local series volumes and imported back without requiring an LLM; source-text hashes prevent applying annotations to changed chapters.
- The LLM client gained OpenAI-compatible API support, configurable reasoning effort, incompatible-parameter handling, and clearer provider errors.
- Added explicit Jiten data refresh and preparation of chapters not yet parsed.

### Download control and installation
- The global torrent toggle is synchronized through protected config writes, with optimistic UI confirmation and a fallback local HTTP control path.
- Recovery of damaged/orphaned downloads clears completed intents before reselection; disabled torrent traffic leaves jobs waiting instead of downloading immediately.
- Empty tasks for already watched and deleted media are removed while unconfirmed unwatched jobs are preserved.
- Installation from current sources supports Git worktrees and explicit `PUDGE_BUILD_CURRENT_TREE`; stale `build/lib` output is isolated during builds.
- The package is installed without leftovers from the previous version and then byte-compared against the wheel, exposing stale runtime files even when the version number is unchanged.
- Backups include the new durable prepared-subtitle directory; Apple Vision OCR uses a scoped autorelease pool.

### Verification tools
- Added a subtitle benchmark CLI and corpus using an embedded Japanese track as ground truth, with timing-error metrics, segment reports, and replay of saved cases.
- Added scripts for algorithm comparison and ten levels of STT influence with checkpoints, partial results, and exported comparisons.
- Stress/benchmark media is isolated from normal library scanning; source-identity and previously acquired-case recovery checks were expanded.
- Updated documentation and many regression tests. The presence of these tests in the diff is not a claim that they were executed successfully while preparing this changelog.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.25...v0.7.26) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/744e8b3f0c8ebb9bf738fbe9af69b87bfd58460f).


## v0.7.25 — Window lifecycle, durable Ready state, and OCR memory

Range: [v0.7.24 → v0.7.25](https://github.com/TH1NKFASTER/pudge/compare/v0.7.24...v0.7.25). Status: published GitHub Release.

### macOS and interface
- The red close button and Cmd+W hide the window while keeping the session alive; reopening from the Dock restores it, while Cmd+Q remains a full quit.
- Added a macOS delegate proxy for termination/reopen behavior and fixed a PyObjC crash path when closing the window.
- Initial Home refresh now combines AniList synchronization and maintenance, reducing intermediate card jumps between sections.
- Refined Planning, global search, Cmd+F, local-media highlighting rules, and readiness labels.

### Subtitles and OCR
- Background subtitle upgrades preserve an already confirmed Ready state; added recovery for records downgraded by stale upgrade jobs.
- Prepared bitmap OCR can be used for playback but counts as Ready only when the `ocr_counts_as_ready` policy allows it.
- PGS/SUP decoding now streams compositions instead of keeping all images in memory.
- Added OCR-worker RSS monitoring and diagnostic progress; heavy library scanning is coordinated with OCR through the scheduler.
- Added persistent Jimaku unpack manifests and reuse of bitmap/OCR outputs, reducing repeated unpacking, synchronization, and recognition.

### Library, audio, and resources
- Added a fingerprint cache for library scans so unchanged roots do not require another full traversal. Removed duplicate scanning during one manual Refresh.
- Improved recovery of aria2 `paused 0/0` records and episode ownership checks so stale rows do not assign a neighboring episode’s file.
- Audiobook bookmarks gained rename, reorder, and post-deletion restoration; source actions and cover preview were added.
- CPU/RSS diagnostics are split by process role and include 5-, 15-, and 60-minute summaries.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.24...v0.7.25) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/58bda4bfd565703be7777bc3a04cb64707578a9a).


## v0.7.24 — Unified search and series-level libraries

Range: [v0.7.23 → v0.7.24](https://github.com/TH1NKFASTER/pudge/compare/v0.7.23...v0.7.24). Status: published GitHub Release.

### Search and Home
- Added global search across local media with normalized titles and AniList aliases, plus recently viewed history.
- Sidebar context actions can search for new subtitles/releases, refresh AniList, and scan local files.
- Managed local episodes are reconciled with already reached AniList progress so watched content does not remain Ready.

### Manga and LN
- Libraries were reworked around series/volume selection, context actions, Jiten metadata, and AniList ratings.
- Added recursive manga-image import with folder grouping, normalized volume numbers, and nested Manga-Zip layout handling.
- Improved physical spread handling and preservation of original page names for later imports.
- Page loading/prefetching is separate from marking content read: forward progress updates reading progress and the last displayed page completes the volume.
- Tooltips show read pages out of the total; library redraw and series scrolling were stabilized.
- LN/Manga selection is cleared when leaving the section; LN preparation always covers the current and next chapter.

### Synchronization and diagnostics
- Extended pre-OP timing-gap refinement using embedded lines while preserving the stable main portion of the episode.
- Low-frequency macOS energy diagnostics now enable automatically with a 30-second sampling interval.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.23...v0.7.24) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/4276286dcdef1f81af6d3de499e46448bfa8fcb7).


## v0.7.23 — Progress reconciliation, Safe Mode, and technical stabilization

Range: [v0.7.22 → v0.7.23](https://github.com/TH1NKFASTER/pudge/compare/v0.7.22...v0.7.23). Status: published GitHub Release.

### Progress and playback
- Ready and Continue Watching are reconciled with AniList progress so episodes already watched on another device do not reappear as next on the Mac or companion.
- Stale mobile progress cannot un-complete an episode; the companion refreshes the library when returning to the foreground.
- Added viewing/resolving mobile-sync conflicts, bounded event history, and revoking a device’s stream access.
- Compatible companion video may be served without transcoding; streaming-cache cleanup and reuse were improved.
- Fixed LN highlight position after ±5s and ±15s seeks.

### Sources and recovery
- Jimaku search now considers exact AniList/title matches beyond the first four entries when duplicates hide the desired source.
- Added Safe Mode after abnormal termination, with database diagnostics and backup access; a planned update is not treated as a crash.
- Safe Mode disables download monitoring and subtitle preparation and offers restart into normal mode.
- Added a two-stage uninstaller that computes Pudge-owned paths. External media folders and third-party applications are excluded from its plan.

### Data and internal services
- Added shared identity, diagnostics, cache-artifact accounting, and background task/process control services; Job Center checkpoints were expanded.
- A backup is created before database migration, and migration/data-link execution was reworked.
- Supported secrets can now be stored in macOS Keychain for standard user configuration. Environment variables retain priority; configuration fallback remains when storage is unavailable.
- Added secret masking in UI and diagnostic cleanup, versioned state snapshots, and dedicated controllers for several web operations.
- The scheduler considers resource, power, and thermal constraints; failure of an auxiliary resource probe must not stop core work.
- Added a dependency lockfile, release-metadata/config-example consistency checks, property/performance tests, and torrent-history analysis tools.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.22...v0.7.23) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/d5fb1c3907f39a7a917ada1a8fec550dea912df8).


## v0.7.22 — Recovering ready subtitles without requiring ffmpeg

Range: [v0.7.21 → v0.7.22](https://github.com/TH1NKFASTER/pudge/compare/v0.7.21...v0.7.22). Status: published GitHub Release.

### Companion
- Recovering an already existing SRT/VTT no longer requires ffmpeg merely to resolve its source.
- When conversion or extraction is actually needed, Pudge uses the discovered or configured ffmpeg path; those operations still require a converter.

### Development
- The Makefile now prefers `.venv-test/bin/python` when available and otherwise falls back to `python3`.
- Updated release-command and runtime-recovery checks along with version metadata.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.21...v0.7.22) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/de0d9d2fc4e718b3b36091f230d08df8c2286e4f).


## v0.7.21 — Browser companion and anime playback

Range: [v0.7.20 → v0.7.21](https://github.com/TH1NKFASTER/pudge/compare/v0.7.20...v0.7.21). Status: tag without a published GitHub Release.

### Companion
- Added an installable browser companion UI: landing page, manifest, service worker, and resources packaged with the app.
- The library groups content by series and can open LN, manga pages, and audiobooks, including covers and a hidden-image placeholder.
- Added enabling the local server from settings, LAN-address discovery, and access management for connected devices.
- Anime is served as HLS with ffmpeg preparation, job state, caching, and temporary access tickets for segments.
- Subtitles are converted to WebVTT; the web player gained text interaction and local progress exchange.

### Recovery and release
- Added a shared subtitle resolver that checks the current path, selection history, and embedded tracks when recovering playback.
- Refined subtitle path/permission handling and runtime-state reset.
- Added a release-preparation script that validates version, Git state, and tags; release and mobile-protocol documentation was updated.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.20...v0.7.21) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/a3e652c7e30e8036f40ff406cc7e34af95a9c72f).


## v0.7.20 — Media libraries, LN highlighting, and the mobile-sync foundation

Range: [v0.7.19 → v0.7.20](https://github.com/TH1NKFASTER/pudge/compare/v0.7.19...v0.7.20). Status: published GitHub Release.

### Import and library
- Added shared drag-and-drop import and watching of configured media folders with source-change detection.
- Removed LN sources are remembered so automatic scans do not immediately re-add them. Batch actions, series deletion, and library-view restoration were expanded.
- LN covers moved from large inline fields to file-backed storage, with migration and lighter card queries.
- Improved Japanese LN-source detection, background AniList linking, and Jiten data retrieval for series and individual volumes.
- Anime gained additional Shana/SubsPlease search sources and local release history; completed-download reconciliation with real files was improved.

### LN and audiobooks
- Reworked the highlight scale so dense text anchors, Japanese reading-unit weights, punctuation, and speech pauses all contribute to position.
- Added alignment quality reports, reprocessing, and paired-reading trace export.
- Pause, stop, and time jumps are faster; audio control is coordinated between library and reader, while audiobooks use isolated mpv control.
- Fixed multi-file audiobook transitions, position restoration, word skipping after seek, and seek-arrow behavior.
- Refined furigana toggling, reserved layout space, and the point at which furigana hides during listening.

### Manga and subtitles
- Added a versioned manga OCR artifact containing normalized pages, regions, and geometry; incompatible old cache results are cleared.
- Expanded direct text overlays, vertical-text detection, text selection, and Jiten-card opening. OCR diagnostics can export backend/frontend geometry.
- Subtitle priority now accounts for language in both content and filename; removal of Chinese lines from bilingual CJK subtitles was improved.
- Refined post-OP shift reacquisition and protected strong embedded timelines from contradictory speech results.

### Mobile sync and resources
- Added the companion server foundation: pairing, device tokens, access revocation, library snapshots, a change journal, and progress exchange for anime, manga, LN, and audiobooks.
- Added the protocol HTTP server and paired-reading link storage. The full browser companion arrives in the next tag.
- Improved global torrent-traffic control, managed aria2 shutdown, and coordination between the background agent and the active app session.
- Added cleanup for temporary audio cache and diagnostic logs, refreshed selection controls, and runtime-diagnostic export.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.19...v0.7.20) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/1b51114b43403d93229570fea0d63f0941545fff).


## v0.7.19 — Pudge dialogs and JitenMPV permissions

Range: [v0.7.18 → v0.7.19](https://github.com/TH1NKFASTER/pudge/compare/v0.7.18...v0.7.19). Status: published GitHub Release.

### Interface
- Hovering an audiobook chapter highlights its range on the timeline.
- Added an asynchronous branded confirmation dialog with the Pudge logo; native `confirm` calls were replaced in the web UI.
- Native Cocoa dialogs now use the Pudge icon; the pywebview requirement was updated.

### JitenMPV on macOS
- Onboarding and settings now explain the Developer Tools permissions required for Pudge and mpv.
- The app can open the relevant System Settings pane, and completing the step is persisted in configuration.
- Starting playback without confirmed permission records a clear diagnostic warning.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.18...v0.7.19) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/9cb579be83b10cb69065ff94d3176daf8c534107).


## v0.7.18 — Unified states, heavy-work coordination, and episode-start protection

Range: [v0.7.17 → v0.7.18](https://github.com/TH1NKFASTER/pudge/compare/v0.7.17...v0.7.18). Status: tag without a published GitHub Release.

### Architecture and downloads
- Seasonal/absolute episode-number resolution moved into `episode_numbering.py` and is shared by the CLI, manager, and UI.
- Episode-card state now comes from the shared `presentation_state` layer; a ready local file should not be hidden by stale transport state.
- Added a download-intent journal recording candidate, backend, selection progress, and winner. aria2 candidate switching and queue-priority control were expanded.
- Destructive switching to another release is restricted once downloaded bytes exist.
- Added a shared heavy-work scheduler with interprocess locking. New OCR/STT/alignment jobs wait for resources and for priority playback to finish.

### Subtitles
- Ambiguous large episode-start corrections are sent to Japanese-speech verification; stable timelines retain the fast path.
- Added limits for dangerous ALASS/STT maps, especially when multiple large jumps could move dialogue into the OP.
- Opening lines are matched to STT sequentially so one speech segment cannot be reused for multiple cues.
- Pre/post-OP refinement is reconciled with the embedded reference; added idempotence, order, duration, shift-invariance, and noise-stability checks.
- Rechecking for improved subtitles uses increasing backoff after completed checks.

### LN, settings, and release
- LN progress is based on character count rather than chapter fraction; the tooltip shows exact values.
- Paired-reading highlighting no longer pre-highlights the next word while paused. Default styling and selected-theme persistence were refined.
- Policy/playback settings were simplified and unavailable controls now explain why. MangaOCR is included in initial dependency installation.
- Release builds export dependencies through uv with hashes; Python and JavaScript syntax checks were expanded across app sources and scripts.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.17...v0.7.18) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/04a0cfc373ffa32673b64b8ec058ff01ba5fb35f).


## v0.7.17 — Removing stale downloads and numbering through AniList relations

Range: [v0.7.16 → v0.7.17](https://github.com/TH1NKFASTER/pudge/compare/v0.7.16...v0.7.17). Status: published GitHub Release.

### aria2 and Home
- Empty recovery magnet helper jobs are removed when the exact video file for that download already exists locally. Without a confirmed file, the helper job is preserved.
- Metadata-only recovery prefers magnets with a more complete tracker set; zero-size jobs participate in stall detection.
- Seed-only jobs no longer consume active-download slots. Removing stale legacy info-hash entries tolerates a missing job but does not hide real client errors.
- A locally ready episode takes precedence over stale transport state on Home; states that require enabling OCR survive intermediate refreshes.
- Compact cards keep percentage and ETA while detailed network metrics move to the download center.

### Subtitles and AniList
- The final path returned by subtitle preparation is treated as the authoritative selection result.
- Absolute-numbering offsets now use the complete cached franchise graph, including intermediate OVA/bridge entries. Stale partial estimates can be refreshed.
- The internal minimum release-selection score is hidden from user settings.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.16...v0.7.17) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/8c944758d0e0bc6b9a617da0b2abcd2fbade5716).


## v0.7.16 — Torrent backend control and translation in mpv

Range: [v0.7.15 → v0.7.16](https://github.com/TH1NKFASTER/pudge/compare/v0.7.15...v0.7.16). Status: published GitHub Release.

### Downloads
- qBittorrent and aria2 are handled concurrently: actions route to the owning backend and global torrent-traffic control was added.
- Added manual and automatic recovery for aria2 downloads with no live peers while preserving verified partial data.
- aria2 verification state is separate from file readiness. Stall recovery uses a 15-minute progress window and 30-minute cooldown.
- Base32 magnet hashes are normalized to hex; large torrent-metadata addition, newly added job discovery, and control-file recovery were improved.
- Incomplete torrent videos are excluded from heavy processing; deterministic subtitle-validation failures receive a longer retry delay.

### Subtitles and mpv
- Large local episode-start corrections can apply only before the OP without shifting an already stable main timeline; affected cached results are queued for rebuild.
- Exact Jimaku candidates may be accepted when independent Japanese sources agree on one clock and the embedded English reference is noisy.
- Added translation of the currently visible line inside mpv with up to 16 previous Japanese lines as context; the English track is additional context.
- Added low-priority translation-cache warming during playback and removed the previous separate subtitle-study window.

### Initial setup and reading
- Added dependency checks and guided initial setup for media and language-study tools.
- Added mutually exclusive JitenMPV / jpdb-mpv-plugin selection; jpdb-plugin discovery covers standard mpv directories and versioned installs.
- Anime Debug can be opened for a specific episode of a multi-episode title.
- Expanded LN bookmark saving and position reset; test logs are separated from runtime logs.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.15...v0.7.16) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/1d3063d7c71fafcfcefd5674525100f659e9df7e).


## v0.7.15 — Maintenance release after the PID fix

Range: [v0.7.14 → v0.7.15](https://github.com/TH1NKFASTER/pudge/compare/v0.7.14...v0.7.15). Status: published GitHub Release.

### Release contents
- Updated package version, README, and CHANGELOG entry to validate the preceding updater fix.
- Application code and tests are unchanged from v0.7.14.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.14...v0.7.15) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/e3c8bb7c689ed1ef3dc77709ebe437280a0ce940).


## v0.7.14 — Stopping the exact PID during updates

Range: [v0.7.13 → v0.7.14](https://github.com/TH1NKFASTER/pudge/compare/v0.7.13...v0.7.14). Status: published GitHub Release.

### Fix
- The updater receives the PID of the exact process that initiated the update via `os.getpid()`.
- Shutdown and follow-up verification use that PID instead of broad command-line process matching.
- If the original process survives, the updater reports its PID and aborts installation. Lifecycle tests were updated.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.13...v0.7.14) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/02e0d94d390f6d05f4ee6a8cf86491a4674c65ed).


## v0.7.13 — Maintenance release for update verification

Range: [v0.7.12 → v0.7.13](https://github.com/TH1NKFASTER/pudge/compare/v0.7.12...v0.7.13). Status: published GitHub Release.

### Release contents
- Updated the package version, README version, and CHANGELOG entry.
- Application code and tests are unchanged from v0.7.12. The previous restart fix is not introduced again in this release.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.12...v0.7.13) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/2bf4eed964c0bc9ead3b843bd03eb45e6cf4b879).


## v0.7.12 — Verifying the old window has exited before update

Range: [v0.7.11 → v0.7.12](https://github.com/TH1NKFASTER/pudge/compare/v0.7.11...v0.7.12). Status: published GitHub Release.

### Updates
- Before installation, the old launcher and `pudge.app_entry` are force-terminated and process presence is checked again.
- If the old session remains active after the bounded wait, the updater fails before invoking the installer.
- Tightened the process-search pattern so it does not match the search command itself; added old-window regression coverage.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.11...v0.7.12) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/71bacb6ad19a91627c9227a67383c26776725af9).


## v0.7.11 — Homebrew discovery from the GUI

Range: [v0.7.10 → v0.7.11](https://github.com/TH1NKFASTER/pudge/compare/v0.7.10...v0.7.11). Status: published GitHub Release.

### Fix
- The installer now checks standard Apple Silicon and Intel Homebrew locations first, then falls back to `PATH`.
- The discovered directory is prepended to `PATH`, so in-app installation finds the same brew tools as Terminal launches.
- The Homebrew-required message remains for systems where no installation is actually found.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.10...v0.7.11) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/51fc7f85f063d0ea75a6593674e2eb2236b9f748).


## v0.7.10 — Update-download retries and waiting for the old process

Range: [v0.7.9 → v0.7.10](https://github.com/TH1NKFASTER/pudge/compare/v0.7.9...v0.7.10). Status: published GitHub Release.

### Updates
- Transport failures while downloading an update are retried automatically with increasing delays; partial files are removed before retry and SHA-256 is recalculated.
- Before installation, old Pudge processes are asked to exit and waited for within a bounded timeout; remaining processes are force-terminated.
- Removed the native WebView update confirmation that was shown with the Python host icon.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.9...v0.7.10) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/b9ca399db1bacee0517dea36b91ebfd199201764).


## v0.7.9 — Pudge icon when launched through Python

Range: [v0.7.8 → v0.7.9](https://github.com/TH1NKFASTER/pudge/compare/v0.7.8...v0.7.9). Status: published GitHub Release.

### Fix
- The native launcher passes the `AppIcon.icns` path, and the entry point sets the application icon through AppKit before starting the GUI.
- Fixed the default Python icon appearing instead of Pudge. Icon-load failure does not block application startup.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.8...v0.7.9) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/7b55ea902f52f9f1d4e7c52036142e77673460f6).


## v0.7.8 — Running the installer from ZIPs and isolating version checks

Range: [v0.7.7 → v0.7.8](https://github.com/TH1NKFASTER/pudge/compare/v0.7.7...v0.7.8). Status: published GitHub Release.

### Fixes
- Extracted `install.sh` is invoked explicitly through `/bin/zsh`, so updates no longer depend on the executable bit surviving ZIP extraction.
- Installed-package verification runs with `-I`; the current directory and Python environment variables can no longer shadow the installed version being checked.
- Added regression coverage for both scenarios.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.7...v0.7.8) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/34e862413be24a205bad6df7e54126d45e8cc7f0).


## v0.7.7 — Relaunching the app after updates

Range: [v0.7.6 → v0.7.7](https://github.com/TH1NKFASTER/pudge/compare/v0.7.6...v0.7.7). Status: published GitHub Release.

### Fix
- The installer and updater now account for the `pudge.app_entry` process introduced by the native launcher when stopping the old version.
- This prevents a live GUI session from being missed before replacement and reopening.
- Added tests that the new process name is wired into both update paths.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.6...v0.7.7) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/cf970be8de43b76dad6c77fced7c2d77ca0d37ec).


## v0.7.6 — Lightweight native macOS launcher

Range: [v0.7.5 → v0.7.6](https://github.com/TH1NKFASTER/pudge/compare/v0.7.5...v0.7.6). Status: published GitHub Release.

### Launch and updates
- Replaced the frozen PyInstaller runtime with a small native launcher that starts `pudge.app_entry` from the managed Python environment.
- The app bundle no longer duplicates Pudge, the Python standard library, or third-party packages; after updates, the installed wheel is executed.
- Preserved fast update mode and rollback for failed package/bundle replacement.
- The native launcher owns Pudge notifications, passes tool paths, and removes environment variables from the previous frozen runtime.
- Updated macOS bundle metadata, icon handling, and installer checks.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.5...v0.7.6) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/dc738823cb07cc8c7d319e2a43d51dd421a609c8).


## v0.7.5 — Fast updates with package rollback

Range: [v0.7.4 → v0.7.5](https://github.com/TH1NKFASTER/pudge/compare/v0.7.4...v0.7.5). Status: published GitHub Release.

### Updates
- `--update` preserves the existing Python environment and backs up the installed Pudge package before replacement.
- Package and app-bundle rollback are available on failure. The new bundle is prepared before the live application is replaced.
- Added reuse of a compatible launcher runtime and skipped reinstalling heavy OCR dependencies during fast updates.
- Cleared inherited Python/Tcl/Tk environment variables that could point builds at files from the old bundle.

### Home
- Ready episodes of currently airing titles now appear in New episodes ready, including titles outside the AniList CURRENT list, instead of incorrectly falling into Completed and ready.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.4...v0.7.5) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/f699386121ae14faafb524663b90bfdc70e5dab5).


## v0.7.4 — Torrent selection and native Jiten actions

Range: [v0.7.3 → v0.7.4](https://github.com/TH1NKFASTER/pudge/compare/v0.7.3...v0.7.4). Status: published GitHub Release.

### Anime and subtitle sources
- Added preference for suitable batch releases with stricter season and episode validation.
- Reworked seeder/leecher contribution to ranking so very large counts cannot overwhelm match quality without bound.
- Added a short qBittorrent candidate race that observes activity, chooses a winner, and cleans up losing jobs.
- Jimaku candidates with explicit AniList-ID or season/episode conflicts are rejected; special exact matches are preserved.
- Planning retry-download buttons are hidden for already-ready or actively downloading episodes but remain available for stalled and failed states.

### Light Novels
- Added Jiten study actions directly in the reader, persistence of the selected deck, and configurable study triggers.
- Furigana and underline visibility can be restricted by word state; text color and underline mode are configurable.
- Improved readings for inflected word forms; furigana is excluded from copied text and the main text sent for translation.
- Added local-LLM generation of reader styling CSS with result validation.
- Fixed secondary-click/trackpad context menus, stacking order, menu closing, filtered-marker visibility, and embedded JavaScript syntax.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.3...v0.7.4) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/ba200f83d97345ee3e24c99e007f85ae8e493ea5).


## v0.7.3 — Job Center, study tools, and initial Visual Novel support

Range: [v0.7.2 → v0.7.3](https://github.com/TH1NKFASTER/pudge/compare/v0.7.2...v0.7.3). Status: published GitHub Release.

### Background jobs and downloads
- Added Job Center with persisted history plus cancel/retry support for OCR, STT, import, and episode-search jobs.
- Downloading individual Planning episodes is now a background job: local files are checked first, then search uses available title variants.
- LN Nyaa search targets Literature / Raw, recognizes volume ranges, and selects the requested volume from batch torrents. After the selected volume is moved, the torrent is removed while keeping the needed file.
- Formalized episode-state transitions and their history so scans cannot downgrade already confirmed or user-managed states.
- Added optional built-in trial Jimaku key support for the first 48 hours. It applies only to builds that ship such a key; a personal key takes precedence.

### Reading and audio
- Added Planning filters/sorting based on Jiten data, expanded LN metadata, and wider audiobook playback cards.
- Added shared Jiten study-state color themes for LN, Manga, and VN, a dedicated pitch-accent color, and mora diagrams in word cards.
- LN gained inline pitch-accent diagrams, a visibility setting, numbered readings, and a character-name editor from the context menu.
- Added confirmed volume completion and local removal of completed volumes; local removal does not reduce AniList progress.
- LN and audiobooks auto-link on sufficiently reliable matches; audiobook entries can search for the corresponding LN.
- Paired-reading highlighting uses acoustic activity to account for pauses between phrases. Paired-audio controls moved into the reader toolbar.
- STT now exposes progress percentage, uses isolated temp files and the configured ffmpeg, and resumes unfinished jobs on startup.
- Fixed final-position persistence on Stop by pausing mpv first. Expanded chapter lists remain expanded during state refresh.

### Visual Novels and setup
- Added the initial VN subsystem: select a macOS window, start/stop capture, OCR text, and send recognized text to study tools.
- Added a Screen Recording permission shortcut and separate VN web modules. This is an initial implementation, not a claim of broad game compatibility.
- Added step-by-step Jimaku/AniList setup explanations; AniList refresh runs after credentials change.
- Fixed character-name editor overlap, search-suggestion placement, dependent-setting availability, and bitmap-subtitle state rendering.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.2...v0.7.3) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/d83752b0fe88fb2a36c28d56102fa2c1a09c109c).


## v0.7.2 — Paired reading, region OCR, and in-app updates

Range: [v0.7.1 → v0.7.2](https://github.com/TH1NKFASTER/pudge/compare/v0.7.1...v0.7.2). Status: published GitHub Release.

### Manga
- Reworked recognition into an “Apple Vision regions → MangaOCR crops” pipeline with nearby-rectangle merging and per-region result storage.
- Improved vertical and stylized text handling, overlay persistence across pages, tooltip closing on pointer exit, and scrolling while zoomed.
- Unified volume preparation and context actions into a shared menu and removed duplicate reader-handler registration.
- Added title rating, AniList relinking, and opening AniList from the linked cover.

### Light Novels and audiobooks
- Added LN↔audiobook linkage and paired reading with text/audio jumps, fragment highlighting, speed controls, and seeking.
- Japanese STT transcripts provide text anchors and chapter boundaries; alignment results are cached.
- Audiobooks gained a position bar, bookmarks, sleep timer, persistent speed, completion marking, and resume-with-rewind.
- EPUB import filters technical sections such as short colophons and author information that are not reading chapters.
- AniList character-name dictionaries help preserve names during translation, including unambiguous short forms.

### Planning and updates
- Added AniList search suggestions, LN/Manga ratings, and lazily loaded Jiten metadata for length, difficulty, and known-word coverage when a key is configured.
- Added manual updates from the UI: release installs download the requested version ZIP and verify SHA-256, with app-bundle rollback on failure.
- Development-checkout updates are allowed only from the official origin with a clean tree and a fast-forward path.
- Added replaceable ffprobe/AniList caches; reselecting an already open LN or Manga tab no longer reloads it unnecessarily.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.1...v0.7.2) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/d507cfbe27cbfe4662406a04d3392f0449329170).


## v0.7.1 — Small timeline cleanup

Range: [v0.7.0 → v0.7.1](https://github.com/TH1NKFASTER/pudge/compare/v0.7.0...v0.7.1). Status: published GitHub Release.

### Changes
- Removed the unused intermediate `new_boundaries` calculation from `_insert_override_segment`. The active boundary recomputation from segment ends remains unchanged.
- Updated the version number, README, and version assertion in tests.
- The v0.7.0 → v0.7.1 diff does not add a new subtitle pipeline or new readers; those larger changes belong to earlier tags.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.7.0...v0.7.1) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/0da50bbd7579307d67a4ea43f31219a2c5d18a88).


## v0.7.0 — Migration to the `pudge` package and expanded readers

Range: [v0.6.77 → v0.7.0](https://github.com/TH1NKFASTER/pudge/compare/v0.6.77...v0.7.0). Status: tag without a published GitHub Release.

### Package and installation
- Renamed the Python package from `anime_mpv` to `pudge`; imports, entry points, resources, build logic, and checks were updated. Pure file moves without content changes are not treated as new functionality.
- Updated branded paths and cleanup of artifacts left by the previous application name.

### Subtitles and anime
- Added a dedicated timeline-alignment algorithm that evaluates local windows, finds a sequential path, builds segments, and determines shift-change boundaries.
- Added separate refinement for episode start, the first line after a pause, and short transition regions; cue ordering and held-out-window behavior are validated.
- Added early rejection of dangerous ALASS discontinuities against the embedded track so expensive later processing cannot hide an already detected failure.
- Fixed absolute episode numbering sent to AniList: progress is mapped to the concrete season/cour entry, and AniList’s published episode count takes precedence over stale local hints.
- Added stage traces, a selected-episode diagnostic snapshot, and forced fresh subtitle selection.

### Manga, LN, and Audiobooks
- Added Manga Reader v2 with Apple Vision text regions, background volume OCR, cached results, and study tools for selected text.
- Manga is grouped by series and volume; AniList linkage propagates within the series. MangaOCR can be installed from the app with log viewing.
- Audiobooks can be imported from multi-file folders and mapped onto a shared timeline. Added stop, seek, speed changes, and record deletion.
- Shared reading tools support text parsing and study actions; LN translation language follows the application language.
- Expanded library context actions and tests for reading, audio, OCR, and timeline transitions.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.77...v0.7.0) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/5ff48bc0f09caea5c37415db9cb69cd5d7a47dd3).


## v0.6.77 — Preparation stages, first Manga support, and Audiobooks

Range: [v0.6.76 → v0.6.77](https://github.com/TH1NKFASTER/pudge/compare/v0.6.76...v0.6.77). Status: published GitHub Release.

### Subtitles
- Preparation is split into explicit discovery, normalization, synchronization, validation, and selection stages. Worker progress and job state are persisted for display and recovery.
- Subtitle upgrades are judged by the quality of the already prepared result; source filename ranking remains a discovery signal.
- Container chapters are used as additional edit boundaries. Added cacheable Japanese STT as a fallback timing reference.
- Local-LLM semantic subtitle validation is disabled by default; the main path relies on deterministic processing.
- Semantic anchors now include validation on held-out examples to reduce self-confirmation of the selected shift.

### Media and interface
- Added initial CBZ/ZIP manga import and reading with page persistence and lazy MangaOCR startup.
- Added audiobook import, chapter listing, and mpv playback with saved position.
- Settings are split into categories; initial setup and the Home attention block are simplified. Media and settings web modules were moved into separate files.

### Data and development
- Added versioned SQLite migrations and stage-state fields for background jobs.
- Backups scrub secrets from config and database data; restore preserves the user’s current secrets. Database pages are rebuilt after secret removal.
- Added license, development/security docs, CI checks, and integration tests for the new subsystems.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.76...v0.6.77) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/cccdf3314d5000d83ad403dfdfb884e88bfd6d72).


## v0.6.76 — Embedded-subtitle anchor lines

Range: [v0.6.75 → v0.6.76](https://github.com/TH1NKFASTER/pudge/compare/v0.6.75...v0.6.76). Status: published GitHub Release.

### Synchronization
- Added matching of Japanese lines against dialogue from the embedded English track to recover timing before the OP, after the OP, in the middle, and near the end of an episode.
- Matching allows unmatched English-only lines such as song lyrics; they are not forced to pair with Japanese cues.
- Timing corrections are applied in stable regions. A transition across the OP is constrained to the corresponding interval without Japanese dialogue.
- Corrections are validated by anchor support, residual error, cue-order preservation, and the absence of a meaningful degradation in global activity.
- The method is integrated into ALASS and constant-offset recovery, with diagnostics reporting which strategy was used.

### Interface
- Removed the repeated explanation of randomized rating order from the normal rating-dialog flow.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.75...v0.6.76) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/d7ff56658110d5eba449b32b136ac8e16b8b1969).


## v0.6.75 — Jimaku rate limiting and two timing plateaus around the OP

Range: [v0.6.74 → v0.6.75](https://github.com/TH1NKFASTER/pudge/compare/v0.6.74...v0.6.75). Status: published GitHub Release.

### Jimaku
- Added a process-shared request budget: an initial burst of four requests followed by replenishment at 20 requests per minute.
- Concurrent calls reserve different future slots, reducing the chance of synchronized retries hitting the API at once.
- Changing the API key resets the previous key’s budget; a damaged budget file no longer blocks the provider entirely.

### Synchronization
- Added detection of stable shift clusters before and after the opening, allowing the two regions to be corrected independently.
- Noisy, weakly supported, or nearly identical regions are discarded so the algorithm does not invent an artificial timing jump.
- Updated the synchronization fingerprint so rebuilt results use the new algorithm.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.74...v0.6.75) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/42a145f590a5aed5849803b7bab7dee9ec5f41c6).


## v0.6.74 — Consistent semantic validation

Range: [v0.6.73 → v0.6.74](https://github.com/TH1NKFASTER/pudge/compare/v0.6.73...v0.6.74). Status: published GitHub Release.

### Subtitles
- Semantic validation now considers per-example scores when aggregate fields returned by the local model contradict each other.
- Unanimous, near-unanimous, and weaker example agreement are distinguished, with different temporal-activity requirements for each level.
- Strong text agreement may pass with moderate activity agreement, but partial semantic agreement does not bypass stricter timing validation.
- Bumped the semantic-cache version.

### Diagnostics
- Preparation and synchronization messages now follow the selected UI language, including English diagnostic reasons.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.73...v0.6.74) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/eb8f1f35558a60881e1b4c68e4d4a6408a8a66e3).


## v0.6.73 — LN position persistence and Jimaku rate-limit handling

Range: [v0.6.72 → v0.6.73](https://github.com/TH1NKFASTER/pudge/compare/v0.6.72...v0.6.73). Status: published GitHub Release.

### Reader
- LN position is saved before closing the reader or changing chapters, then restored after the page is built.
- Both vertical scrolling and horizontal paged reading are covered. Scroll saves are debounced to coalesce frequent events.
- Increased the maximum text width to 2400 px; dictionary cards now use clearer study-state names and ruby markup for readings.

### Jimaku
- HTTP 429 establishes a shared cooldown persisted on disk; new requests respect it instead of retrying immediately.
- During a rate limit, a previously saved response is used when available. `Retry-After` is honored with a bounded maximum wait.
- A job deferred because of the service limit is returned to the queue without increasing the media-preparation failure count.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.72...v0.6.73) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/e4f6d0d4585c9ce9c80204330b640c19b37fcbff).


## v0.6.72 — qBittorrent paths and persisted synchronization results

Range: [v0.6.71 → v0.6.72](https://github.com/TH1NKFASTER/pudge/compare/v0.6.71...v0.6.72). Status: published GitHub Release.

### Downloads and subtitles
- Added recovery for qBittorrent downloads in `missingFiles` state when files moved under the new library root after the Anime MPV → Pudge rename. Pudge points qBittorrent at the existing new location and requests a recheck.
- Added location changes and forced file verification to the qBittorrent provider.
- Extended migration of previously synchronized Jimaku subtitles: selection history can now identify a stale intermediate result even after the original alignment cache has been removed.

### Light Novels and settings
- Linked LN cards now include an AniList action and a revised metadata/action layout.
- Clarified AniList, Jimaku, LLM, and qBittorrent explanations and grouped related settings more consistently.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.71...v0.6.72) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/ac683525577c89e838d733446fa5a7056a7e5c1c).


## v0.6.71 — Preparation recovery and installing the current source tree

Range: [v0.6.69 → v0.6.71](https://github.com/TH1NKFASTER/pudge/compare/v0.6.69...v0.6.71). Status: published GitHub Release.

### Fixes
- qBittorrent Web API failures or temporary unavailability no longer prevent ready-to-run subtitle jobs from being processed.
- Failure to show the native “preparation completed” notification is logged without invalidating the job result.
- Added selective rebuilding of stale playback SRTs originating from synchronization caches. The final pipeline result is cleared as well, while manually selected subtitles are protected from this migration.
- Simplified the explanation in the rating dialog.

### Installation and development
- Installing from a Git checkout now builds a wheel from the current source tree in a temporary directory. An old wheel next to the installer no longer determines what code gets installed.
- Added a pre-push hook that runs tests in four batches and rejects the push if any batch fails; `.venv-test` is ignored by Git.
- Fixed v0.6.71 version metadata and the corresponding checks.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.69...v0.6.71) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/32f46a90501beb3a98adc59ba332a7044cd8a6c9).


## v0.6.69 — Subtitle waiting and constant-offset search

Range: [v0.6.68 → v0.6.69](https://github.com/TH1NKFASTER/pudge/compare/v0.6.68...v0.6.69). Status: tag without a published GitHub Release.

### Subtitles and performance
- Reworked constant-shift search: coarse hypotheses are evaluated first, then only the best sufficiently distinct candidates are refined. Duplicate evaluations of the same shift are eliminated.
- Reduced the number of initial hypotheses and added the `candidate_evaluations` counter to expose the real cost of the search.
- Bumped the synchronization cache key so results from the previous algorithm cannot masquerade as new ones.

### Interface
- Deferred subtitle jobs no longer keep the UI in an active Checking state until their retry time arrives.
- Background polling now takes the nearest scheduled retry into account; manual refresh remains available when jobs are only waiting for their scheduled time.

[Source diff](https://github.com/TH1NKFASTER/pudge/compare/v0.6.68...v0.6.69) · [Version commit](https://github.com/TH1NKFASTER/pudge/commit/c77b259782ef84f9b19a05d5bb3fd2c7f9d51c4e).


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

- Piecewise synchronization no longer interpolates the cold-open offset across a long gap without dialogue; the next stable clock is used immediately after the opening/title card.
- Fixed Hyakkano S03E05: the first line after a 106-second gap now receives +0.35s instead of an incorrect +2.26s.
- Existing playback SRTs are automatically rebuilt.

- Fixed grouped `1–3 ↔ 1–3` synchronization: a long English cue no longer stretches or compresses the internal boundaries of Japanese SRT cues.
- A split/merge group now transfers only the shared local shift, preserving the original durations and pauses of the Japanese subtitles.
- Added a quality gate: a correction is rejected when it barely improves timing activity or materially changes cue durations.
- Previously prepared playback SRTs are automatically queued for revalidation.

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
- Initial setup now explains that qBittorrent is optional: aria2 still downloads automatically, while qBittorrent provides richer categories, tags, and torrent-management controls.
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

# Visual Novel reader plan

The Visual Novel reader is an optional tool that starts only when the user opens
it. Ordinary library refreshes, subtitle preparation, and manga reading must not
start screen capture or OCR.

## Capture pipeline

1. Ask for macOS Screen Recording permission only after the user selects
   **Visual Novel reader**.
2. Use ScreenCaptureKit to list shareable windows and persist a window identity,
   never a screen-coordinate rectangle. This also covers a CrossOver window.
3. Capture at 2 fps while text is stable and temporarily increase to 5 fps after
   a frame change. Keep the queue depth at two frames and discard stale frames.
4. Detect changed regions before OCR. Run Apple Vision on changed areas and use
   MangaOCR only for Japanese crops that need a second pass.
5. Stabilize repeated lines by normalized text and geometry. Expose one selectable
   overlay plus a chronological transcript instead of appending every frame.
6. Pause capture when the VN window is hidden, minimized, or unchanged for 30
   seconds. Stop all workers when the reader closes.

## Study and context

- Feed stable lines through the existing Jiten/JPDB parsing cache.
- Keep the previous 10 stable lines as local context for translation and the
  optional local LLM. Never send continuous screenshots to the LLM.
- Build a per-title name glossary from AniList plus user corrections. Corrections
  override AniList and are reusable by LN, manga, subtitles, and VN translation.
- Deduplicate cards by backend word/reading id and preserve the original sentence.

## Planned releases

- **0.8.0:** window picker, permission flow, adaptive capture, selectable OCR,
  transcript, Jiten/JPDB actions, and energy diagnostics.
- **0.8.1:** local-LLM context disambiguation and editable character glossary.
- **0.8.2:** optional text-hook adapters when a VN engine supports them; OCR stays
  the universal fallback for CrossOver and untranslated builds.

## Acceptance checks

- No capture or OCR process exists before explicit activation.
- Memory stays bounded during a two-hour session; no unbounded frame history.
- A hidden/unchanged window consumes no OCR calls.
- Repeated dialogue produces one transcript entry and one set of study tokens.
- Revoking Screen Recording permission fails locally with a clear recovery action.

# Visual Novel reader

The Visual Novel reader is an optional macOS tool for games that do not provide
selectable Japanese text. It stays completely idle until the user opens the
reader, chooses a window, and presses **Start reader**.

## Current behavior

- Lists suitable visible application windows on macOS.
- Requests Screen Recording permission only when capture is explicitly started.
- Captures only the selected window.
- Skips OCR when the frame has not changed and backs off further when the window stays unchanged.
- Uses macOS text recognition for the current frame.
- Waits for repeated matching text before adding a line to the transcript, which reduces duplicates during animation or transitions.
- Keeps a bounded recent transcript rather than an unlimited screenshot history.
- Runs recognized Japanese through the same reading/study-card tools used elsewhere in Pudge.
- Lets the selected VN window carry an AniList identity for library context.
- Stops capture and workers when the reader is stopped or the application closes.

The reader does not run OCR during ordinary anime, manga, Light Novel, or
library refresh activity.

## Known limitations

The current reader recognizes the selected window as an image. It does not yet
use engine-specific text hooks, speaker-name extraction, region selection, or a
separate MangaOCR second pass. Very animated windows can therefore produce
noisier text than static dialogue boxes.

## Possible next improvements

Future work can improve text-region detection, speaker/name handling, local LLM
context, and optional engine-specific text hooks. Those are enhancements to the
existing reader rather than requirements for the current feature.

Any future capture changes should keep the same safety rules: explicit user
activation, bounded memory, no background capture after Stop, and no continuous
screenshots sent to an online service.

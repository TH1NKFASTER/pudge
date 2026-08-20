# Pudge user guide

This guide covers the tasks you are most likely to do. Pudge keeps its library
on your Mac and contacts online services only for features you turn on.

## Watch an anime

1. Add a title from Planning or AniList.
2. Open its menu and download an episode or a full available release.
3. Pudge checks whether the episode is already on disk, then downloads only what is missing.
4. It finds Japanese subtitles, checks their timing, and shows the episode as **Ready**.
5. Open the episode. Pudge saves your position and marks it watched when you reach the configured completion threshold.

If subtitle preparation stops, retry it from the episode card. Diagnostics shows
the reason when a retry needs more than a normal network refresh.

### What the episode labels mean

| Label | Meaning |
|---|---|
| Found locally | The video is on disk but subtitle preparation has not started. |
| Preparing subtitles | Pudge is finding, extracting, or checking subtitles. |
| Waiting for text subtitles | The available subtitle is image-based; selectable Japanese text is still being prepared. |
| Ready | A checked Japanese text subtitle or embedded text track is available. |
| Watched | The episode was completed. |
| Dropped | The title was removed from the active library. |

A normal scan never turns a watched or ready episode back into an earlier state.
Resetting progress or running a repair is always an explicit action.

### Image-based subtitles

PGS/SUP subtitles are pictures, so their text cannot be selected. When image
subtitle OCR is enabled, Pudge converts them to Japanese text and checks the
result. The original image track can still be used for playback while this is
in progress.

## Read a Light Novel

1. Import an EPUB or TXT file from **Light Novels**.
2. Link it to AniList if you want cover art and progress updates.
3. Choose Jiten or JPDB in Settings for dictionary and study actions.
4. Adjust font, width, colors, furigana, and pitch accent in reader appearance.
5. Select a word to open its reading and study card.

**Finish volume** needs two clicks within five seconds. This prevents an
accidental AniList progress update. **Remove Finished** clears only Pudge's
local badge and does not reduce AniList progress.

## Read manga

Import a CBZ or ZIP archive from **Manga**. Reading works without OCR. Run
MangaOCR only when you want selectable Japanese text from a page; it does not
run simply because you opened the book.

## Pair a Light Novel with an audiobook

Import both items and give them the correct AniList identity when possible.
Pudge links obvious matches automatically, but leaves ambiguous titles for you
to choose. Once audio transcription and alignment finish, **Listen together**
keeps audio position and reader highlighting in step.

If Pudge chose the wrong match, unlink it, correct the AniList entries, and
select the pair manually. Existing manual links are never replaced by automatic
matching.

## Use Pudge on a phone or tablet

Enable the companion server in Settings, start pairing, and open the provided
address or QR code on a device connected to the same trusted network. The
device receives a revocable access token; it never reads the SQLite database
directly.

The companion library refreshes whenever it returns to the foreground and every
15 seconds while it remains visible. When an episode is completed on the Mac,
an older mobile resume event cannot turn it back into **Continue**. If the Mac
is asleep or Pudge is closed, the phone keeps its last view until it can connect
again.

## Back up and restore

Use **Settings → Maintenance → Create full backup**. A backup contains settings,
the library database, mappings, queues, history, and prepared subtitle files. It
does not contain videos, torrent payloads, or API credentials.

Restoring replaces the current settings and database but keeps the credentials
already stored on that Mac.

## Remove Pudge completely

Open **Settings → Remove Pudge** and click the red button. Two confirmations are
required because the action cannot be undone.

The uninstaller removes Pudge's app bundle, command-line tools, LaunchAgent,
settings, database, Pudge-created backups in Downloads, cache, logs,
paired-device records, Keychain entries, and the Pudge library folder. Folders
added only for watching or subtitle search remain. Homebrew and shared tools
such as mpv, qBittorrent, and JitenMPV also remain installed.

## If something looks wrong

- **A completed episode still says Continue on mobile:** bring the companion page to the foreground and make sure the Mac is awake and Pudge is running.
- **An episode moved backwards:** refresh once, then include its state history from Diagnostics in a bug report.
- **OCR or transcription is not moving:** retry the item, then check Diagnostics for the last job error.
- **Nyaa found the wrong or no Light Novel:** correct the local title or AniList link and search again.
- **The wrong book and audiobook were linked:** unlink them and choose the pair manually.

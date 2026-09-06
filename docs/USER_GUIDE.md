# Pudge user guide

This guide covers the tasks you are most likely to do. Pudge keeps its library
on your Mac and contacts online services only for features you turn on.

## Watch an anime

1. Add a title from Planning or AniList.
2. Download an episode or an available batch.
3. Pudge finds Japanese subtitles, checks them, and marks an episode **Ready** only when a usable subtitle is actually available.
4. Open the next unwatched episode. Pudge saves your position and marks it watched when you reach the configured completion threshold.

**Completed & Ready** follows the next unwatched episode. A later downloaded
episode does not make a title Ready while an earlier unwatched episode still
needs subtitle repair.

### What the episode labels mean

| Label | Meaning |
|---|---|
| Found locally | The video is on disk but subtitle preparation has not started. |
| Preparing subtitles | Pudge is finding, extracting, checking, or repairing subtitles. |
| Waiting for text subtitles | The available subtitle is image-based; selectable Japanese text is still being prepared. |
| Ready | A checked Japanese text subtitle or embedded text track is available now. |
| Watched | The episode was completed. |
| Dropped | The title was removed from the active library. |

Refresh also checks that prepared subtitle files still exist. If a previously
Ready file disappeared, Pudge can move that episode back to preparation and
rebuild it. When several episodes need repair, the next unwatched episode of
each title is handled before later episodes.

Heavy repair waits during active playback when necessary and resumes after the
player closes.

### Image-based subtitles

PGS/SUP subtitles are pictures, so their text cannot be selected. When image
subtitle OCR is enabled, Pudge can convert them to Japanese text and check the
result. The original image track can still be used for playback while this is
in progress.

## Read a Light Novel

1. Import an EPUB or TXT file from **Light Novels**.
2. Link it to AniList if you want cover art and progress updates.
3. Choose Jiten or JPDB in Settings for dictionary and study actions.
4. Adjust font, width, colors, furigana, and reader appearance.
5. Select a word to open its reading and study card.

Books are grouped by series and volume. Series cards and individual books can
be selected for bulk actions.

**Finish volume** needs two clicks within five seconds. This prevents an
accidental AniList progress update. **Remove Finished** clears only Pudge's
local badge and does not reduce AniList progress.

## Read manga

Import a CBZ or ZIP archive from **Manga**. Manga is grouped by series and
volume, and both series cards and individual volumes can be selected for bulk
actions.

Reading works without OCR. Run MangaOCR only when you want selectable Japanese
text from a page; it does not run simply because you opened the book.

## Listen to audiobooks

Import an audiobook file or folder from **Audiobooks**. Books from the same
series are grouped together and shown as `Volume 1`, `Volume 2`, and so on when
a volume can be identified. Pudge can infer the volume from the audiobook or
from a linked Light Novel.

A series card keeps the nearest unfinished volumes visible and scrolls inside
when the series is longer. Click the background of a volume card to select that
volume. Click the outer series card to select or clear the whole series.
Playback buttons, sliders, chapter controls, and cover art keep their own
actions and do not toggle selection.

## Pair a Light Novel with an audiobook

Import both items and give them the correct AniList identity when possible.
Pudge links obvious matches automatically, but leaves ambiguous titles for you
to choose. Once audio analysis and alignment finish, **Listen together** keeps
audio position and reader highlighting in step.

If Pudge chose the wrong match, unlink it, correct the AniList entries, and
select the pair manually. Existing manual links are never replaced by automatic
matching.

## Read a Visual Novel

Open **Visual Novels**, choose a visible game window, and press **Start reader**.
Pudge asks for macOS Screen Recording access only when you start this feature.
It reads changed frames, keeps a short transcript, and lets you use the same study
cards on recognized Japanese text.

Capture stops when you press **Stop** or leave the reader. Repeated unchanged
frames do not create duplicate transcript lines. If capture permission is
missing, the reader shows a direct recovery action instead of silently failing.

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
the library database, mappings, queues, history, and Pudge-managed prepared
subtitle files. It does not contain videos, torrent payloads, or API credentials.

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

- **A Ready anime suddenly starts preparing:** Refresh found that its prepared subtitle was missing or no longer usable and queued a repair.
- **Repair is waiting while an episode is playing:** close mpv; heavy work resumes automatically.
- **A completed episode still says Continue on mobile:** bring the companion page to the foreground and make sure the Mac is awake and Pudge is running.
- **OCR or audio analysis is not moving:** retry the item, then check Diagnostics for the last job error.
- **The wrong book and audiobook were linked:** unlink them and choose the pair manually.

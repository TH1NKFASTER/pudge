# Pudge user guide

This guide describes the normal workflows rather than every individual control. Pudge is local-first: it indexes files already on disk, then uses optional services only for metadata, downloads, subtitles and study data.

## 1. Add an anime and prepare it for watching

1. Add the title to Planning through AniList or the Planning search.
2. Open its context menu and choose a series release, or use **Download released automatically**.
3. Pudge checks the local library before every Nyaa search, so an episode already on disk is skipped.
4. The episode then moves through subtitle preparation. A text subtitle that passes language and timing validation makes the episode **Ready**.
5. If preparation fails or is interrupted, start the action again from the relevant media card or Diagnostics.

For a partial season, automatic per-episode download searches only for released episode numbers and records one result for each episode. It does not silently replace a local file.

## 2. Understand video and subtitle states

The durable episode states are:

| State | Meaning | Normal next state |
|---|---|---|
| `local` | Video was found; preparation has not been requested yet | `waiting_subtitles` |
| `waiting_subtitles` | Text subtitle discovery/alignment is queued | `ready` or `waiting_text_subtitles` |
| `waiting_text_subtitles` | A bitmap subtitle exists, but Pudge is finding or creating selectable Japanese text | `ready` |
| `ready` | A validated text subtitle or embedded text track is selected | `watched` |
| `watched` | Playback met the watched threshold | `ready` after an explicit progress reset |
| `dropped` | The title was dropped and may be scheduled for cleanup | `local` after an explicit restore |

A library scan is observational: it cannot move `ready`, `watched`, `dropped` or a confirmed bitmap fallback backwards. Repair actions are explicit transitions and are written to state history.

### Bitmap subtitles and OCR

PGS/SUP and other image subtitles cannot provide selectable text. With image-subtitle OCR enabled, Pudge extracts the image track, runs OCR and validates the resulting Japanese text. Until that succeeds, the video remains `waiting_text_subtitles`; the bitmap track can still be used for Library-only playback. Retry the relevant preparation action if OCR fails.

## 3. Read a Light Novel with pitch accent

1. Import one or more EPUB/TXT files from **Light Novels**.
2. Link the book to the AniList novel entry when prompted.
3. Enable **Furigana** and **Inline pitch accent** in reader appearance.
4. Select a word to open its Jiten/JPDB card and study actions.

For an inflected surface form, Pudge rebuilds the displayed reading from Jiten token ruby ranges. If Jiten supplies a surface-form accent, it is used directly. Otherwise Pudge transfers the dictionary downstep to the reconstructed mora sequence and marks it as derived. This keeps the diagram aligned with the actual conjugated reading without presenting the fallback as a separately verified dictionary entry.

### Finish a volume safely

**Finish volume** is a two-step action: click it once to arm it, then click it again within five seconds. Finishing may update AniList volume progress. To remove only the local badge, right-click the volume and choose **Remove Finished**. Removing the badge deliberately does not reduce AniList progress.

## 4. Pair a Light Novel and audiobook

Pudge attempts a conservative automatic link after either side is imported or receives an AniList identity:

- an identical AniList ID is the strongest signal;
- normalized titles must otherwise be at least 90% similar;
- explicit volume numbers must agree;
- ambiguous candidates are left unlinked.

When linked, the audiobook receives the novel's AniList identity if it has none (or the novel receives the audiobook identity). Japanese STT runs in the background, then text/audio alignment enables **Listen together**, synchronized seeking and reader highlighting. You can still change AniList or the audio link manually.

If only the audiobook is local, choose **Find LN on Nyaa** on its card. The query uses the linked novel title first, then the audiobook AniList title, then the local filename, and includes the volume when known.

## 5. Jimaku trial and personal keys

A release may include a shared Jimaku key for the first 48 hours. The release workflow reads it from the GitHub Actions repository secret `PUDGE_TRIAL_JIMAKU_API_KEY`; the key is not committed to the source tree or saved to the user's config. A personal Jimaku key always takes priority.

If a self-built release does not provide that build secret, trial access is disabled and personal Jimaku keys continue to work.

## 6. Common recovery scenarios

- **A scan changed an episode backwards:** current builds prevent this. Refresh once; if it persists, inspect the episode and subtitle job in Diagnostics and include the state history in a bug report.
- **OCR/STT appears stuck:** retry the relevant preparation action from the media card or inspect Diagnostics before reporting it.
- **The wrong LN and audiobook linked:** unlink them in the LN audio picker, set the correct AniList identities, and link manually. Automatic matching will not overwrite an existing link.
- **Nyaa found nothing for an audiobook:** adjust the local audiobook title or bind AniList, then run **Find LN on Nyaa** again.

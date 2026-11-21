# Pudge Mac ↔ iPhone/iPad sync protocol (v2)

This is how the iPhone/iPad app talks to Pudge on the Mac. The Mac side is in
`pudge/iphone_http.py` (HTTP server and routes), `pudge/iphone_service.py` (pairing,
audiobooks, positions, bookmarks, handoff, archives), `pudge/iphone_media.py` (reading, video,
study, statistics) and `pudge/web_app_iphone.py` (the window's iPhone settings, Bonjour). The
phone side is `ios/Pudge/App/PudgeModel.swift`, `ios/Pudge/Networking/MacConnection.swift` and
`ios/PudgeCore`. The old v1 web companion (`/api/v1/…`, port 47821, `[companion]` config,
`MOBILE_SYNC_PROTOCOL.md`) was removed in 0.7.30.

## Transport and discovery

- HTTPS on port **8766**, TLS 1.2 or newer, a self-signed certificate made on first start
  (`<data dir>/iphone/tls-cert.pem`, `tls-key.pem`; never rotated silently).
- The phone **pins** the certificate: the SHA-256 of its DER bytes comes in the pairing QR. The
  bearer token is sent only after the pin passed. Redirects only to the same host and port.
- Connections are served only on this Mac's private LAN addresses from private peers (and
  loopback for the Simulator); VPN tunnels and public addresses are closed before TLS.
- Limits before authentication: 15 s for TLS handshake + request line + headers, 16 KB pairing
  body, 128 connections (32 per address). Addresses that have not authenticated in the last hour
  share 64 of them, so devices that never paired cannot keep a paired phone out.
- Bonjour `_pudge-audio._tcp` on each private LAN interface, only while at least one device is
  paired. The instance name is random per network and day (not the server ID); TXT is `v=2`.
  The phone tries every Pudge service it finds; only its own Mac passes the pin. Older Macs
  named the service after the server ID.
- The phone keeps the Mac's numeric addresses next to the saved one (usually its Bonjour host
  name, which macOS changes after a name conflict, e.g. `…-3391.local.` → `…-3395.local.`): the
  invitation's `baseURLs`, and addresses Bonjour resolves once they answered as this Mac (pin and
  `serverId`). When the saved address does not answer, those are tried too over the same pinned
  session (same pin, same `serverId`, no token), and the first that answers is saved.
- The Bonjour search starts over after a network change (any other network, also Wi-Fi to Wi-Fi),
  in the foreground, and on the first miss or when the search is not running. Without Wi-Fi, a
  wired network or this phone's Personal Hotspot (`bridge*`) the phone does not ask a Mac saved at
  a LAN address ("No Wi-Fi"); one saved under another address (VPN, DNS name) is still asked.
  After no answer it asks again after 5 s, 15 s, then every 60 s (at once after a network change,
  in the foreground, on pull to refresh; a check begun before that is not counted), so its
  screens never wait for an absent Mac.

## Pairing

1. The Mac shows a QR: `pudge-audio://pair?data=<base64url JSON>` with `protocolMajor` (2),
   `serverId`, `serverName`, `baseURL`, `baseURLs` (every LAN address, best first),
   `certificateSHA256`, `invitation` (256-bit, single use, 10 minutes) and `expiresAt`.
2. `POST /api/v2/pair` `{invitation, deviceId, attemptId, name}` → `{"status": "pending",
   "confirmationCode", "deviceId", "serverId"}`. The Mac's window shows Allow/Deny with the same
   six-digit code. The phone repeats the same request every 2 s.
3. After Allow: `{"status": "approved", "deviceId", "serverId", "token"}`. Deny, expiry, a used
   or cancelled invitation: HTTP 422 `{"error": "<reason shown on the phone>"}`.
4. Every other route needs `Authorization: Bearer <token>`. The Mac keeps only the token's
   SHA-256; revoking a device closes its connections at once (HTTP 401 afterwards).

`deviceId` is the phone's Keychain ID (this device only). A phone restored from a backup or moved
to a new iPhone keeps its local database but gets a new ID: it re-addresses its queued checkpoints
to the new ID (`LocalStore.adoptDeviceId`), and the Mac answers checkpoints of the earlier ID with
a final `rejected: device_changed` receipt, never an error.

## Routes

Unauthenticated: `GET /api/v2/capabilities` → `{protocolMajor, serverId, capabilities: [...]}`,
`POST /api/v2/pair`.

Audiobooks:

| Route | Purpose |
| --- | --- |
| `GET /api/v2/catalog` | Every book's manifest; `errors`, `preparing` (ask again shortly). |
| `GET /api/v2/catalog?lazy=1` | Manifests of books already prepared; the rest under `pending` (`assetId`, `title`, `durationMs`, `seriesTitle`, `volume`, `needsPreparation`). Capability `lazy-catalog`. |
| `POST /api/v2/assets/prepare` `{assetId}` | Prepare one book first (hash, convert FLAC/Opus/Ogg to AAC): `{"status": "ready", "manifest"}`, `preparing` or `failed`. |
| `GET /api/v2/assets/<asset>/manifest` | One manifest. |
| `GET /api/v2/assets/<asset>/progress` | Position state (below), plus `playingOn`, `playingOnName`, `deviceNames`. |
| `GET /api/v2/assets/<asset>/bookmarks` | `{items: [bookmark]}` (the Mac's own bookmarks included). |
| `POST /api/v2/sync/state` `{assets: [...], since?}` | Progress and bookmarks of up to 1000 books in one answer: `{revision, unchanged, items: [{assetId, progress, bookmarks}]}`; with `since` equal to the current `revision`, `unchanged: true` and no items. Capability `state-batch`. |
| `GET /api/v2/resources/<resource>/content?sha256=` | The bytes (single `Range` supported; `ETag` = SHA-256, the same after a Mac restart; `If-Range` other than that ETag gets the whole file with 200). Episode and reading downloads resume a cut transfer with `Range: bytes=<have>-` + `If-Range`. |
| `POST /api/v2/checkpoints` | A position checkpoint (below) → receipt. |
| `POST /api/v2/progress/resolve` `{assetId, expectedRevision, checkpointId}` | Choose one conflict candidate for every device. |
| `POST /api/v2/bookmarks` | A bookmark event → receipt. |
| `POST /api/v2/handoff`, `/api/v2/handoff/ack`, `GET /api/v2/commands` | Continue on another device (below). |
| `POST /api/v2/archives`, `PUT /api/v2/archives/<transfer>/resources/<resource>?offset=`, `POST /api/v2/archives/commit`, `POST /api/v2/archives/verify` | Upload a book imported on the phone to the Mac (resumable, 4 MB chunks, SHA-256 checked) and get a retention receipt before the phone frees space. |
| `POST /api/v2/activity` `{items, sentAt}` | Reading/watching intervals for Statistics; each item answered `applied`, `duplicate` or `rejected` with a reason. |
| `POST /api/v2/inventory` | What this device has downloaded, for the Mac's library icons (below). Capability `inventory`. |

Both catalog routes answer with an `ETag` (a hash of the answer: the same library, preparation
state and errors give the same tag) and `304` (no body) to a matching `If-None-Match`
(capability `catalog-etag`). The phone keeps its last applied answer on disk with the tag and
sends it on the next refresh (launch, foreground, network change); on `304` it reuses the kept
answer and saves only manifests missing locally (and marks as this Mac's every listed book not
marked yet). The phone reads the catalog's lists item by item: a book it cannot read (a field
missing or of another type, a manifest its rules refuse) is left out and logged, the others load,
and such a catalog counts as incomplete (nothing is marked removed on the Mac).
`tests/test_fix_rv5_ios.py` checks the Mac's answers against the fields the Swift structs need. An older Mac never sends `ETag` (the phone keeps
nothing and always gets the full answer); an older phone never sends `If-None-Match`.

Reading, video, study and statistics are under `/api/v2/media/…` (`ln`, `ln/<id>`,
`ln/<id>/chapters/<n>`, `ln/<id>/position`, `<ln|manga>/<id>/package`, `manga`, `manga/<id>`,
`manga/<id>/pages/<n>[/text]`, `manga/<id>/ocr[-package]`, `manga/<id>/position`, `manga/<id>/spreads`,
`downloads/check`, `episodes`, `episodes/<id>[/prepare|/subtitles|/position|/subtitle-delay|/watched|/gate/…]`,
`anime`, `planning`, `cover/<kind>/<id>`, `ln-image/<name>`, `streams/<id>/keepalive|close`, `reader`, `subtitle-look`, `study/…`, `stats`,
`grammar/…`). A refused media request is HTTP 422 `{"error": "<readable reason>"}`; an unknown one 404.

## Episodes: one copy each, subtitle timing, Skip opening/ending

- Smaller copies (capability `video-quality`): `quality` (`original`, `smaller` 720p, `saver` 540p)
  as `?quality=` on `episodes/<id>` and in the body of `.../prepare` and `.../stream`; each episode
  and episode state lists `qualities: [{id, sizeBytes?, prepared, estimated?}]`.

- `GET media/anime` lists one item per episode number of a title. Two library files of the same
  episode (an older single download and a later batch) are one item: the copy the Mac plays
  (`database.preferred_copy`: the file on disk next to more of the title's episodes, then the
  newest row). The other id still answers (a phone that downloaded it keeps it); the phone keeps one
  item per number too and does not download a second copy of an episode it has or is downloading.
- Each item may carry `subtitleDelayMs` (mpv's z/x delay for that subtitle, + later; 0: none) and
  `skipSegments: [{kind: "intro"|"outro", start, end}]` (seconds; the Mac's detected OP/ED, empty
  with Skip opening/ending off). Older phones ignore both; older Macs leave them out.
- `POST media/episodes/<id>/position {seconds, duration, observedAt, sentAt, ranges?}` is sent while
  the player plays (every 15 s, on pause, after a seek, on close and when the app goes to the
  background; kept on the phone while the Mac is away, the newest place by `observedAt` wins). The
  Mac's Home shows it under Continue Watching and mpv opens there. `ranges: [[start, end], …]`
  (seconds played on the phone until its watch counted) join the parts mpv played
  (`pudge/watch_coverage.py`, overlaps once): when the union reaches the usual rule (65 %, ≥ 3 min)
  and some device reached the end (85 % on the phone, mpv's watched point), the episode counts like
  a watch (AniList, schedule, cleanup), once; the answer is then `{"ok": true, "counted": true}`.
  mpv's own count and `episodes/<id>/watched` use the same union.
- `POST media/episodes/<id>/subtitle-delay {ms}` keeps the phone's timing as mpv's delay for the
  same subtitle (the next mpv launch starts with it); 0 forgets it. The phone keeps its timing per
  episode and sends it later when the Mac is away; an older Mac (404) keeps it phone-only.
- The player shows Skip opening / Skip ending as mpv does: during the first 5 s of a segment (and
  while the controls are shown inside it); a tap seeks to its end. Downloaded episodes keep the
  segments for offline playback. No automatic skipping (the Mac has none).

## Explain grammar (light-novel reader, manga)

The Mac asks its configured LLM exactly as its own reader's "Explain grammar" and reading assistant
do (same prompts with the book text quoted as data, same cache, answers in the Mac's interface
language); `pudge/iphone_grammar.py`. The chats are kept on the phone (the last five per book).

- `POST media/grammar/explain {sentence, context}` (≤ 2000 / last 4000 characters) and
  `POST media/grammar/ask {sentence, context, points: [{pattern, meaning}], messages: [{role, content}]}`
  (≤ 40 messages, the question ≤ 4000 characters, body ≤ 384 KB) start an answer and wait up to 5 s:
  `{job, state: "running"}`, `{job, state: "done", explanation: {translation, points: [{pattern,
  meaning, explanation, form, certainty}], partial, summary}}` (ask: `reply`), or
  `{job, state: "failed", error, detail: ""}` (the provider's own error stays in the Mac's log).
  `summary` is what the phone sends back as that first answer.
- `GET media/grammar/jobs/<job>?wait=1` waits up to 5 s again; a job is answered only to the
  device that started it and kept 15 minutes.
- The LLM off on the Mac: 422 with the reason (turn it on in Pudge on the Mac). A device has at most
  two running answers it still asks for (polled in the last 20 s) and 30 requests per 10 minutes;
  four run on the Mac in all: 503 with the reason.

## The end of an anime (score wheel, next entry)

`pudge/iphone_finale.py`, the Mac's own end-of-anime flow (score wheel, then the direct sequel).
A counted watch (`POST media/episodes/<id>/watched`, or `position` answering `counted`) of the final
episode (a movie / one-episode title too), or of the latest aired episode of an airing title not
scored yet, adds `finale: {mediaId, title, kind: "final"|"latest", episode, userScore, prompted}`
(`episode`: the rating episode, 0 for "latest"; `prompted`: answered already on the Mac or a phone).

- `POST media/anime/<id>/score {score: 1-10, episode}` → `{ok, score, anilistPending}` (AniList away:
  queued on the Mac and sent with its next refresh); `{skip: true, episode}` → `{ok, skipped}`.
  The phone keeps a score set while the Mac is away and sends it once the Mac answers.
- `GET media/anime/<id>/next-entry` → `{sequel: {mediaId, title, cover, siteUrl, startDate,
  mediaStatus, listStatus, format, episodes, released, canPlanning, canWatching, canDownload} | null}`.
- `POST media/anime/<id>/next-entry {action: "planning"|"watching"|"download"}` →
  `{ok, reason, anilistPending, sequel}` (download reasons as on the Mac: `not_released`,
  `no_release`, `downloads_disabled`, `download_busy`, `no_sequel`).

Watching an episode from the Mac without downloading it (capability `video-stream`):
`POST media/episodes/<id>/stream` answers like `prepare` and, once the copy is ready, adds
`stream: {id, leaseSeconds}`. The Mac keeps that copy while the stream lives: the phone renews it
(`POST media/streams/<id>/keepalive`, every 30 s; 422 when it ended: open a new one) and closes it
with the player (`POST media/streams/<id>/close`); without a renewal or byte range for
`leaseSeconds` (90), or when the device is revoked, it ends. The player reads the copy as byte
ranges of `GET /api/v2/resources/<resource>/content?sha256=&stream=<id>` (moov first: written with
`-movflags +faststart`); a range of a stream renews it and its last byte is no download. An older
Mac without `video-stream` ignores `stream=`: the phone still plays from it (its copy stays two hours
after the last request), and a range that reaches the last byte counts there as a download (only
the Mac's cache bookkeeping).

## Manifests

`protocolMajor` 2, `assetId` (stable per book), `editionId` and `timelineRevision` (change when
the audio bytes change: a new edition), `title`, `durationMs`, `parts` (`partId`, `resourceId`,
`sha256`, `sizeBytes`, `fileName`, `offsetMs`, `durationMs`, a continuous timeline), `chapters`,
optional `cover`, `readAlong` (`readalong.json`, ≤ 50 MB), `seriesTitle`, `volume`,
`lightNovelId`. A resource ID is derived from the SHA-256 of its bytes; the Mac serves it from
wherever those bytes are now (a moved folder keeps working). The phone stores manifests by
`assetId:editionId`; after a complete catalog it marks the Mac's books no longer listed as
"older edition" or "removed on the Mac" (shown only while downloaded).

## Positions (checkpoints)

A checkpoint: `checkpointId` (UUID, the idempotency key), `assetId`, `editionId`,
`timelineRevision`, `playbackSessionId`, `deviceId`, `sequence` (per session), `baseProgressRevision`
(the revision it continues), `positionMs`, `partId`, `partPositionMs`, `reason` (`periodic`,
`pause`, `seek`, `chapter`, `finish`, `reset`, `unfinish`, `interruption`, `background`, `stop`,
`handoff`), `observedAt` (phone clock, ms) and, for merged periodic saves, `continuousSince`
(`observedAt`, `positionMs` where uninterrupted playback began; capability `checkpoint-runs`).

Receipt: `{checkpointId, revision, status}` with `status`:

- `accepted` — it is the shared position now (`revision` + 1). It is accepted when it continues
  the current revision, or continues its own session after the last resolution.
- `outdated` — an older save of the same session.
- `conflict` — another device moved meanwhile: kept as a candidate (one per device and session);
  every device shows "Choose where to continue" until one is chosen (`/progress/resolve`).
- `rejected` with `reason` — final, never retried: `unknown_asset` (removed on the Mac),
  `edition_changed` (the Mac has a newer edition), `device_changed` (saved under an earlier ID of
  this phone).

The same `checkpointId` sent again gets the same receipt (kept 30 days). The same ID with other
data is an error (HTTP 409). A queued item the Mac refuses with HTTP 409/422 is set aside by the
phone (kept for diagnostics) and the rest of the queue is still sent. While the phone cannot send,
it merges consecutive `periodic` checkpoints of one session into the newest one with
`continuousSince`; the Mac counts that run as listening time (0.4–3.2× playback speed).

Mac clock times (`receivedAt`) decide "is it still playing there" (45 s) and the order of
candidates; `observedAt` is the phone's clock. Finished is set by `finish` and cleared only by
`unfinish` (capability `progress-unfinish`; older Macs refuse it); `reset` (Start over) moves the place and keeps Finished.

## Bookmarks

An event: `bookmarkId`, `eventId` (idempotency key), `assetId`, `editionId`, `positionMs`,
`title`, `deleted`, `baseRevision` (the revision it edits). Receipt `{eventId, revision}`, or
`{eventId, revision, status: "rejected", reason}`: `bookmark_changed` (changed on the Mac or
another device first — the phone drops its copy and takes the Mac's), `unknown_asset`,
`edition_changed`. Bookmarks made on the Mac appear as events of device `mac`.

## Light-novel read marker ("Read up to here")

One marker per volume, the same on the Mac and every paired device (`pudge/ln_read_marker.py`).
It is the reading-progress marker shown in the text, not a list of bookmarks (the audiobook
bookmarks above are a separate list). `GET media/ln/<id>` carries `marker` (or null): `chapter`,
`paragraph` (the reader paragraph, illustrations included), `paragraphOffset` (UTF-16 in its
text), `char` (characters from the chapter start), `kind` (`manual` or `auto`), `at` (seconds),
`device`, `deviceName`, `here` (set by the asking device).

- `POST media/ln/<id>/marker` `{chapter, paragraph, paragraphOffset, kind, markerId?, observedAt?,
  sentAt?}` → `{ok, moved, marker}`: "Read up to here" (`manual`) or the sentence the linked
  audiobook reads (`auto`). A marker set without the Mac waits on the phone (every manual one,
  the newest automatic one per volume) and is sent with the queued places, with the time it was
  set (`observedAt`, moved by the phone's clock difference `now - sentAt`). The same `markerId`
  again (per device, kept 30 days; only manual and queued markers carry one) changes nothing
  and gets the first answer with `duplicate: true`. A volume deleted on the Mac is HTTP 422
  (dropped), 503 is "later", and 404 (an older Mac) ends the phone's queue for that Mac.
- `POST media/ln/<id>/position {finished: true, finishOnly: true}` (capability `ln-finish`) marks a
  volume read (a finished audiobook's "Mark light novel read") without touching its place or
  marker; an older Mac gets the list's place with `observedAt` 1.
- `POST media/ln/<id>/position` may add `paragraph` (the paragraph at the top): the place moves
  the marker as `auto` and the answer carries `marker`. Without `paragraph` (older apps) the
  marker moves by the paragraph of `offset` and the answer is unchanged.

With "Auto-bookmark after 20 s" off on the Mac (`media/reader` → `autoBookmarks: false`) nothing
automatic moves the marker. Rules: `manual` sits exactly at the character; the newest wins, and one sent late still counts
when it is further ahead. `auto` only moves forward, by at least 300 characters (counted through
the chapters between), snapped to its sentence start; it replaces a `manual` marker only when it
is both further ahead and newer.

## Manga two-page view (iPhone/iPad landscape)

When the reader is wide enough for two pages (decided by the space and the page shape, not the
device), pages are shown in pairs like a printed book, in the volume's reading direction (`rightToLeft`:
the lower page on the right). The pairs are the Mac's two-page table (`pudge/manga_spreads.py`;
the phone's copy is tested against the same cases, `tests/fixtures/manga_spreads.json`): the cover
alone, then pairs, a double-page spread alone and the pairs starting again after it; with the
volume's "Shift pairs by one page" the page after the cover is alone too.

- `GET media/manga/<id>/spreads` → `{wide: [page], aspect, shift, spreads: [[page]], align, alignment}`: the pages that
  are one wide picture (by name "012-013.jpg" or wider than 1.15 × their height), the usual page's
  height / width (null: unknown), the shift setting and the table. Older Macs send only `wide` and
  `aspect` (no shift); Macs before the two-page view answer 404.
- `POST media/manga/<id>/spreads {shift}` turns the shift on or off for the volume (kept on the Mac,
  the same setting as the Mac's reader) and answers as the GET. A change made without the Mac is kept
  on the phone and sent before the next GET.
- Scanned pages are straightened and aligned (the Mac works it out once per page, in the
  background: `pudge/manga_align.py`). The GET also has `align` (the volume's "Align scanned pages",
  on unless turned off) and `alignment` (null: the Mac cannot work it out) =
  `{version, pending, single: {"<page>": transform}, pairs: {"<low>-<high>": {"rtl"|"ltr": [transform|null, transform|null]}}}`:
  pages shown alone are only straightened; a pair's two transforms are in page order for each reading
  direction; `pending` > 0: ask again a little later. A transform `{angle, scale, dx, dy, gutter?, backing?, fill?}`
  turns the drawn page `angle` degrees clockwise and scales it around its centre, then moves it by
  `dx` of its width and `dy` of its height, its text boxes with it; a pair is drawn at one height,
  each page cut off at the gutter line (`gutter`: its side there) with `backing` (`[x0, y0, x1, y1]`
  of its box) painted `fill` behind it. The same cases as the Mac reader: `tests/fixtures/manga_alignment.json`.
  `POST media/manga/<id>/spreads {align}` turns it on or off like the shift (both may be sent together).
- A downloaded volume's `package.json` has `widePages` and `pairShift` (its files are renamed
  `page-<n>`, so their spread names are lost), `pageAlign` and `alignment` as far as the Mac had it
  then; an older package reads the shapes from its files.
- The place stays one page: `POST media/manga/<id>/position {page, read, readThrough?}` sends the first
  page of the pair turned to (reading order); `readThrough` = `page + 1` marks the pair's second page
  read too (any other value is ignored; an older Mac ignores it and marks only `page`). Turning the
  device or pairing the pages anew saves nothing.
- Statistics: an activity item's `from`/`to` may add `pageEnd` (= `page + 1`) while two pages are on
  screen; both pages count as visited, each once (locator `page_ids`).

## Downloads on the devices (Mac library icons)

The Mac's library shows which paired iPhones/iPads have a title downloaded (`pudge/iphone_inventory.py`,
`pudge/web/device_copies.js`; phone: `DeviceInventory` in PudgeCore, sent by `PudgeModel`).

- `POST /api/v2/inventory` `{idiom, episodes, lightNovels, manga, audiobooks}` → `{ok, receivedAt, counts}`:
  the whole inventory, in place of the device's last report. `idiom` is `phone` or `pad`; the lists hold
  the ids of this API (`media/episodes/<id>`, `media/ln/<id>`, `media/manga/<id>`, audiobook `assetId`),
  at most 5000 each, body at most 512 KB. Ids below 1 (downloads of a previous Mac, renumbered on the
  phone) are ignored; anything else invalid refuses the report (HTTP 409) and keeps the stored one.
- The phone sends it after connecting or refreshing and, 3 s after the downloads settle, when an
  episode, volume or audiobook finished downloading or was removed; the same inventory again only
  after 6 hours (and always after pairing again or switching Macs). Macs without the capability get none.
- The Mac keeps one report per paired device; revoking the device drops it (a report still in
  flight is refused with HTTP 401), and pairing the same device again starts without one. The window shows only devices still paired, under their pairing names; a report
  older than 7 days is shown "as of <date>". Anime cards: an iPhone/iPad glyph in the cover's top-left
  corner, the tooltip says how many episodes each device has; light-novel, manga and audiobook cards:
  the glyph in the title line; volume rows and Planning's episode buttons: the devices that have it.

## Handoff ("Continue")

- Phone takes over from the Mac or another phone: `POST /api/v2/handoff` `{operationId, assetId,
  expectedRevision, sourceSessionId, targetReady}` → `source_paused` (with `positionMs`,
  `revision`), `pending` (the other phone pauses on its next sync; ask again), `revision_conflict`,
  `source_unreachable` or `target_not_ready`. A paused source needs no dialog.
- The source phone polls `GET /api/v2/commands`, pauses, sends its final `handoff` checkpoint and
  `POST /api/v2/handoff/ack` `{operationId, checkpointId}`.
- Mac takes over from the phone: the Mac's window asks the phone through the same commands; the
  Mac starts only after the phone's acknowledged pause. Unanswered handoffs end after a minute.

## Storage on the Mac

`<data dir>/iphone/`: `server-secret` (the identity; the server ID is derived from it),
`tls-*.pem`, `objects/` (archived phone books, covers, read-along files), `prepared/` (AAC copies:
20 GB budget, 30 days; copies a paired device downloaded completely are kept), `video/`,
`reading/`, `staging/`. Checkpoint and bookmark receipts and listening marks older than 30 days
and finished handoffs older than 7 days are pruned hourly. A full backup carries the identity;
restoring it asks whether this Mac replaces the backed-up one (identity and pairings return) or
is a new Mac (it keeps its own identity and phones pair with it anew).

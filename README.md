# Pudge

Pudge is a Mac app for watching anime with Japanese subtitles and keeping your
anime, manga, Light Novels, and audiobooks together. It can find new episodes,
prepare subtitles, play video through mpv, and update AniList without turning
the setup into a collection of separate scripts.

Current version: **0.7.23**.

## What Pudge can do

- find anime releases through Nyaa and download them with its built-in downloader;
- use qBittorrent instead when you want its extra torrent-management controls;
- find, check, and repair Japanese subtitle timing automatically;
- keep watched progress in sync with AniList;
- read EPUB/TXT Light Novels and CBZ/ZIP manga;
- play M4B, MP3, Opus, and FLAC audiobooks, including paired reading with a Light Novel;
- open a private companion page on your phone or tablet for reading and local playback.

Pudge keeps its library and progress on your Mac. Optional online services are
used only for the features you enable. Exported backups do not contain API
tokens.

For everyday instructions, see the [user guide](docs/USER_GUIDE.md). For the
rules behind matching, subtitles, and media states, see
[algorithms and state model](docs/ALGORITHMS.md).

## Requirements

- macOS 14 or newer; Apple Silicon is recommended;
- [Homebrew](https://brew.sh/);
- accounts or tokens only for the integrations you choose, such as Jimaku,
  AniList, or Jiten. The official jpdb-mpv-plugin handles its own authorization.

The installer adds mpv, ffmpeg/ffprobe, ALASS, 7-Zip, aria2, and Python 3.12
through Homebrew. During initial setup, Pudge checks mpv and ffmpeg and can
repair a missing Homebrew installation. It can also install the separate
[JitenMPV](https://github.com/Sirush/JitenMPV) plugin when you explicitly select
it.

Automatic downloads work without a separate torrent app: Pudge starts its own
local aria2 process when needed. qBittorrent remains an advanced alternative in
Settings. Versions below 5.2 use a Web UI username and password;
API-key authentication requires qBittorrent 5.2 or newer.

## Install a release

Download `pudge-macos-vX.Y.Z.zip` from GitHub Releases, then run:

```bash
cd ~/Downloads
unzip pudge-macos-v0.7.23.zip
cd pudge
./install.sh
```

After installation, use **Settings → Application updates** to check for a new
version. Release installs verify the published SHA-256 checksum and restore the
previous app bundle automatically if installation fails.

The first subtitle transcription may download a small MLX Whisper model. Pudge
uses transcription only after ordinary timing methods fail. Audiobook
transcription runs after import, continues in the background, and is cached by
content.

## Jimaku and AniList credentials

### Jimaku

Official release builds may provide shared Jimaku access for the first 48
hours. A personal key always takes priority, and the shared build key is never
stored in the repository.

1. Open [Jimaku registration/login](https://jimaku.cc/login) and choose **Register**.
2. After signing in, open [your Jimaku account](https://jimaku.cc/account) and copy the API key.
3. In Pudge, open **Settings → Jimaku**, paste the key, and save.

### AniList

1. Sign in to AniList and open [Developer settings](https://anilist.co/settings/developer).
2. Choose **Create New Client**, name it `Pudge`, and use `https://anilist.co/api/v2/oauth/pin` as the redirect URL.
3. Copy the numeric Client ID into **Settings → AniList → Client ID**.
4. Open **Get key**, approve access on AniList, and paste the issued token into Pudge.
5. Save the settings. Pudge updates AniList data automatically after the credentials change.

Treat the AniList token like a password. This flow follows AniList's
[implicit-grant authentication guide](https://docs.anilist.co/guide/auth/).

## MangaOCR

Manga reading works without OCR. To add on-demand Japanese text recognition to
an installed release:

```bash
~/.local/share/pudge/venv/bin/python -m pip install "manga-ocr>=0.1.14,<1"
```

MangaOCR downloads its model the first time it runs. Pudge does not run OCR
while you are simply browsing pages.

## Remove Pudge

Open **Settings → Remove Pudge** and click the red
**Delete Pudge from this Mac** button. After two confirmations, Pudge removes
the app, its command-line tools, LaunchAgent, settings, database, Pudge-created
backups in Downloads, cache, logs, paired devices, Pudge Keychain entries, and
files in the Pudge library folder.

Folders you added only for watching or subtitle search are left alone. Shared
tools such as Homebrew, mpv, qBittorrent, and JitenMPV are also left installed
because other apps may use them.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sync]"
make test-batches
make lint
```

Add `.[manga]` when working on OCR. More details are in
[DEVELOPMENT.md](DEVELOPMENT.md).

## Troubleshooting

- If an episode appears under **Action required**, open the setting named on the card.
- For subtitle problems, open **Settings → Diagnostics** and include the relevant log details in a bug report.
- If a mobile library looks old, bring the companion page to the foreground. It refreshes immediately and every 15 seconds while visible.
- Backups keep the library, settings, and prepared subtitle cache. Restoring a backup keeps the credentials already installed on that Mac.

Report security issues through [SECURITY.md](SECURITY.md). Contribution and
release instructions are in [CONTRIBUTING.md](CONTRIBUTING.md) and
[RELEASING.md](RELEASING.md).

## License

[MIT](LICENSE)

# Pudge

Pudge is a macOS media companion focused on anime with Japanese subtitles. It combines a local library, AniList progress, Nyaa downloads, qBittorrent/aria2, mpv playback, subtitle discovery and automatic timing repair in one native window.

Current version: **0.7.16**.

## What it does

- prepares Japanese subtitles automatically and rejects low-confidence timing instead of asking the viewer to edit them;
- uses embedded subtitle clocks and container chapters around openings/transitions, with cached tiny Japanese STT as a last resort;
- keeps anime lists and watched progress in sync with AniList;
- finds releases through Nyaa and downloads them with its built-in downloader; qBittorrent remains an optional advanced backend;
- reads EPUB/TXT light novels, CBZ/ZIP manga, and M4B/MP3/Opus/FLAC audiobooks;
- keeps Activity out of the primary navigation while surfacing only genuine user-action blockers on Home.

Pudge is local-first. The optional local LLM is disabled for subtitle decisions by default. API tokens are not included in exported backups.

See the [user guide](docs/USER_GUIDE.md) for end-to-end scenarios and [algorithms/state model](docs/ALGORITHMS.md) for the matching, subtitle, pitch-accent and job rules.

## Requirements

- macOS 14 or newer, preferably Apple Silicon;
- [Homebrew](https://brew.sh/);
- accounts/tokens only for the integrations you enable (Jimaku, AniList or Jiten);
  the official jpdb-mpv-plugin manages its own authorization.

The installer adds mpv, ffmpeg/ffprobe, ALASS, 7-Zip, aria2 and Python 3.12 through Homebrew.
First experience also checks mpv and ffmpeg and can repair a missing Homebrew
installation of either component. It can optionally install the separate
[JitenMPV](https://github.com/Sirush/JitenMPV) plugin for interactive subtitle
words inside mpv and reuse the Jiten API key entered in Pudge settings.
Automatic downloads need no torrent-client setup: Pudge starts its private local aria2 process when needed.
qBittorrent can still be selected in Settings as an advanced alternative. Versions below 5.2 use
the Web UI username and password; API-key authentication requires qBittorrent 5.2 or newer.

## Install a release

Download `pudge-macos-vX.Y.Z.zip` from GitHub Releases, then:

```bash
cd ~/Downloads
unzip pudge-macos-v0.7.16.zip
cd pudge
./install.sh
```

After installation, Settings → Application updates can check GitHub manually.
Release installs verify the published SHA-256 checksum before reinstalling;
development checkouts update only from the official origin when the current
branch is clean and can be fast-forwarded. The previous app bundle is restored
automatically if installation fails.

The first STT fallback may download a small MLX Whisper model. Subtitle transcription only runs after deterministic alignment methods fail. Audiobook transcription starts automatically after import, continues in the background outside the reader, reports live progress, and is cached by content. Linked Light Novels are aligned automatically when that transcript is ready.

## Jimaku and AniList credentials

### Jimaku

Official release builds may include a shared Jimaku key for the first 48 hours. A personal key always takes priority. The shared key is injected from a GitHub Actions repository secret during release builds and is not committed to the source tree.

1. Open [Jimaku registration/login](https://jimaku.cc/login) and choose **Register**. Jimaku asks only for a username and password; no email is required.
2. After signing in, open [your Jimaku account](https://jimaku.cc/account) and copy the API key.
3. In Pudge, open **Settings → Jimaku**, paste the key, and save.

### AniList

1. Sign in to AniList and open [Developer settings](https://anilist.co/settings/developer).
2. Choose **Create New Client**. Set the name to `Pudge` and the redirect URL to `https://anilist.co/api/v2/oauth/pin`, then save.
3. Copy only the numeric Client ID into **Settings → AniList → Client ID** in Pudge.
4. Pudge then reveals **Get key**. Open it, authorize the application on AniList, and copy the issued token into the newly shown field.
5. Save Pudge settings. Pudge updates AniList data automatically after the credentials change.

The token grants access to your AniList account and should be treated like a password. The flow follows AniList's [implicit-grant authentication guide](https://docs.anilist.co/guide/auth/).

Manga reading works without OCR. To enable on-demand [MangaOCR](https://github.com/kha-white/manga-ocr) in an installed release:

```bash
~/.local/share/pudge/venv/bin/python -m pip install "manga-ocr>=0.1.14,<1"
```

MangaOCR downloads its model on first use. Pudge never runs it while merely browsing pages.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sync]"
make test-batches
make lint
```

Use `.[manga]` as well when developing the OCR path. See [DEVELOPMENT.md](DEVELOPMENT.md) for the test layout.

## Troubleshooting

- If an episode appears under **Action required**, open the indicated setting. Pudge uses that section only for missing Jimaku credentials, macOS folder access, or disabled bitmap OCR—not for ordinary network retries.
- Subtitle timing details and engine evidence are recorded in the app log. The visible Activity page remains intentionally hidden; maintenance and diagnostics are available from Settings.
- Backups preserve the library, settings and cached prepared subtitles, but keep the currently installed credentials when restored.

Please report security issues using [SECURITY.md](SECURITY.md). Contribution and release workflows are in [CONTRIBUTING.md](CONTRIBUTING.md) and [RELEASING.md](RELEASING.md).

## License

[MIT](LICENSE)

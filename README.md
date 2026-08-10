# Pudge

Pudge is a macOS media companion focused on anime with Japanese subtitles. It combines a local library, AniList progress, Nyaa downloads, qBittorrent/aria2, mpv playback, subtitle discovery and automatic timing repair in one native window.

Current version: **0.6.77**.

## What it does

- prepares Japanese subtitles automatically and rejects low-confidence timing instead of asking the viewer to edit them;
- uses embedded subtitle clocks and container chapters around openings/transitions, with cached tiny Japanese STT as a last resort;
- keeps anime lists and watched progress in sync with AniList;
- finds releases through Nyaa and manages downloads with qBittorrent or the bundled aria2 fallback;
- reads EPUB/TXT light novels, CBZ/ZIP manga, and M4B/MP3/Opus/FLAC audiobooks;
- keeps Activity out of the primary navigation while surfacing only genuine user-action blockers on Home.

Pudge is local-first. The optional local LLM is disabled for subtitle decisions by default. API tokens are not included in exported backups.

## Requirements

- macOS 14 or newer, preferably Apple Silicon;
- [Homebrew](https://brew.sh/);
- accounts/tokens only for the integrations you enable (Jimaku, AniList, qBittorrent, Jiten or JPDB).

The installer adds mpv, ffmpeg/ffprobe, ALASS, 7-Zip, aria2 and Python 3.12 through Homebrew.

## Install a release

Download `pudge-macos-vX.Y.Z.zip` from GitHub Releases, then:

```bash
cd ~/Downloads
unzip pudge-macos-v0.6.77.zip
cd pudge
./install.sh
```

The first STT fallback may download a small MLX Whisper model. Transcription only runs after deterministic alignment methods fail, and its result is cached by video fingerprint.

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

# Pudge

Pudge is a macOS anime library/player manager built around AniList, Nyaa, qBittorrent/aria2, mpv and Japanese subtitles.

Current version: **0.6.71**.

## Install a release

Download `pudge-macos-vX.Y.Z.zip` from GitHub Releases, then:

```bash
cd ~/Downloads
rm -rf pudge
unzip pudge-macos-v0.6.71.zip
cd pudge
./install.sh
```

The installer sets up the managed Python environment and required macOS/Homebrew tools.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sync]"
```

Run the whole test suite normally:

```bash
make test
```

Or run the whole suite in deterministic batches:

```bash
make test-batches
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for details.

## Releases

Version tags are automated. Pushing a tag such as `v0.6.69` runs the full test suite in four macOS batches, builds the release ZIP and attaches it to a GitHub Release.

See [RELEASING.md](RELEASING.md).

## Release notes

Historical release notes are kept in [CHANGELOG.md](CHANGELOG.md).

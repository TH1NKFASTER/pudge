# Pudge development

## Local setup

Pudge is developed on macOS and uses Python 3.12 for the installed application.

```bash
cd ~/Downloads/pudge
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sync]"
```

## Product and UI copy

Keep implementation details out of user-facing copy. Explain what an action
does, when it runs, and what the user should expect. Internal cache names,
scoring formulas, pipeline stage names, and retry machinery belong in source,
tests, or diagnostics unless the detail helps the user make a decision.

Use the same names that appear in the app. Prefer ordinary phrases such as
"initial setup", "refresh", "repair subtitles", and "remove Pudge" over
internal project labels.

The same rule applies to documentation: write the shortest explanation that is
still accurate. Developer-facing protocol and release documents may be more
technical where precision is necessary.

## Tests

Run the complete suite:

```bash
make test
```

Or run the complete suite in deterministic batches:

```bash
make test-batches
```

Run one batch manually:

```bash
python scripts/run_test_batch.py --batch 0 --batches 4
```

Every `tests/test_*.py` file belongs to exactly one batch. Behavior fixes should
include a regression test for the user-visible failure, not only for the new
implementation shape.

## Lint and static checks

```bash
make lint
python -m compileall -q pudge scripts
```

Changed JavaScript modules should also pass `node --check`.

## Build a release ZIP locally

```bash
make build-release
```

The resulting archive is written to `dist/pudge-macos-vX.Y.Z.zip`.

Generated wheels, ZIPs, virtual environments, caches, logs and local config are
ignored by git.

## Release bundle contract

The published source release ZIP includes the documented developer tooling needed by the commands above, including `Makefile`, `scripts/`, `MOBILE_SYNC_PROTOCOL.md`, and the static workflow files used by source-level release checks. `scripts/check_release_bundle.py` validates the staged archive so internal documentation links and documented commands do not silently disappear from a release.

Release automation requires a clean working tree before the version bump and stages only the files it intentionally updates. It must not use `git add -A` to absorb unrelated local work.

## Legacy Anime MPV migration

The one-time Anime MPV → Pudge compatibility migration lives at `scripts/migrations/legacy_anime_mpv.py`. It is deliberately narrow: it migrates known legacy defaults only, never performs arbitrary product renames, does not follow unexpected symlinks, and leaves conflicting source files untouched. Configuration changes are written atomically with a backup, and similar-looking custom paths are not rewritten merely because they contain the legacy product name.

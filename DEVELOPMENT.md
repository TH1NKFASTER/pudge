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

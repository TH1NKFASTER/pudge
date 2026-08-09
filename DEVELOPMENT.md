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

## Tests

Run the complete suite:

```bash
make test
```

Run the complete suite in four deterministic batches:

```bash
make test-batches
```

Run one batch manually:

```bash
python scripts/run_test_batch.py --batch 0 --batches 4
```

Every `tests/test_*.py` file belongs to exactly one batch.

## Lint

```bash
make lint
```

## Build a release ZIP locally

```bash
make release
```

The resulting archive is written to `dist/pudge-macos-vX.Y.Z.zip`.

Generated wheels, ZIPs, virtual environments, caches, logs and local config are ignored by git.

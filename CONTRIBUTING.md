# Contributing to Pudge

Pudge targets macOS and Python 3.12. Keep changes local-first, preserve existing
user data, and avoid unnecessary background work while the UI is idle.

## Workflow

1. Create a focused branch from `main`.
2. Install `.[dev,sync]` as described in `DEVELOPMENT.md`.
3. Add regression coverage for behavior changes. For UI work, test the behavior and asset wiring as well as visible copy.
4. Run `make test-batches`, `make lint`, `python -m compileall -q pudge scripts`, and `node --check` for changed JavaScript modules.
5. Open a pull request describing user-visible behavior, migration impact, and macOS-specific testing.

Do not commit credentials, media, generated models, logs, local config, test
caches, or release archives. Subtitle changes should preserve enough selection
and timing evidence for Diagnostics to explain the result.

Releases are performed only through the tagged workflow described in
`RELEASING.md`.

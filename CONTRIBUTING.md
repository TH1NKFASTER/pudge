# Contributing to Pudge

Pudge targets macOS and Python 3.12. Keep changes local-first, preserve existing user data, and avoid adding background work that runs while the UI is idle.

## Workflow

1. Create a focused branch from `main`.
2. Install `.[dev,sync]` as described in `DEVELOPMENT.md`.
3. Add regression coverage for behavior changes. UI changes should include DOM/asset integration coverage, not only string assertions.
4. Run `make test-batches`, `make lint`, `python -m compileall -q anime_mpv scripts`, and `node --check` for changed JavaScript modules.
5. Open a pull request describing user-visible behavior, migration impact and macOS-specific testing.

Do not commit credentials, media, generated models, logs, local config, test caches or release archives. Subtitle changes should retain the original candidate, timing evidence and a deterministic quality gate; user-facing manual timing correction is intentionally out of scope.

Releases are performed only through the tagged workflow described in `RELEASING.md`.

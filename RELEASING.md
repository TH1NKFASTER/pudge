# Releasing Pudge

Release from a reviewed `main` checkout. The release helper refuses unrelated local changes; after it performs the version bump it stages only the explicit release metadata files rather than `git add -A`.

## Normal release

```bash
make release VERSION=<next-version>
```

The helper fetches `origin/main` and tags, checks that the checkout is not stale/diverged, bumps version metadata, refreshes `uv.lock`, runs `make quality` and all four test batches, commits only the release metadata files, pushes `main`, creates the tag, and pushes it.

If the generated changelog entry still contains `TODO: release notes`, validation stops. Edit only the generated release metadata and rerun the same command; unrelated working-tree changes remain a hard error.

Use `--no-push` through `scripts/release.py` when validating the release commit without publishing it.

## CI gates

Tag releases require both:

- the same `make quality` gate used locally;
- all four test batches and release metadata validation.

The release archive is built only after both jobs succeed.

## Release archive contract

The macOS source archive includes runtime sources plus the developer paths referenced by its documentation: `tests/`, `docs/`, `scripts/`, `Makefile`, `MOBILE_SYNC_PROTOCOL.md`, and release workflow fixtures. `scripts/check_release_bundle.py` validates required paths and local Markdown links before the ZIP is created.

The archive intentionally excludes user databases, caches, OCR corpora/fixtures that are not licensed for redistribution, logs, secrets and model caches.

## GitHub Release notes

The release workflow extracts the matching `## vX.Y.Z` section from `CHANGELOG.md` and publishes that text as the GitHub Release body. It does not rely on GitHub-generated notes. Re-running the workflow for an existing tag refreshes both the assets and the Release body, so old releases can be backfilled after release-note workflow fixes.

# Contributing

Use Python 3.12. Install development dependencies with `python -m pip install -e ".[dev,sync]"`.

Run `make test` for the complete synthetic source suite and `make quality` for release checks. The macOS installer runs the same suite before changing the application. OCR tests must use generated pages; copyrighted pages and private corpora must never be committed.

Build an archive with `./build_release.sh`. Release versions, tags and ZIP names use `x.y.z`. Older `vX.Y.Z` tags remain readable. `make release VERSION=x.y.z` verifies metadata, tests and the lockfile before creating a tag.

Benchmark tools, research reports and old documentation are kept outside the public repository. Applying v28 preserves a verified local archive and provides rollback.

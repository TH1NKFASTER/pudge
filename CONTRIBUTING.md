# Contributing

Use Python 3.12. Install development dependencies with `make install-dev` (`uv sync --locked`: exactly the versions in `uv.lock`, into `.venv-test`). After changing dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`; CI and installs from a checkout refuse a lock that is out of date.

Run `make test` for the complete synthetic source suite and `make quality` for release checks. Python code is formatted with ruff (`make format`; `make lint` and CI check all of it, ruff 0.16 as pinned in the `dev` extra). The macOS installer runs the same suite before changing the application. OCR tests must use generated pages; copyrighted pages and private corpora must never be committed.

Build an archive with `./build_release.sh`. A release of `x.y.z` is the commit `Pudge vx.y.z`, the tag `vx.y.z` and the GitHub Release `Pudge vx.y.z` with `pudge-macos-vx.y.z.zip` and its `.sha256`. The `0.7.30` tag without the `v` remains readable. `make release VERSION=x.y.z` verifies metadata, tests and the lockfile before creating a tag.

Benchmark tools, research reports and old documentation are kept outside the public repository. Applying v28 preserves a verified local archive and provides rollback.

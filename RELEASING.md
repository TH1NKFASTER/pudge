# Releasing Pudge

Pudge releases are built from an exact version tag. The release script checks
the branch, runs every required check, creates the commit and tag, and pushes
only after all checks pass.

## Standard release

From a clean `main` checkout with the release changes already reviewed:

```bash
make release VERSION=0.7.23 PYTHON=.venv-test/bin/python
```

The command:

1. fetches `origin/main` and the existing tags;
2. stops if the checkout is stale, diverged, or contains an unsafe version tag;
3. updates the version in package and documentation files;
4. runs lint and all four test batches;
5. checks the staged diff for whitespace errors;
6. commits the release, pushes `main`, and creates the matching tag.

Public tags are never moved. If a step fails, fix the cause and start again
instead of forcing the tag or publishing an unchecked archive.

## Build without publishing

Use this when you need to inspect the archive but do not want a commit or tag:

```bash
make build-release
```

The archive is written to `dist/pudge-macos-vX.Y.Z.zip`.

## Run the checks yourself

The release command runs these checks automatically, but they are useful before
the final review:

```bash
make test-batches
make lint
python -m compileall -q pudge scripts
```

## What GitHub Actions publishes

After a valid version tag is pushed, GitHub Actions:

1. verifies that the tag matches `pudge.__version__`;
2. runs the full suite in four macOS test batches;
3. builds `pudge-macos-vX.Y.Z.zip`;
4. creates the GitHub Release;
5. attaches the ZIP and its SHA-256 checksum.

No archive is published when a test batch or metadata check fails. The tag must
always point to the reviewed release commit.

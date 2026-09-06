# Releasing Pudge

Pudge releases are built from an exact version tag. The release command checks
the checkout, updates version metadata when needed, runs the required checks,
and publishes only after they pass.

## Standard release

From a clean `main` checkout with the release changes already reviewed:

```bash
make release VERSION=0.7.26 PYTHON=.venv-test/bin/python
```

The command verifies that the checkout is current, updates version metadata,
runs lint and the full test batches, creates the release commit, pushes `main`,
and creates the matching version tag.

Public tags are never moved. If a step fails, fix the cause and start again
instead of forcing the tag or publishing an unchecked archive.

## Build without publishing

Use this when you want to inspect the archive without creating a commit or tag:

```bash
make build-release
```

The archive is written to `dist/pudge-macos-vX.Y.Z.zip`.

## Run the checks yourself

```bash
make test-batches
make lint
python -m compileall -q pudge scripts
```

Before a public release, also read the user-facing README, user guide, changelog,
and algorithms overview once as product documentation rather than as code
comments. Remove stale implementation detail instead of documenting obsolete
internals.

## What GitHub Actions publishes

After a valid version tag is pushed, GitHub Actions verifies the tag/version,
runs the macOS test batches, builds `pudge-macos-vX.Y.Z.zip`, and creates the
GitHub Release with its checksum.

No archive is published when validation fails. The tag must always point to the
reviewed release commit.

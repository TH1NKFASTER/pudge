# Releasing Pudge

GitHub Releases are automated from version tags.

## 1. Update the version

Use the version helper:

```bash
make bump VERSION=0.6.69
```

It updates `anime_mpv/__init__.py`, `pyproject.toml` and the README example together.

## 2. Run checks locally

```bash
make test-batches
make lint
```

## 3. Commit and push

```bash
git add -A
git commit -m "Pudge v0.6.69"
git push
```

## 4. Create and push the tag

```bash
git tag v0.6.69
git push origin v0.6.69
```

GitHub Actions will then:

1. verify that the tag matches `anime_mpv.__version__`;
2. run the full test suite in four macOS batches;
3. build `pudge-macos-v0.6.69.zip`;
4. create a GitHub Release and attach the ZIP.

If any test batch fails, the release ZIP is not published.

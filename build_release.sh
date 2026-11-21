#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
source "$PROJECT_DIR/pudge/brand.env"

TRIAL_KEY_ASSET="$PROJECT_DIR/pudge/assets/jimaku-trial-key"
remove_trial_asset=0
if [[ -n "${PUDGE_TRIAL_JIMAKU_API_KEY:-}" ]]; then
  printf '%s' "$PUDGE_TRIAL_JIMAKU_API_KEY" > "$TRIAL_KEY_ASSET"
  chmod 600 "$TRIAL_KEY_ASSET"
  remove_trial_asset=1
fi
cleanup_trial_asset() {
  if [[ "$remove_trial_asset" == "1" ]]; then
    rm -f "$TRIAL_KEY_ASSET"
  fi
}
trap cleanup_trial_asset EXIT

VERSION=$(python - <<'PY'
from pudge import __version__
print(__version__)
PY
)

rm -rf build dist pudge.egg-info
find pudge tests .github/release -type d -name __pycache__ -prune -exec rm -rf {} +
python .github/release/write_build_info.py pudge "$VERSION" --release
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  python -m pytest -q
fi
find pudge tests .github/release -type d -name __pycache__ -prune -exec rm -rf {} +
python -m pip wheel . --no-deps --no-build-isolation -w dist
command -v uv >/dev/null 2>&1 || { echo "uv is required to build a release" >&2; exit 1; }
uv lock --check
uv export --locked --extra sync --no-dev --no-emit-project --format requirements.txt --output-file dist/release-requirements.txt

STAGE="$PROJECT_DIR/dist/release/$APP_SLUG"
rm -rf "$PROJECT_DIR/dist/release"
mkdir -p "$STAGE" "$STAGE/.github"
if [[ "$(git rev-parse --show-toplevel 2>/dev/null || true)" == "$(pwd -P)" ]]; then
  # A checkout: the release holds the files git tracks (a developer folder's untracked
  # leftovers stay out) and the two this build generated.
  python - "$STAGE" <<'PY'
import os, shutil, subprocess, sys
from pathlib import Path

stage = Path(sys.argv[1])
listed = subprocess.run(["git", "ls-files", "-z", "--", "pudge", "tests"], check=True, capture_output=True).stdout
names = [name for name in listed.decode("utf-8", "surrogateescape").split("\0") if name]
names += ["pudge/build-info.json", "pudge/assets/jimaku-trial-key"]
for name in dict.fromkeys(names):
    source = Path(name)
    if not os.path.lexists(source) or "__pycache__" in source.parts:
        continue
    target = stage / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
PY
else
  cp -R pudge tests "$STAGE/"
fi
cp -R .github/workflows .github/release "$STAGE/.github/"
cp install.sh README.md config.example.toml pyproject.toml build_release.sh Makefile \
  LICENSE SECURITY.md CONTRIBUTING.md CHANGELOG.md uv.lock "$STAGE/"
cp "dist/pudge-${VERSION}-py3-none-any.whl" "$STAGE/"
cp "dist/release-requirements.txt" "$STAGE/"
chmod +x "$STAGE/install.sh" "$STAGE/build_release.sh" "$STAGE/pudge/legacy_install.py"
python .github/release/check_release_bundle.py "$STAGE"

ARCHIVE="$PROJECT_DIR/dist/$APP_SLUG-macos-v${VERSION}.zip"
(
  cd "$PROJECT_DIR/dist/release"
  zip -qr "$ARCHIVE" "$APP_SLUG"
)
python .github/release/finish_release_archives.py "$ARCHIVE"

echo "$ARCHIVE"

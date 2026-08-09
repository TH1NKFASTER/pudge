#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
source "$PROJECT_DIR/anime_mpv/brand.env"

VERSION=$(python - <<'PY'
from anime_mpv import __version__
print(__version__)
PY
)

rm -rf build dist anime_mpv.egg-info
find anime_mpv tests -type d -name __pycache__ -prune -exec rm -rf {} +
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  python -m pytest -q
fi
find anime_mpv tests -type d -name __pycache__ -prune -exec rm -rf {} +
python -m pip wheel . --no-deps --no-build-isolation -w dist

STAGE="$PROJECT_DIR/dist/release/$APP_SLUG"
rm -rf "$PROJECT_DIR/dist/release"
mkdir -p "$STAGE"
cp -R anime_mpv tests "$STAGE/"
cp install.sh README.md config.example.toml pyproject.toml build_release.sh rename_brand.py brand_migration.py "$STAGE/"
cp "dist/anime_mpv-${VERSION}-py3-none-any.whl" "$STAGE/"
chmod +x "$STAGE/install.sh" "$STAGE/build_release.sh"

(
  cd "$PROJECT_DIR/dist/release"
  zip -qr "../${APP_SLUG}-macos-v${VERSION}.zip" "$APP_SLUG"
)

echo "$PROJECT_DIR/dist/${APP_SLUG}-macos-v${VERSION}.zip"

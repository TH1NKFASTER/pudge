from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import MokuroImportError

ROOT = Path(__file__).resolve().parents[1]


def make_archive(path: Path) -> None:
    image = path.parent / "001.png"
    Image.new("RGB", (80, 120), "white").save(image)
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(image, arcname="001.png")


def test_manga_status_preferences_are_durable_and_sanitized(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    initial = service.reader_preferences()
    assert initial["configured"] is False
    assert initial["statusHighlight"] is False

    saved = service.save_reader_preferences(
        {
            "statusHighlight": True,
            "statusOpacity": 999,
            "statusNew": False,
            "statusNewColor": "#ABCDEF",
            "statusKnownColor": "not-a-color",
            "unrelated": "ignored",
        }
    )
    assert saved["configured"] is True
    assert saved["statusHighlight"] is True
    assert saved["statusOpacity"] == 65
    assert saved["statusNew"] is False
    assert saved["statusNewColor"] == "#abcdef"
    assert saved["statusKnownColor"] == "#45cf80"

    restarted = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    restored = restarted.reader_preferences()
    assert restored["configured"] is True
    assert restored["statusHighlight"] is True
    assert restored["statusNew"] is False


def test_manga_state_exposes_persisted_reader_preferences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    service.save_reader_preferences({"statusHighlight": True})
    monkeypatch.setattr(service, "ocr_available", lambda **_: False)
    state = service.state()
    assert state["reader_preferences"]["configured"] is True
    assert state["reader_preferences"]["statusHighlight"] is True
    assert state["ocr_available"] is False


def test_mokuro_direct_selection_resolves_only_unambiguous_adjacent_archive(tmp_path: Path) -> None:
    archive = tmp_path / "book.cbz"
    make_archive(archive)
    sidecar = tmp_path / "book.mokuro"
    sidecar.write_text(json.dumps({"pages": []}), encoding="utf-8")
    assert MangaService.archive_for_mokuro(sidecar) == archive.resolve()

    second = tmp_path / "book.zip"
    make_archive(second)
    with pytest.raises(MokuroImportError, match="more than one archive"):
        MangaService.archive_for_mokuro(sidecar)


def test_mokuro_archive_dot_sidecar_and_known_subdirectory(tmp_path: Path) -> None:
    archive = tmp_path / "book.cbz"
    make_archive(archive)
    explicit = tmp_path / "book.cbz.mokuro"
    explicit.write_text("{}", encoding="utf-8")
    assert MangaService.archive_for_mokuro(explicit) == archive.resolve()

    explicit.unlink()
    nested = tmp_path / "mokuro" / "book.mokuro"
    nested.parent.mkdir()
    nested.write_text("{}", encoding="utf-8")
    assert MangaService.archive_for_mokuro(nested) == archive.resolve()


def test_g20_reader_ux_and_direct_mokuro_contracts_are_wired() -> None:
    index = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "Episode {episode} is not ready'" in index
    assert "Episode {episode} is not ready yet" not in index
    assert "Серия {episode} не готова" in index

    appearance_close = index.index("if($('lnReaderAppearance')?.classList.contains('open'))")
    select_close = index.index("if(window.PudgeSelect?.closeIfOpen?.())return;")
    assert appearance_close < select_close

    assert "hydratePersistedStatusPreferences(state?.reader_preferences)" in manga
    assert "manga_save_reader_preferences(mangaStatusPreferenceSnapshot())" in manga
    assert "button.disabled = active || !available" in manga
    assert "ocrButton.toggleAttribute('disabled', !available)" in manga
    assert "OCR backend is not installed" in manga

    assert "*.cbz;*.zip;*.mokuro" in app
    assert "archive_for_mokuro(path)" in app
    assert "import_mokuro(" in app
    assert "require_full_coverage=True" in app
    assert "if not mokuro_attempted and self._manga_ocr_backend_available()" in app

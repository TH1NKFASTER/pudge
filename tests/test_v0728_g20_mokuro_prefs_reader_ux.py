from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import MokuroImportError
from tests.i18n_support import en, has_text, ru
from tests.t71g06_support import STUDY, column, reader


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


def test_manga_state_exposes_persisted_reader_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    service.save_reader_preferences({"statusHighlight": True})
    monkeypatch.setattr(service, "_mokuro_python", lambda: None)  # Mokuro not installed
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


def test_not_ready_episode_status_names_the_episode(page) -> None:
    assert en("label.episodeNotReady", episode=3) == "Episode 3 is not ready"
    assert ru("label.episodeNotReady", episode=3) == "Серия 3 не готова"
    assert not has_text("Episode {episode} is not ready yet")
    for lang in ("en", "ru"):
        result = page.run(
            "return [episodePresentationStatus({}, 3), episodePresentationStatus({presentation: {status: 'later'}}, 3),"
            " episodePresentationStatus({}, null), t('label.notReady')];",
            lang=lang,
        )
        no_presentation, unknown_status, no_episode, not_ready = result.value
        assert (
            no_presentation
            == unknown_status
            == (en if lang == "en" else ru)("label.episodeNotReady", episode=3)
        )
        assert no_episode == not_ready


def test_one_escape_closes_the_light_novel_appearance_panel_with_its_open_menu(page) -> None:
    result = page.run(
        r"""
        await __click('#lnReaderAppearanceToggle');
        const opened = $('lnReaderAppearance').classList.contains('open');
        $('lnrFont').closest('.pudge-select').querySelector('.pudge-select-button').id = 'fontSelect';
        await __fire('#fontSelect', 'pointerdown', {button: 0}, 'PointerEvent');
        const menu = PudgeSelect.isOpen();
        await __key('Escape'); await __tick(10);
        return {opened, menu, panel: $('lnReaderAppearance').classList.contains('open'), menuAfter: PudgeSelect.isOpen()};
        """
    )
    assert result.value == {"opened": True, "menu": True, "panel": False, "menuAfter": False}


def test_word_state_settings_come_from_the_backend_and_are_saved_back(page) -> None:
    saved = {
        "configured": True,
        "statusHighlight": True,
        "statusNewColor": "#abcdef",
        "statusKnownColor": "not-a-color",
    }
    result = reader(
        page,
        r"""
        const reader = $('mangaReaderV2'), control = key => reader.querySelector(`[data-manga-setting="${key}"]`);
        const color = name => reader.style.getPropertyValue(`--manga-status-${name}`);
        const out = {highlight: control('statusHighlight').checked, newColor: control('statusNewColor').value,
                     colors: [color('new'), color('known')], marks: marks().length, saves: calls('manga_save_reader_preferences').length};
        await __input('[data-manga-setting="statusKnown"]', false); await __tick(50);
        await __input('[data-manga-setting="statusNewColor"]', '#123456'); await __tick(50);
        out.changed = {color: color('new'), marks: marks().map(m => m.word)};
        out.inputs = [...reader.querySelectorAll('.manga-v2-status-palette input')].map(i => [i.dataset.mangaSetting, i.type]);
        return out;
        """,
        regions={0: [column("猫が好き", 0.80, 0.90)]},
        parse={"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]},
        prefs=saved,
        before=STUDY
        + "window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}, '3:0': {states: ['known'], normalizedState: 'known'}};",
    )
    out = result.value
    assert (out["highlight"], out["newColor"], out["marks"], out["saves"]) == (True, "#abcdef", 2, 0)
    assert out["colors"] == ["#abcdef", "#45cf80"]  # an invalid colour falls back to the default
    assert out["inputs"] == [
        ["statusNewColor", "color"],
        ["statusLearningColor", "color"],
        ["statusKnownColor", "color"],
        ["statusDueColor", "color"],
    ]
    assert out["changed"] == {"color": "#123456", "marks": ["猫"]}
    known_off, new_color = [call[0] for call in result.calls_of("manga_save_reader_preferences")]
    assert known_off["statusKnown"] is False and known_off["statusHighlight"] is True
    assert new_color["statusNewColor"] == "#123456"
    assert set(new_color) == {
        "statusHighlight",
        "statusOpacity",
        "statusNew",
        "statusLearning",
        "statusKnown",
        "statusDue",
        "statusNewColor",
        "statusLearningColor",
        "statusKnownColor",
        "statusDueColor",
    }


def test_first_launch_keeps_the_local_word_state_choice_and_highlighting_starts_off(page) -> None:
    script = "return {highlight: $('mangaReaderV2').querySelector('[data-manga-setting=\"statusHighlight\"]').checked, marks: marks().length};"
    local = reader(
        page,
        script,
        prefs={"configured": False},
        before=STUDY,
        localStorage={"pudge.manga.reader.v2": '{"statusHighlight": true, "statusOpacity": 50}'},
    )
    [[snapshot]] = local.calls_of("manga_save_reader_preferences")
    assert local.value["highlight"] is True
    assert (snapshot["statusHighlight"], snapshot["statusOpacity"]) == (True, 50)

    fresh = reader(
        page,
        script,
        regions={0: [column("猫が好き", 0.80, 0.90)]},
        parse={"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]},
        prefs={"configured": False},
        before=STUDY + "window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}};",
    )
    assert fresh.value == {"highlight": False, "marks": 0}
    assert fresh.calls_of("manga_save_reader_preferences")[0][0]["statusHighlight"] is False


def test_ocr_button_is_greyed_out_without_an_ocr_engine_and_while_ocr_runs(page) -> None:
    script = r"""
        const button = document.querySelector('[data-manga-v2-action="ocr-book"]');
        const out = {disabled: button.disabled, aria: button.getAttribute('aria-disabled'), title: button.title,
                     unavailable: button.classList.contains('unavailable'), text: button.textContent};
        await __key('o'); await __tick(10);
        out.toast = takeToast();
        out.labels = {missing: t('manga.ocrBackendIsNotInstalled'), running: t('manga.ocrRunning')};
        return out;
    """
    missing = reader(page, script, before="window.__ocrAvailable = false;").value
    assert (missing["disabled"], missing["aria"], missing["unavailable"]) == (True, "true", True)
    assert missing["title"] == missing["toast"] == missing["labels"]["missing"]
    running = reader(
        page,
        script,
        before="__api.manga_ocr_book_status = async () => ({running: true, state: 'running', total_pages: 4, cached_pages: 1});",
    ).value
    assert (running["disabled"], running["unavailable"], running["title"], running["text"]) == (
        True,
        False,
        "",
        running["labels"]["running"],
    )


def _volume(folder: Path, name: str, *, sidecar_pages: tuple[str, ...] | None = None) -> Path:
    archive = folder / f"{name}.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        for number in (1, 2):
            image = folder / f"{number:02}.png"
            Image.new("RGB", (100, 200), "white").save(image)
            cbz.write(image, arcname=image.name)
    if sidecar_pages is not None:
        pages = [
            {
                "img_path": page,
                "img_width": 100,
                "img_height": 200,
                "blocks": [{"box": [10, 20, 40, 80], "vertical": True, "lines": [f"{name}-{page}"]}],
            }
            for page in sidecar_pages
        ]
        (folder / f"{name}.mokuro").write_text(json.dumps({"pages": pages}), encoding="utf-8")
    return archive


def test_opening_manga_files_imports_mokuro_or_starts_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_web_app import make_api

    api = make_api(tmp_path / "app")
    folder = tmp_path / "picked"
    folder.mkdir()
    _volume(folder, "chosen-sidecar", sidecar_pages=("01.png", "02.png"))
    plain = _volume(folder, "plain")
    nearby = _volume(folder, "nearby", sidecar_pages=("01.png", "02.png"))
    _volume(folder, "partial", sidecar_pages=("01.png",))
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    dialogs: list[dict] = []

    class Window:
        def create_file_dialog(self, *_args, **kwargs):
            dialogs.append(kwargs)
            return [
                str(folder / "chosen-sidecar.mokuro"),
                str(plain),
                str(nearby),
                str(folder / "partial.mokuro"),
                str(folder / "notes.txt"),
            ]

    api.window = Window()
    started: list[int] = []
    monkeypatch.setattr(api, "_manga_ocr_backend_available", lambda **_: True)
    monkeypatch.setattr(api, "start_manga_ocr_book", lambda book_id, *a, **k: started.append(int(book_id)))

    result = api.choose_manga_file()

    assert dialogs[0]["allow_multiple"] is True and "*.cbz;*.zip;*.mokuro" in dialogs[0]["file_types"][0]
    ids = {Path(book["path"]).stem: int(book["id"]) for book in result["books"]}
    assert set(ids) == {"chosen-sidecar", "plain", "nearby", "partial"}
    texts = lambda name: [
        row["text"] for row in api.manga.text_regions(ids[name], 1, cached_only=True)["regions"]
    ]  # noqa: E731
    # A chosen .mokuro opens its volume with that OCR; a sidecar next to a chosen volume is used too.
    assert texts("chosen-sidecar") == ["chosen-sidecar-02.png"] and texts("nearby") == ["nearby-02.png"]
    # Only a volume without Mokuro OCR is sent to the OCR engine.
    assert started == [ids["plain"]]
    # A chosen sidecar must cover the whole volume; an unknown file is refused.
    assert any(error.startswith("partial.cbz:") for error in result["errors"])
    assert texts("partial") == []
    assert any("notes.txt" in error for error in result["errors"])

from __future__ import annotations

import json
import stat
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge import manga_mokuro_session as mokuro_sessions
from pudge.config import CONFIG_WARNINGS, load_config, write_config
from pudge.database import Database
from pudge.manga import MangaService
from tests.i18n_support import en, ru


def _png(path: Path, size=(120, 160)) -> bytes:
    image = Image.new("RGB", size, "white")
    image.save(path, format="PNG")
    return path.read_bytes()


def _book(tmp_path: Path) -> tuple[MangaService, int, Path]:
    db = Database(tmp_path / "pudge.db")
    archive = tmp_path / "book.cbz"
    image_path = tmp_path / "01.png"
    data = _png(image_path)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("01.png", data)
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book = service.import_file(archive)
    return service, int(book["id"]), archive


def _fake_mokuro_python(tmp_path: Path) -> Path:
    path = tmp_path / "fake-mokuro-python"
    log = tmp_path / "fake-mokuro-starts.jsonl"
    path.write_text(
        f"""#!{sys.executable}
import json, os, sys, zipfile
with open({str(log)!r}, 'a') as started:
    started.write(json.dumps({{'argv': sys.argv[1:], 'background': os.environ.get('PUDGE_BACKGROUND_PRIORITY', '')}}) + '\\n')
for line in sys.stdin:
    request = json.loads(line)
    with zipfile.ZipFile(request['archive']) as archive:
        archive.read(request['name'])
    page = {{'img_width': 120, 'img_height': 160,
            'blocks': [{{'box': [10, 20, 80, 120], 'vertical': True, 'font_size': 20, 'lines': ['日本語'],
                        'lines_coords': [[[10, 20], [80, 20], [80, 120], [10, 120]]]}}]}}
    sys.stdout.write(json.dumps({{'id': request['id'], 'page': page}}, ensure_ascii=False) + '\\n')
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_mokuro_backend_reads_pages_without_unpacking_and_publishes(tmp_path: Path) -> None:
    service, book_id, archive = _book(tmp_path)
    fake = _fake_mokuro_python(tmp_path)
    progress: list[tuple[int, int, str]] = []
    try:
        result = service.ocr_book_mokuro(
            book_id,
            mokuro_python=fake,
            progress=lambda done, total, _page, phase: progress.append((done, total, phase)),
        )
    finally:
        mokuro_sessions.close_all()

    assert result["ok"] is True
    assert result["backend"] == "mokuro"
    assert result["complete"] is True
    assert archive.is_file()
    assert not archive.with_suffix("").exists(), "Mokuro must not unpack next to the user's archive"
    assert progress[-1] == (1, 1, "ocr")
    artifact = service.ocr_artifact(book_id)
    assert artifact["pages"][0]["regions"][0]["provenance"]["source"] == "mokuro"
    assert not (tmp_path / "cache" / "manga-ocr" / "mokuro-runs").exists()
    # Mokuro runs as pudge's own long-lived worker script in the Mokuro environment.
    [started] = [
        json.loads(line)
        for line in (tmp_path / "fake-mokuro-starts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    worker = Path(started["argv"][0])
    assert worker.name == "manga_mokuro_worker.py" and worker.is_file() and started["argv"][1:] == ["serve"]
    assert started["background"] == ""


def test_background_library_ocr_gets_its_own_low_priority_mokuro_worker(tmp_path: Path) -> None:
    service, book_id, _archive = _book(tmp_path)
    fake = _fake_mokuro_python(tmp_path)
    try:
        result = service.ocr_book_mokuro(book_id, mokuro_python=fake, background=True)
    finally:
        mokuro_sessions.close_all()
    assert result["complete"] is True
    [started] = [
        json.loads(line)
        for line in (tmp_path / "fake-mokuro-starts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert started["background"] == "1"


def test_mokuro_backend_requires_managed_python(tmp_path: Path) -> None:
    service, book_id, _archive = _book(tmp_path)
    missing = tmp_path / "does-not-exist"
    try:
        service.ocr_book_mokuro(book_id, mokuro_python=missing)
    except RuntimeError as exc:
        assert "Mokuro OCR backend is not installed" in str(exc)
    else:
        raise AssertionError("missing Mokuro backend must fail")


def test_an_old_config_that_chose_the_pudge_engine_runs_mokuro(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(load_config(path), path)
    text = path.read_text(encoding="utf-8")
    assert "manga_ocr_backend" not in text
    # A config saved by an older version that chose Pudge's own engine.
    path.write_text(text.replace("[tools]\n", '[tools]\nmanga_ocr_backend = "pudge"\n', 1), encoding="utf-8")
    assert 'manga_ocr_backend = "pudge"' in path.read_text(encoding="utf-8")
    loaded = load_config(path)
    assert not hasattr(loaded.tools, "manga_ocr_backend")
    assert not any("manga_ocr_backend" in warning for warning in CONFIG_WARNINGS)  # silently
    write_config(loaded, path)
    assert "manga_ocr_backend" not in tomllib.loads(path.read_text(encoding="utf-8"))["tools"]


def test_settings_have_no_manga_ocr_engine_choice(page) -> None:
    result = page.run(
        r"""
        const picker = Boolean($('s_manga_ocr_backend'));
        await __click('#saveSettings'); await __tick(10);
        return {picker, mokuro: Boolean($('s_manga_mokuro_python'))};
        """,
        state={"settings": {"manga_ocr_backend": "pudge"}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value == {"picker": False, "mokuro": True}
    [[values]] = result.calls_of("save_settings")
    assert "manga_ocr_backend" not in values


def test_backend_ignores_an_engine_choice(tmp_path: Path) -> None:
    from tests.test_web_app import make_api

    api = make_api(tmp_path)
    assert "manga_ocr_backend" not in api.get_state()["settings"]
    api.save_settings({"manga_ocr_backend": "pudge"})
    assert "manga_ocr_backend" not in api.get_state()["settings"]
    assert "manga_ocr_backend" not in api.config.config_path.read_text(encoding="utf-8")


def test_volume_ocr_runs_mokuro(tmp_path: Path, monkeypatch) -> None:
    from tests.test_web_app import make_api

    api = make_api(tmp_path / "app")
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("01.png", _png(tmp_path / "01.png"))
    book_id = int(api.manga.import_file(archive)["id"])
    ran: list[dict] = []
    done = {"ok": True, "complete": True, "cached_pages": 1, "total_pages": 1}
    monkeypatch.setattr(api.manga, "ocr_book_mokuro", lambda book, **kwargs: ran.append(kwargs) or done)

    api._run_manga_book_ocr(book_id)

    [kwargs] = ran
    assert kwargs["mokuro_python"] == api._mokuro_ocr_python_path()
    assert kwargs["background"] is False
    assert not hasattr(api.manga, "ocr_book")  # Pudge's own engine is gone


def test_installing_mokuro_creates_its_own_environment_with_the_pinned_version(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge import web_app
    from tests.test_web_app import make_api

    api = make_api(tmp_path)
    commands: list[list[str]] = []

    def run(command, *args, **kwargs):
        commands.append([str(part) for part in command])
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_app.subprocess, "run", run)
    monkeypatch.setattr(web_app, "DEFAULT_LOG_PATH", tmp_path / "logs" / "pudge.log")
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    api._run_mokuro_ocr_install()

    venv_python = str(api.config.paths.cache_dir / "mokuro-ocr" / "venv" / "bin" / "python")
    # The tested version, with all of its dependencies held to the tested set.
    constraints, _tested = api._mokuro_tested_set()
    assert commands[1] == [
        venv_python,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "mokuro==0.2.5",
        "-c",
        str(constraints),
    ]
    assert commands[2][0] == venv_python
    assert api.manga_ocr_status()["state"] == "ready"


def test_install_button_names_mokuro(page) -> None:
    # "pudge": a status an older version could still report while it ran.
    for backend in ("mokuro", "pudge"):
        result = page.run(
            "await PudgeMedia.refreshMangaOcrStatus(); return {button: __text('#installMangaOcr'), label: t('media.installMokuro')};",
            api={
                "manga_ocr_status": {
                    "backend": backend,
                    "state": "not_installed",
                    "running": False,
                    "installed": False,
                }
            },
        )
        assert result.value["button"] == result.value["label"]
    assert en("media.installMokuro") == "Install Mokuro"
    assert ru("media.installMokuro") == "Установить Mokuro"

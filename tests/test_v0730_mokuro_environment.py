"""Mokuro's environment: lookup, configuration, its standalone runner, and the GPL boundary.

The real Mokuro (GPL-3.0) is never imported by pudge; it runs in its own interpreter.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pudge.manga as manga_module
from pudge.config import load_config, write_config
from pudge.manga import MangaService
from source_policy import sources


def _service(tmp_path: Path, configured: str = "") -> MangaService:
    service = MangaService.__new__(MangaService)
    service.cache_dir = tmp_path / "cache"
    service.mokuro_python = configured
    service.work_scheduler = None
    service._ocr_lock = threading.Lock()
    return service


def test_mokuro_environment_lookup_prefers_config_then_managed_venv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_MOKURO_PYTHON", raising=False)
    assert _service(tmp_path)._mokuro_python() is None
    venv_python = tmp_path / "cache" / "mokuro-ocr" / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    assert _service(tmp_path)._mokuro_python() == venv_python
    configured = tmp_path / "other-python"
    configured.write_text("", encoding="utf-8")
    assert _service(tmp_path, str(configured))._mokuro_python() == configured


def test_mokuro_python_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    assert cfg.tools.manga_mokuro_python == ""
    cfg.tools.manga_mokuro_python = "/opt/mokuro/bin/python"
    write_config(cfg, path)
    assert load_config(path).tools.manga_mokuro_python == "/opt/mokuro/bin/python"
    assert (
        tomllib.loads(path.read_text(encoding="utf-8"))["tools"]["manga_mokuro_python"]
        == "/opt/mokuro/bin/python"
    )


def test_mokuro_runner_works_alone_in_an_environment_without_pudge(tmp_path: Path) -> None:
    # Mokuro's own interpreter runs the script; pudge is not installed there. Run it the same
    # way: a copy in an empty folder, no site-packages, no PYTHONPATH.
    runner = tmp_path / "mokuro-env" / "manga_mokuro_worker.py"
    runner.parent.mkdir()
    shutil.copyfile(Path(manga_module.__file__).with_name("manga_mokuro_worker.py"), runner)
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(runner), "serve"],
        input='{"id": 1, "op": "ping"}\n{"id": 2, "op": "translate"}\n',
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert [json.loads(line) for line in done.stdout.splitlines()] == [
        {"id": 1, "ok": True},
        {"id": 2, "error": "unknown op 'translate'"},
    ]
    usage = subprocess.run(
        [sys.executable, "-I", "-S", str(runner)], capture_output=True, text=True, timeout=60, cwd=tmp_path
    )
    assert usage.returncode == 2 and "usage:" in usage.stderr


def test_pudge_never_imports_mokuro_or_its_text_detector() -> None:
    # policy: Mokuro and comic-text-detector are GPL-3.0; pudge may only run them in their own
    # interpreter (manga_mokuro_worker.py). A lazy import inside a function would not show
    # in any run of the code, so the import statements themselves are checked.
    for name, text in sources("pudge/**/*.py").items():
        if name.endswith("manga_mokuro_worker.py"):
            continue
        assert "comic_text_detector" not in text, name
        assert not re.search(r"^\s*(from|import)\s+mokuro\b", text, re.MULTILINE), name

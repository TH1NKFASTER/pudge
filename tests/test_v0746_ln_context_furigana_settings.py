from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def test_context_furigana_settings_default_on_and_persist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    defaults = service.settings_payload()
    assert defaults["furigana_on_hover"] is True
    assert defaults["furigana_on_reading"] is True

    saved = service.save_settings({
        "furigana_on_hover": False,
        "furigana_on_reading": False,
    })
    assert saved["furigana_on_hover"] is False
    assert saved["furigana_on_reading"] is False
    reloaded = LightNovelService(service.config).settings_payload()
    assert reloaded["furigana_on_hover"] is False
    assert reloaded["furigana_on_reading"] is False


def test_reader_gear_contains_both_context_furigana_controls() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert 'id="lnrFuriganaHover"' in source
    assert 'id="lnrFuriganaReading"' in source
    assert 'id="lnrFuriganaReadingRow" class="check" hidden' in source
    assert 'id="lnrFuriganaReadingSpacer" hidden' in source
    assert "settings.lnFuriganaHover" in source
    assert "settings.lnFuriganaReading" in source


def test_reading_setting_is_visible_only_for_paired_light_novels() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "const linked=!!ui.lnBook?.paired_audio" in source
    assert "if(row)row.hidden=!linked" in source
    assert "if(spacer)spacer.hidden=!linked" in source
    assert "linked&&st.furigana_on_reading!==false" in source


def test_context_furigana_is_rendered_and_revealed_by_css() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert 'class="ln-context-furigana"' in source
    assert ".ln-context-furigana rt{visibility:hidden}" in source
    assert ".ln-reader.furigana-hover .ln-word:hover" in source
    assert ".ln-reader.furigana-reading .ln-word.ln-paired-word-current" in source
    assert "furigana_on_hover:c('lnrFuriganaHover')" in source
    assert "furigana_on_reading:c('lnrFuriganaReading')" in source


def test_inline_javascript_parses() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(
            r"<script([^>]*)>(.*?)</script>", source, flags=re.I | re.S
        )
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts
    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-v0746-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)

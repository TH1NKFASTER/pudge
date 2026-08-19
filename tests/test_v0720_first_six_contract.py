from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
MANGA_JS = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
MANGA_PY = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
WEB_APP = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
LOGGING = (ROOT / "pudge/logging_utils.py").read_text(encoding="utf-8")


def test_essential_srs_order_and_label() -> None:
    block = HTML[HTML.index('data-settings-category="essential"><h3>${ui.lang===\'ru\'?\'Jiten / JPDB\''):]
    positions = [block.index(token) for token in ('id="s_ln_jiten_key"','id="s_ln_jpdb_key"','id="s_ln_backend"','id="s_mpv_study_plugin"')]
    assert positions == sorted(positions)
    assert "'settings.lnStudyBackend':'SRS/Vocab source'" in HTML
    assert "'settings.lnStudyBackend':'Источник SRS/словаря'" in HTML


def test_ln_auto_download_is_advanced_and_default_false_contract() -> None:
    pos = HTML.index('id="s_ln_auto_download"')
    advanced = HTML.rfind('data-settings-category="advanced"', 0, pos)
    downloads = HTML.rfind('data-settings-category="downloads"', 0, pos)
    assert advanced > downloads
    light_novels = (ROOT / "pudge/light_novels.py").read_text(encoding="utf-8")
    assert "auto_download_nyaa: bool = False" in light_novels


def test_transport_edge_precedes_slow_verification() -> None:
    toggle = HTML[HTML.index("async function toggleLnPairedPlayback(){"):HTML.index("async function openLightNovel(bookId)")]
    assert "lnPairedTransportEdgePromise" in toggle
    assert "audiobook_set_paused(Number(live.audiobook_id),!wanted)" in toggle
    assert toggle.index("audiobook_set_paused(Number(live.audiobook_id),!wanted)") < toggle.index("light_novel_paired_state")
    assert "if(!wanted)cancelLnPairedInterpolation();" in toggle


def test_debug_exports_are_internal_and_expire_after_two_hours() -> None:
    assert "DEBUG_LOG_RETENTION_SECONDS = 2 * 60 * 60" in LOGGING
    assert "debug_log_dir()" in WEB_APP
    assert "Pudge-patch-logs" not in WEB_APP
    assert ' / "Downloads"' not in WEB_APP[WEB_APP.index("def manga_export_ocr_debug"):WEB_APP.index("def audiobook_state")]


def test_manga_clicks_use_raw_image_geometry_and_2d_vertical_grid() -> None:
    assert "data-raw-x" in MANGA_JS
    assert "function rawRegionRect" in MANGA_JS
    assert "function mangaTokenAtPoint" in MANGA_JS
    assert "const column =" in MANGA_JS and "const row =" in MANGA_JS
    assert '"segments": [' in MANGA_PY
    assert "mangaVerticalTokenAtPoint" not in MANGA_JS


def test_single_page_ocr_button_is_removed() -> None:
    assert 'data-manga-v2-action="ocr-page"' not in MANGA_JS
    assert "type === 'ocr-page'" not in MANGA_JS
    assert 'data-manga-v2-action="ocr-book"' in MANGA_JS

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge/web/index.html"
MANGA = ROOT / "pudge/web/manga_reader_v2.js"
MANGA_CSS = ROOT / "pudge/web/manga_reader_v2.css"
SETTINGS = ROOT / "pudge/web/settings.js"
MEDIA = ROOT / "pudge/web/media.js"
AUDIO = ROOT / "pudge/audiobooks.py"
WEB_APP = ROOT / "pudge/web_app.py"


def test_pause_is_fire_and_forget_and_frontend_hot_path_precedes_state_read() -> None:
    audio = AUDIO.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    method = audio[audio.index("    def set_paused("):audio.index("    def set_speed(")]
    assert "_ipc_commands_no_wait" in method
    assert "_ipc_command(" not in method
    toggle = html[html.index("async function toggleLnPairedPlayback(){"):html.index("async function openLightNovel(bookId){")]
    hot = toggle.index("pywebview.api.audiobook_set_paused")
    first_state = toggle.index("pywebview.api.light_novel_paired_state")
    assert hot < first_state
    assert "appliedDesired" in toggle


def test_reader_controls_keep_their_right_edge_without_audio() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert ".ln-reader-actions{display:flex;align-items:center;gap:6px;flex:0 0 auto;min-width:0;white-space:nowrap;margin-left:auto}" in html


def test_manga_cover_prefers_local_cache_and_uses_real_logo_placeholder() -> None:
    js = MANGA.read_text(encoding="utf-8")
    css = MANGA_CSS.read_text(encoding="utf-8")
    resolve = js[js.index("  async function resolveCover(book)"):js.index("  function progressText(book)")]
    assert resolve.index("const local = localCover(book)") < resolve.index("coverCache[id]")
    assert 'class="ln-card-cover cover-placeholder"' in js
    assert '>漫</div>' not in js
    assert "object-fit:contain" in css


def test_job_center_ui_and_public_bridge_are_removed() -> None:
    html = HTML.read_text(encoding="utf-8")
    web = WEB_APP.read_text(encoding="utf-8")
    for token in ('id="jobs"', 'openJobCenter', 'jobCenterContent', 'loadJobCenter()', 'job_center_cancel', 'job_center_retry'):
        assert token not in html
    assert "def job_center_state(" not in web
    assert "def job_center_cancel(" not in web
    assert "def job_center_retry(" not in web
    assert "self.job_center" in web  # internal job tracking remains


def test_ln_percentage_chip_has_character_tooltip() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert 'value===`${percent}%`?`<span data-tooltip="${escapeHtml(progressTitle)}"' in html
    assert 'class="ln-card-progress" data-tooltip="${escapeHtml(progressTitle)}"' in html


def test_manga_vertical_low_confidence_and_selection_guard() -> None:
    js = MANGA.read_text(encoding="utf-8")
    assert "lowConfidenceVertical" in js
    assert "mangaTokenAtPoint" in js
    assert "rawRegionRect" in js
    assert "text.length > 48" in js
    assert "snap.width > imageRect.width * .75" in js
    assert "currentJapaneseSelection(image)" in js


def test_settings_categories_match_new_contract() -> None:
    html = HTML.read_text(encoding="utf-8")
    settings = SETTINGS.read_text(encoding="utf-8")
    media = MEDIA.read_text(encoding="utf-8")
    assert "reading: 'Reading'" not in settings
    assert "reading: 'Чтение'" not in settings
    assert 'data-settings-category="essential"><h3>${ui.lang===\'ru\'?\'Jiten / JPDB\'' in html
    parse_pos = html.index('id="s_ln_parse_ahead"')
    advanced_pos = html.rfind('data-settings-category="advanced"', 0, parse_pos)
    assert advanced_pos >= 0
    manga_pos = html.index('id="mangaOcrStatus"')
    essential_pos = html.rfind('data-settings-category="essential"', 0, manga_pos)
    assert essential_pos >= 0
    assert "status.state === 'ready' ? 'advanced' : 'essential'" in media
    assert "block.dataset.settingsCategory = block.dataset.settingsCategory || categoryFor(block)" in settings
    assert "PudgeSettings = {enhance, focusAction, refresh}" in settings

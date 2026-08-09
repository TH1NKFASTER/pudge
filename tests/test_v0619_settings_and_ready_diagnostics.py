from pathlib import Path


def web_html() -> str:
    return (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")


def test_settings_render_before_experimental_pages_and_have_error_boundary() -> None:
    html = web_html()
    assert "function renderSettingsSafely()" in html
    assert "function renderSafely(name,renderer)" in html
    assert "function renderAll(){applyStaticLanguage();renderSettingsSafely();applyStaticLanguage();renderDataPages();" in html
    assert "EVENT ui.render.fail page=settings" in html
    assert "EVENT ui.render.fail page=${name}" in html


def test_settings_nav_can_recover_missing_content() -> None:
    html = web_html()
    assert "b.dataset.page==='settings'&&!$('settingsContent')?.children.length" in html
    assert "renderSettingsSafely();const changed=setPage" in html


def test_why_not_ready_is_hidden_and_guarded_for_ready_anime() -> None:
    html = web_html()
    assert "function needsReadinessDiagnosis(anime)" in html
    assert "if(state==='ready'||state==='watched')return false" in html
    assert "const diagnose=!planned&&!suppressDiagnosis&&needsReadinessDiagnosis(a)?" in html
    assert "if(action==='diagnose'){if(!a.readinessDiagnosisSuppressed&&needsReadinessDiagnosis(a))" in html

from pathlib import Path


def test_refresh_button_uses_disabled_spinner_and_status_tooltip():
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")

    assert '<button id="refreshAll" data-i18n="action.refresh">Refresh</button>' in html
    assert 'button.disabled=active' in html
    assert 'button.innerHTML=`<span class="spinner" aria-hidden="true"></span><span>${label}</span>`' in html
    assert 'button.title=t(statusKey)' in html
    assert "button.textContent=t('action.refresh')" in html
    assert "button.removeAttribute('title')" in html
    assert 'class="refresh-glyph"' not in html

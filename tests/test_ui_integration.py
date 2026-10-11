from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
WEB = ROOT / "pudge" / "web"


def test_web_pages_and_external_modules_are_wired(page) -> None:
    # The page harness fails on a missing or broken <script src>; stylesheets it skips
    # silently, so their files are checked here.
    result = page.run(
        """
        return {
          pages: ['current', 'lightnovels', 'manga', 'audiobooks', 'planned', 'settings'].filter(id => document.getElementById(id)?.classList.contains('page')),
          modules: {settings: typeof window.PudgeSettings?.enhance, media: typeof window.PudgeMedia?.loadManga, debug: typeof window.PudgeDebug?.open},
          settingsTabs: document.querySelectorAll('#settingsContent .settings-categories [data-settings-category]').length,
          stylesheets: [...document.querySelectorAll('link[rel="stylesheet"]')].map(link => link.getAttribute('href')),
        };
        """
    )
    out = result.value
    assert out["pages"] == ["current", "lightnovels", "manga", "audiobooks", "planned", "settings"]
    assert out["modules"] == {"settings": "function", "media": "function", "debug": "function"}
    assert out["settingsTabs"] == 5
    assert {"settings.css", "media.css", "debug.css"} <= set(out["stylesheets"])
    assert [href for href in out["stylesheets"] if not (WEB / href).is_file()] == []


def test_external_javascript_has_real_syntax_check() -> None:
    node = shutil.which("node")
    if node is None:
        return
    for script in (WEB / "settings.js", WEB / "media.js", WEB / "debug.js"):
        subprocess.run([node, "--check", str(script)], check=True, capture_output=True, text=True)


def test_activity_pages_stay_hidden_and_problems_surface_on_home(page) -> None:
    # No Downloads / Diagnostics pages in the sidebar; a title that needs the user
    # shows up under "Action required" on Home and its button opens the setting.
    item = {"media_id": 5, "title": "Needs Jimaku", "action_code": "configure_jimaku"}
    for lang in ("en", "ru"):
        result = page.run(
            """
            const nav = [...document.querySelectorAll('.nav [data-page]')].map(button => button.dataset.page);
            const section = document.querySelector('#current [data-home-section="section.needsAction"]');
            const out = {nav, title: section?.querySelector('.section-title')?.innerText, expected: t('section.needsAction'),
                         button: section?.querySelector('[data-action="open-needs-action"]')?.innerText, configure: PudgeI18n.t('ui.configureJimaku')};
            await __click('#current [data-action="open-needs-action"]');
            await __tick(50);
            return {...out, page: ui.page, focused: document.activeElement?.id};
            """,
            lang=lang,
            state={"home": {"needs_action": [item]}},
        )
        out = result.value
        assert "downloads" not in out["nav"] and "diagnostics" not in out["nav"]
        assert out["title"] == out["expected"]
        assert out["button"] == out["configure"]
        assert out["page"] == "settings" and out["focused"] == "s_jimaku"

"""Settings survive failing page renderers; "Why not ready?" only where it makes sense
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

WAITING = {
    "media_id": 1,
    "title": "Waiting Show",
    "episodes": 12,
    "next_episode": 3,
    "local": {"state": "waiting_subtitles", "episode": 3, "video_path": "/v/1.mkv"},
}
READY = {
    "media_id": 2,
    "title": "Ready Show",
    "episodes": 12,
    "next_episode": 3,
    "local": {
        "state": "ready",
        "subtitle_origin": "jimaku",
        "subtitle_source": "external",
        "episode": 3,
        "video_path": "/v/2.mkv",
    },
}
OCR_READY = {
    "media_id": 3,
    "title": "Ocr Show",
    "episodes": 12,
    "next_episode": 3,
    "local": {"state": "ready", "subtitle_origin": "ocr", "episode": 3, "video_path": "/v/3.mkv"},
}
WATCHED = {
    "media_id": 4,
    "title": "Watched Show",
    "episodes": 12,
    "next_episode": 3,
    "local": {"state": "watched", "episode": 2, "video_path": "/v/4.mkv"},
}
CAUGHT_UP = {"media_id": 5, "title": "Caught Up Show", "episodes": 12, "next_episode": 4, "local": None}
COMPLETED = {
    "media_id": 6,
    "title": "Completed Show",
    "episodes": 12,
    "next_episode": 12,
    "local": {"state": "waiting_subtitles", "episode": 12, "video_path": "/v/6.mkv"},
}
PLANNED = {
    "media_id": 7,
    "title": "Planned Show",
    "media_kind": "anime",
    "episodes": 12,
    "media_status": "FINISHED",
    "local": None,
}
HOME = {
    "current": [WAITING, READY, OCR_READY, WATCHED, CAUGHT_UP, COMPLETED],
    "planned": [PLANNED],
    "home": {
        "waiting": [WAITING],
        "new_ready": [READY, OCR_READY],
        "recently_watched": [WATCHED],
        "caught_up": [CAUGHT_UP],
        "completed_ready": [COMPLETED],
    },
}

# Opens the card menu (⋯) of an anime and lists its actions.
MENU = """
const menuFor = async (id, page = 'current') => {
  const button = document.querySelector(`#${page} [data-media-id="${id}"] [data-action="card-menu"]`);
  button.click();
  await __flush();
  return [...document.querySelectorAll('#contextMenu [data-context-action]')].map(item => item.dataset.contextAction);
};
const why = () => document.querySelector('#contextMenu [data-context-action="diagnose"]');
"""


def test_failing_page_renderer_does_not_take_settings_down(page) -> None:
    result = page.run(
        """
        const out = {};
        const realCurrent = renderCurrent;
        renderCurrent = () => { throw new Error('current exploded'); };
        $('settingsContent').innerHTML = ''; ui.currentRenderSignature = null;
        renderAll();
        out.afterPageFailure = $('settingsContent').querySelectorAll('.setting-block').length;
        renderCurrent = realCurrent;
        // Settings are drawn before the data pages: a failure there leaves them intact.
        const realData = renderDataPages;
        renderDataPages = () => { throw new Error('data pages exploded'); };
        $('settingsContent').innerHTML = '';
        try { renderAll(); } catch (error) { out.thrown = error.message; }
        out.afterDataFailure = $('settingsContent').querySelectorAll('.setting-block').length;
        renderDataPages = realData;
        return out;
        """,
    )
    assert result.value["afterPageFailure"] > 10
    assert result.value["thrown"] == "data pages exploded"
    assert result.value["afterDataFailure"] > 10
    assert any(
        line.startswith("error: EVENT ui.render.fail page=current") and "current exploded" in line
        for line in result.console
    )


def test_failing_settings_renderer_is_logged_and_the_sidebar_recovers_it(page) -> None:
    result = page.run(
        """
        const out = {};
        const realSettings = renderSettings;
        renderSettings = () => { throw new Error('settings exploded'); };
        ui.state.home = {...ui.state.home, waiting: [{media_id: 9, title: 'Fresh Show', episodes: 12, next_episode: 1}]};
        renderAll();
        out.settingsBlocks = $('settingsContent').children.length;
        out.home = __text('#current');
        renderSettings = realSettings;
        await __click('.nav [data-page="settings"]');
        await __tick(20);
        out.page = ui.page;
        out.recovered = $('settingsContent').querySelectorAll('.setting-block').length;
        return out;
        """,
    )
    out = result.value
    assert out["settingsBlocks"] == 0  # a half-drawn page is cleared
    assert "Fresh Show" in out["home"]  # other pages still render
    assert out["page"] == "settings" and out["recovered"] > 10
    assert any(
        line.startswith("error: EVENT ui.render.fail page=settings") and "settings exploded" in line
        for line in result.console
    )


def test_why_not_ready_is_offered_only_for_anime_that_are_not_ready(page) -> None:
    result = page.run(
        MENU
        + """
        const out = {};
        for (const id of [1, 2, 3, 4, 5, 6]) out[id] = (await menuFor(id)).includes('diagnose');
        setPage('planned'); await __tick(50);
        out.planned = (await menuFor(7, 'planned')).includes('diagnose');
        out.label = why() ? why().innerText : t('action.whyNotReady');
        ui.state.settings.ocr_counts_as_ready = true;
        setPage('current'); await __tick(50);
        out.ocrCounted = (await menuFor(3)).includes('diagnose');
        return out;
        """,
        state=HOME,
    )
    out = result.value
    assert out["1"] is True  # waiting for subtitles
    assert out["2"] is False  # ready
    assert out["3"] is True  # ready with OCR subtitles only, while OCR does not count as ready
    assert out["ocrCounted"] is False  # ... and once it does
    assert out["4"] is False  # watched
    assert out["5"] is False  # caught up
    assert out["6"] is False  # Completed and ready section
    assert out["planned"] is False


def test_why_not_ready_rechecks_before_diagnosing(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            MENU
            + """
            await menuFor(1);
            const label = why().innerText;
            await __click('#contextMenu [data-context-action="diagnose"]');
            await __tick(20);
            const title = __text('#modalTitle');
            // A menu opened before the anime moved to Caught up does not diagnose it.
            await __click('#modalClose');
            await menuFor(1);
            ui.state.home = {...ui.state.home, waiting: [], caught_up: [ui.state.home.waiting[0]]};
            await __click('#contextMenu [data-context-action="diagnose"]');
            await __tick(20);
            return {label, title, expected: t('action.whyNotReady')};
            """,
            lang=lang,
            state=HOME,
            api={"diagnose_episode": {"ready": False, "title": "Waiting Show", "checks": []}},
        )
        out = result.value
        assert out["label"] == out["expected"]
        assert out["title"].startswith(out["expected"])
        assert result.calls_of("diagnose_episode") == [[1, 3]]

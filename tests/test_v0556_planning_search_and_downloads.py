from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def _planning_state(tmp_path: Path) -> tuple[WebAppApi, dict]:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=151807,
            title="Mushoku Tensei III",
            titles=["Mushoku Tensei: Jobless Reincarnation Season 3"],
            synonyms=["無職転生 III"],
            status="PLANNING",
            media_status="NOT_YET_RELEASED",
        )
    )
    # Its AniList ID is a prefix of the first one's.
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=15180, title="Other show", status="PLANNING", media_status="FINISHED")
    )
    return api, json.loads(json.dumps(api.get_state(), default=str))


SHOWN = "const shown = () => [...document.querySelectorAll('#plannedContent > [data-media-id]:not([hidden])')].map(el => Number(el.dataset.mediaId));"


def test_planning_search_finds_titles_aliases_and_anilist_ids(tmp_path: Path, page) -> None:
    api, state = _planning_state(tmp_path)
    planned = state["planned"][0]
    assert planned["titles"] == ["Mushoku Tensei: Jobless Reincarnation Season 3"]
    assert planned["synonyms"] == ["無職転生 III"]
    for lang in ("en", "ru"):
        result = page.run(
            SHOWN
            + """
            setPage('planned');
            await __tick(20);
            const out = {type: $('plannedSearch').type, all: shown()};
            for (const [name, query] of [['id', '151807'], ['shorterId', '15180'], ['partialId', '1518'], ['alias', 'jobless reincarnation'],
                                         ['synonym', '無職転生'], ['none', 'zzz']]) {
              await __input('#plannedSearch', query);
              out[name] = shown();
            }
            out.empty = __text('#plannedContent .planned-empty');
            out.expected = PudgeI18n.t('ui.nothingMatchesTheseFilters');
            return out;
            """,
            lang=lang,
            state=state,
            api={"planning_search_anilist": []},
        )
        out = result.value
        assert out["type"] == "search" and sorted(out["all"]) == sorted(out["partialId"]) == [15180, 151807]
        assert out["id"] == out["alias"] == out["synonym"] == [151807]
        assert out["shorterId"] == [15180]  # an exact ID wins over IDs containing it
        assert out["none"] == [] and out["empty"] == out["expected"]


def test_cmd_f_opens_global_search_on_any_page(tmp_path: Path, page) -> None:
    # Cmd+F used to jump to Planning (and had its own shortcut setting); it now opens
    # the global search over the current page. Results open where the title lives.
    api, state = _planning_state(tmp_path)
    rows = json.loads(json.dumps(api.global_media_search("無職転生", 40), default=str))
    result = page.run(
        SHOWN
        + """
        await __key('f', {metaKey: true});
        await __tick(20);
        const out = {page: ui.page, open: __visible('#globalSearchOverlay'), focused: document.activeElement?.id,
                     placeholder: $('globalSearchInput').placeholder, expected: t('ui.searchPudge')};
        await __input('#globalSearchInput', ' 無職転生 ');
        await __tick(400);
        out.results = __text('#globalSearchResults');
        await __key('Enter', {}, '#globalSearchInput');
        await __tick(20);
        out.opened = {page: ui.page, search: $('plannedSearch').value, shown: shown(), open: __visible('#globalSearchOverlay')};
        out.ownShortcut = 'shortcut_app_planning_search' in collectSettings()
          || [...document.querySelectorAll('#settingsContent [id]')].some(el => el.id.includes('planning_search'));
        return out;
        """,
        state=state,
        api={"global_media_search": rows, "planning_search_anilist": []},
    )
    out = result.value
    assert out["page"] == "current" and out["open"] is True and out["focused"] == "globalSearchInput"
    assert out["placeholder"] == out["expected"]
    assert result.calls_of("global_media_search") == [["無職転生", 40]]
    assert "Mushoku Tensei III" in out["results"]
    assert out["opened"] == {
        "page": "planned",
        "search": "Mushoku Tensei III",
        "shown": [151807],
        "open": False,
    }
    assert out["ownShortcut"] is False  # the old "Planning search" shortcut setting is gone


def test_finished_watching_title_without_ready_files_is_available_to_download(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=42,
            title="Finished backlog anime",
            status="CURRENT",
            progress=2,
            episodes=12,
            media_status="FINISHED",
        )
    )

    home = api.get_state()["home"]

    assert [item["media_id"] for item in home["download_available"]] == [42]
    assert home["caught_up"] == []
    assert home["completed_ready"] == []


def test_unreleased_watching_title_is_not_offered_for_download(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=43,
            title="Future anime",
            status="CURRENT",
            progress=0,
            episodes=12,
            media_status="NOT_YET_RELEASED",
        )
    )

    assert api.get_state()["home"]["download_available"] == []


def test_download_group_and_planning_download_only_action_use_batch_search(tmp_path: Path, page) -> None:
    assert en("section.availableDownload") == "Available to download"
    assert ru("section.availableDownload") == "Можно скачать"
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=42,
            title="Finished backlog anime",
            status="CURRENT",
            progress=2,
            episodes=12,
            media_status="FINISHED",
        )
    )
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=7, title="Planned show", status="PLANNING", media_status="FINISHED", episodes=12
        )
    )
    state = json.loads(json.dumps(api.get_state(), default=str))
    for lang in ("en", "ru"):
        result = page.run(
            """
            const section = document.querySelector('[data-home-section="section.availableDownload"]');
            const out = {heading: section?.querySelector('h2').innerText, expected: t('section.availableDownload'),
                         titles: [...section.querySelectorAll('article')].map(el => el.dataset.title),
                         button: __text('[data-home-section="section.availableDownload"] button.download-card-button'),
                         downloadAnime: t('action.downloadAnime')};
            await __click('[data-home-section="section.availableDownload"] button.download-card-button');
            await __tick(10);
            out.modal = __text('#modalTitle');
            out.releases = t('modal.releases');
            closeModal();
            // Planning: "Download without watching" in the card menu searches whole-series releases too.
            setPage('planned');
            await __tick(20);
            await __fire('#plannedContent > [data-media-id="7"] .anime-title', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
            out.menu = [...document.querySelectorAll('#contextMenu [data-context-action]')].map(b => b.dataset.contextAction);
            out.withoutWatching = __text('#contextMenu [data-context-action="download-only"]');
            out.withoutWatchingLabel = t('action.downloadWithoutWatching');
            await __click('#contextMenu [data-context-action="download-only"]');
            await __tick(10);
            return out;
            """,
            lang=lang,
            state=state,
            api={"search_releases": []},
        )
        out = result.value
        assert out["heading"] == out["expected"] and out["titles"] == ["Finished backlog anime"]
        assert out["button"] == out["downloadAnime"] and out["modal"] == out["releases"]
        assert "download-only" in out["menu"] and out["withoutWatching"] == out["withoutWatchingLabel"]
        assert result.calls_of("search_releases") == [
            [42, None, True],
            [7, None, True],
        ]  # (media, episode, batch)

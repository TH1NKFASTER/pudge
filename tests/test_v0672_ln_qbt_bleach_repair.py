import tomllib
from pathlib import Path

import httpx

from pudge import __version__
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryEpisode
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.subtitle_formats import clean_srt_for_playback
from css_support import css_rules
from source_policy import source
from tests.i18n_support import en, has_text, ru


def _srt(text: str = "日本語") -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_v0672_version() -> None:
    # policy: the release version is declared in pyproject.toml and announced in the README.
    expected = tomllib.loads(source("pyproject.toml"))["project"]["version"]
    assert __version__ == expected
    assert f"Current version: **{expected}**." in source("README.md")


def test_alignment_generation_uses_history_when_old_sync_source_was_pruned(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)

    raw = cfg.paths.cache_dir / "jimaku" / "bleach" / "candidate.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_srt("raw"), encoding="utf-8")

    aligned = cfg.paths.cache_dir / "synced" / "legacy-ffsubsync.srt"
    aligned.parent.mkdir(parents=True, exist_ok=True)
    aligned.write_text(_srt("aligned"), encoding="utf-8")

    stale_playback, _ = clean_srt_for_playback(aligned, cfg.paths.cache_dir)
    direct_playback, _ = clean_srt_for_playback(raw, cfg.paths.cache_dir)
    assert stale_playback.name != direct_playback.name

    video = cfg.library.root_dir / "Bleach.2004.S17E43.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=43,
            video_path=video,
            subtitle_path=stale_playback,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=43,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        score=98.36,
        status="selected",
        reason="Preparation completed",
        details={"candidate_path": str(raw), "final_path": str(stale_playback)},
    )

    clean_video = cfg.library.root_dir / "Direct - 01.mkv"
    clean_video.write_bytes(b"video")
    clean_raw = cfg.paths.cache_dir / "jimaku" / "direct" / "candidate.srt"
    clean_raw.parent.mkdir(parents=True, exist_ok=True)
    clean_raw.write_text(_srt("direct"), encoding="utf-8")
    clean_playback, _ = clean_srt_for_playback(clean_raw, cfg.paths.cache_dir)
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=2,
            title="Direct",
            episode=1,
            video_path=clean_video,
            subtitle_path=clean_playback,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=clean_video,
        media_id=2,
        episode=1,
        source="jimaku",
        candidate_name=clean_raw.name,
        candidate_path=clean_raw,
        status="selected",
        reason="Preparation completed",
        details={"candidate_path": str(clean_raw), "final_path": str(clean_playback)},
    )

    manager.db.set_state("subtitle_validation_generation", "15")
    assert manager._requeue_legacy_generated_subtitles() == 1

    bleach = manager.db.episode_by_path(video)
    direct = manager.db.episode_by_path(clean_video)
    assert bleach is not None and bleach.subtitle_path is None
    assert bleach.state == "waiting_subtitles"
    assert direct is not None and direct.subtitle_path == clean_playback.resolve()
    assert direct.state == "ready"
    assert manager.db.get_state("subtitle_validation_generation", "") == "17"


def test_qbittorrent_can_set_location_and_recheck() -> None:
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.url.path, body))
        return httpx.Response(200, text="Ok.")

    client = QBittorrentClient("http://qbt.local", api_key="key")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    client.set_location("abc", Path("/tmp/pudge/Bleach"))
    client.recheck("abc")
    client.close()

    assert [path for path, _body in seen] == [
        "/api/v2/torrents/setLocation",
        "/api/v2/torrents/recheck",
    ]
    assert b"hashes=abc" in seen[0][1]
    assert b"location=" in seen[0][1]
    assert b"hashes=abc" in seen[1][1]


def test_manager_repairs_missingfiles_after_brand_rename(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    target = cfg.library.root_dir / "BLEACH_ Sennen Kessen-hen - Kashin-tan"
    target.mkdir(parents=True)
    old = tmp_path / "Movies" / "Anime MPV" / target.name

    item = DownloadItem(
        torrent_hash="bleach",
        name="Bleach E43",
        state="missingFiles",
        progress=0.0,
        save_path=str(old),
        content_path=str(old / "Bleach.2004.S17E43.mkv"),
        raw={"category": "anime-mpv", "_tag_set": ["anime-mpv"]},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.locations: list[tuple[str, Path]] = []
            self.rechecks: list[str] = []

        def set_location(self, torrent_hash: str, location: Path) -> None:
            self.locations.append((torrent_hash, location))

        def recheck(self, torrent_hash: str) -> None:
            self.rechecks.append(torrent_hash)

    client = FakeClient()
    assert manager._repair_legacy_qbittorrent_paths(client, [item]) == 1
    assert client.locations == [("bleach", target.resolve())]
    assert client.rechecks == ["bleach"]


def test_ln_card_body_is_a_column_with_actions_at_the_bottom() -> None:
    # Cards of different heights keep their buttons on one line at the bottom.
    rules = css_rules("index.html")
    body = rules.declarations(".ln-card-body")
    assert (body["display"], body["flex-direction"], body["min-width"]) == ("flex", "column", "0")
    actions = rules.declarations(".ln-card-actions")
    assert (actions["display"], actions["flex-wrap"], actions["margin-top"], actions["gap"]) == (
        "flex",
        "wrap",
        "auto",
        "6px",
    )
    # The linked cover opening AniList: tests/test_v0700_reading_library_polish_local.py.


def test_light_novels_have_no_nyaa_volume_search_and_settings_keep_auto_download(page) -> None:
    assert not has_text("Run auto Nyaa") and not has_text("Run auto Nyaa", "ru")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels', true); await __tick(100);
            await __fire('#lightNovelsContent [data-ln-book="6"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
            const out = {actions: [...document.querySelectorAll('[data-ln-action], [data-ln-context-action]')].map(n => n.dataset.lnAction || n.dataset.lnContextAction),
                         autoNyaa: Boolean($('lnAutoNyaa')), text: __text('#lightnovels')};
            setPage('settings', true); await __tick(50);
            const auto = $('s_ln_auto_download');
            out.settings = {auto: auto?.type, autoLabel: auto?.closest('label').innerText, expected: t('settings.lnAutoNyaa'),
                            category: auto?.closest('[data-settings-category]').dataset.settingsCategory, nyaaCategory: Boolean($('s_ln_nyaa_category'))};
            // AniList: credentials only, no separate link to AniList's authentication guide.
            const anilist = $('s_anilist_enabled').closest('.setting-block');
            out.anilist = {guideLinks: [...anilist.querySelectorAll('a, button, [data-url]')].filter(n =>
                              /docs\.anilist\.co/.test(n.getAttribute('href') || n.dataset.url || '') || n.innerText === PudgeI18n.t('action.anilistDocs')).length};
            const semantic = $('s_subtitle_semantic_checks');
            out.semantic = {type: semantic.type, label: semantic.closest('label').innerText, expected: PudgeI18n.t('ui.allowLlmSubtitleVerificationExpensive')};
            await __input('#s_ln_auto_download', true);
            await __click('#saveSettings'); await __tick(10);
            return out;
            """,
            lang=lang,
            api={
                "light_novel_state": {
                    "version": 1,
                    "settings": {},
                    "books": [{"id": 6, "title": "Novel", "volume": 3}],
                }
            },
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert "nyaa" not in out["actions"] and out["autoNyaa"] is False
        assert "Run auto Nyaa" not in out["text"]
        assert out["settings"] == {
            "auto": "checkbox",
            "autoLabel": out["settings"]["expected"],
            "expected": out["settings"]["expected"],
            "category": "advanced",
            "nyaaCategory": False,
        }
        assert out["anilist"] == {"guideLinks": 0}
        assert (
            out["semantic"]["type"] == "checkbox" and out["semantic"]["label"] == out["semantic"]["expected"]
        )
        [[values]] = result.calls_of("save_settings")
        assert values["ln_auto_download_nyaa"] is True
    assert en("ui.allowLlmSubtitleVerificationExpensive") == "Allow LLM subtitle verification (expensive)"
    assert ru("ui.allowLlmSubtitleVerificationExpensive") == "Разрешить LLM-проверку субтитров (дорого)"
    assert "Light Novel translation" in en("settings.llmOptional")
    assert "перевода в читалке ранобэ" in ru("settings.llmOptional")

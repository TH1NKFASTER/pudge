"""v0.7.2 follow-ups: Planning suggestions below the list, the light-novel listen tray and
word card, credential guides, audiobook analysis progress and its STT worker
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.subtitles import stt_worker
from pudge.web_app import WebAppApi
from source_policy import source
from t71g19_support import LN_PAIRED, OPEN
from tests.i18n_support import en, has_text, ru


def test_planning_anilist_suggestions_come_after_the_planning_list(page) -> None:
    result = page.run(
        r"""
        setPage('planned', true); await __tick(20);
        await __input('#plannedSearch', 'Suggested'); await __tick(400);
        const content = $('plannedContent'), suggestions = $('plannedSuggestions');
        return {after: content.nextElementSibling === suggestions, text: __text('#plannedSuggestions'), header: PudgeI18n.t('ui.anilistSuggestions'),
                listed: __text('#plannedContent')};
        """,
        state={
            "planned": [
                {"media_id": 1, "media_kind": "anime", "title": "Suggested Already Planned", "episodes": 12}
            ]
        },
        api={
            "planning_search_anilist": [
                {
                    "media_id": 9,
                    "media_kind": "anime",
                    "title": "Suggested Show",
                    "site_url": "https://anilist.co/anime/9",
                }
            ]
        },
    )
    out = result.value
    assert result.calls_of("planning_search_anilist") == [["Suggested"]]
    assert "Suggested Already Planned" in out["listed"]
    assert out["after"] is True and out["text"].startswith(out["header"]) and "Suggested Show" in out["text"]


def test_dialogs_open_above_the_light_novel_reader() -> None:
    # A dialog opened from the reader (link an audiobook, Names) used to open behind it.
    rules = css_rules("index.html")
    assert int(rules.get(".modal-backdrop", "z-index")) > int(rules.get(".ln-reader-shell", "z-index"))


# Book 9 linked to audiobook 31; the player is not running. window.PAIRED is what the
# backend reports; light_novel_play_paired starts playback.
TRAY = (
    LN_PAIRED
    + r"""
window.PAIRED = {linked: true, audiobook_id: 31, alignment_mode: 'stt', playing: false, paused: false, player_running: false,
  position: 10, speed: 1, duration: 600, ln_chapter_index: 0, chapter_char_offset_exact: 0,
  alignment: {ready: false, status: 'transcribing', progress_percent: 42}};
__api.light_novel_paired_state = async () => window.PAIRED;
const open9 = __api.light_novel_open;
__api.light_novel_open = async id => {
  const book = await open9(id);
  if (id === 8) return {...book, paired_audio: null};
  return {...book, paired_audio: {...book.paired_audio, alignment: window.PAIRED.alignment,
    book: {...book.paired_audio.book, playing: false, paused: false, player_running: false}}};
};
__api.light_novel_play_paired = async () => ({...window.PAIRED, playing: true, player_running: true, anchor_window: {path: [{time: 10, offset: 0}, {time: 28, offset: 18}]}});
"""
)
TRAY_VIEW = r"""
const view = () => ({tray: __visible('#lnPairedTray'), button: __text('#lnPairedAudio'), disabled: $('lnPairedAudio')?.disabled,
  controls: ['[data-ln-audio-seek="-15"]', '#lnPairedScrubber', '#lnPairedTime', '#lnPairedSpeed', '#lnPairedAutoScrollLabel'].map(s => __visible('#lnPairedTray ' + s))});
"""


def test_listen_tray_shows_for_linked_books_and_opens_its_controls_on_listen(page) -> None:
    assert en("ui.audioAnalysis") == "Audio analysis" and ru("ui.audioAnalysis") == "Разбор аудио"
    for lang in ("en", "ru"):
        result = page.run(
            TRAY_VIEW
            + r"""
            await __settle(openLightNovel(8)); await __tick(300);
            const out = {plain: view()};
            await __settle(openLightNovel(9)); await __tick(300);
            out.analysing = view();
            out.analysis = `${PudgeI18n.t('ui.audioAnalysis')} · 42%`;
            PAIRED.alignment = {ready: true, status: 'ready'};
            await __tick(800);
            out.ready = view(); out.listen = PudgeI18n.t('ui.listen');
            await __click('#lnPairedAudio'); await __tick(300);
            out.listening = view(); out.pause = PudgeI18n.t('ui.pause');
            return out;
            """,
            lang=lang,
            before=TRAY,
        )
        out = result.value
        assert out["plain"]["tray"] is False  # no audiobook, no tray
        assert out["analysing"]["tray"] is True and out["analysing"]["button"] == out["analysis"]
        assert out["analysing"]["disabled"] is True and not any(out["analysing"]["controls"])
        assert out["ready"]["button"] == out["listen"] and out["ready"]["disabled"] is False
        assert not any(out["ready"]["controls"])  # collapsed until listening starts
        assert out["listening"]["button"] == out["pause"] and all(out["listening"]["controls"])
        assert len(result.calls_of("light_novel_play_paired")) == 1


def test_word_card_offers_play_from_here_once_the_audio_is_aligned(page) -> None:
    assert en("ui.playFromHere") == "Play from here"
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + r"""
            AUDIO.playing = false; await __tick(300);
            await __click('#lnReader [data-ln-token="0:1"]'); await __tick(300);             // 言い方, audio offset 2
            const button = document.querySelector('[data-pudge-study-extra-action="paired-audio-here"]');
            const out = {label: button && button.textContent, expected: PudgeI18n.t('ui.playFromHere'), visible: Boolean(button && button.checkVisibility())};
            await __click('[data-pudge-study-extra-action="paired-audio-here"]'); await __tick(100);
            out.lit = words().filter(word => word.current).map(word => word.text);
            return out;
            """,
            lang=lang,
            before=LN_PAIRED
            + "__api.light_novel_play_paired_at_offset = async () => ({...window.pairedState(), position: 12, playing: true});",
        )
        out = result.value
        assert out["visible"] is True and out["label"] == out["expected"]
        assert result.calls_of("light_novel_play_paired_at_offset") == [[9, 0, 2, 1]]
        assert out["lit"] == ["言い方"]


def test_light_novel_menu_and_reader_have_no_names_editor(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels', true); await __tick(100);
        await __fire('#lightNovelsContent [data-ln-book="9"]', 'contextmenu', {button: 2, clientX: 10, clientY: 10}, 'MouseEvent');
        const actions = [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(b => b.dataset.lnContextAction);
        await __key('Escape');
        await __settle(openLightNovel(9)); await __tick(100);
        return {actions, names: Boolean($('lnCharacterNames')),
                toolbar: [...document.querySelectorAll('#lnReaderShell .ln-reader-toolbar button')].filter(b => b.checkVisibility()).map(b => b.id)};
        """,
        before=LN_PAIRED,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [{"id": 9, "title": "Novel", "volume": 1, "character_count": 100}],
            }
        },
    )
    out = result.value
    assert "read" in out["actions"] and "name-cues" not in out["actions"]
    assert out["names"] is False and "lnCharacterNames" not in out["toolbar"]


def test_credential_guides_link_to_jimaku_account_and_anilist_oauth(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const links = [...document.querySelectorAll('#settingsContent [data-action="url"]')].map(b => b.dataset.url).filter(url => url.includes('jimaku.cc'));
            const guide = document.createElement('div'); guide.innerHTML = PudgeI18n.t('ui.openJimakuAndChooseRegister');
            const shown = __text('#settingsContent').includes(guide.querySelector('li').innerText);
            await __click('#settingsContent [data-action="url"][data-url="https://jimaku.cc/account"]'); await __tick(10);
            return {links, shown};
            """,
            lang=lang,
        )
        assert result.value["shown"] is True
        assert "https://jimaku.cc/account" in result.value["links"]
        assert not [url for url in result.value["links"] if url.endswith(("/profile", "/api/docs"))]
        assert result.calls_of("open_url") == [["https://jimaku.cc/account"]]
    # The first-run guide opens the same AniList token page as Settings.
    onboarding = page.run(
        r"""
        const next = async () => { await __click('#onboardingNext'); await __tick(20); };
        while (ui.onboardingStep < 3) await next();
        await __input('#o_anilist_enabled', true);
        await __input('#o_anilist_client', '12345');
        await __click('#o_anilist_flow [data-anilist-continue]');
        await __click('#onboardingAniListAuthorize'); await __tick(10);
        """,
        state={"settings": {"onboarding_completed": False}},
        api={"first_experience_dependencies": {"mpv": {"installed": True}, "ffmpeg": {"installed": True}}},
    )
    assert onboarding.calls_of("open_url") == [
        ["https://anilist.co/api/v2/oauth/authorize?client_id=12345&response_type=token"]
    ]
    # policy: the README is the guide people follow to get their keys; there is no behaviour to run.
    assert "## Jimaku and AniList credentials" in source("README.md")


def test_audiobook_status_reports_live_background_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    service._set_transcription_job(
        7,
        {
            "status": "transcribing",
            "ready": False,
            "started_at": 1.0,
            "progress_percent": 42.0,
            "processed_audio_seconds": 420.0,
            "remaining_audio_seconds": 580.0,
            "total_duration": 1000.0,
        },
    )

    status = service.transcription_status(7)

    assert status["background"] is True
    assert status["progress_percent"] == 42.0
    assert status["remaining_audio_seconds"] == 580.0
    assert status["elapsed_seconds"] > 0


def test_audiobook_state_resumes_an_interrupted_queued_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    book = service._upsert(
        path=source,
        title="Book",
        duration=3600.0,
        files=[
            {
                "index": 0,
                "path": str(source),
                "title": "Book",
                "duration": 3600.0,
                "start": 0.0,
                "end": 3600.0,
            }
        ],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 3600.0}],
    )
    # v158: background STT is only meaningful for audiobooks explicitly linked
    # to a light novel.  An unlinked library book must stay idle.
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (7001, int(book["id"]), "chapter", 1.0, 1.0),
        )
    resumed: list[int] = []
    monkeypatch.setattr(
        service,
        "prepare_transcription",
        lambda book_id, **_kwargs: (
            resumed.append(int(book_id)) or {"status": "queued", "ready": False, "background": True}
        ),
    )

    state = service.state()

    assert resumed == [book["id"]]
    assert state["books"][0]["transcription"]["background"] is True


def test_stt_worker_writes_machine_readable_progress(tmp_path: Path, monkeypatch) -> None:
    # The worker reports mlx-whisper's progress bar as JSON for the parent process.
    class Bar:
        def __init__(self, total=None, **_kwargs):
            self.total, self.n = total, 0

        def update(self, n=1):
            self.n += n

    fake = types.ModuleType("tqdm")
    fake.tqdm = Bar
    monkeypatch.setitem(sys.modules, "tqdm", fake)
    monkeypatch.setattr(stt_worker, "_PROGRESS_TARGET", [None])
    output = tmp_path / "chunk.progress.json"
    stt_worker._install_progress_reporter(output)

    def reported() -> dict:
        return json.loads(output.read_text(encoding="utf-8"))  # the worker's report file

    bar = fake.tqdm(total=200)
    assert reported()["percent"] == 0
    bar.update(50)
    assert {key: reported()[key] for key in ("percent", "current", "total")} == {
        "percent": 25,
        "current": 50,
        "total": 200,
    }
    bar.update(150)
    assert reported()["percent"] == 100


AUDIO_BOOKS = [
    {
        "id": 5,
        "title": "Analysed",
        "duration": 1000,
        "position": 0,
        "chapters": [{"start": 0, "title": "One"}, {"start": 500, "title": "Two"}],
        "transcription": {
            "status": "transcribing",
            "ready": False,
            "progress_percent": 42.4,
            "remaining_audio_seconds": 580,
            "processed_audio_seconds": 420,
        },
    },
    {
        "id": 6,
        "title": "Other",
        "duration": 600,
        "position": 0,
        "chapters": [{"start": 0, "title": "A"}, {"start": 300, "title": "B"}],
    },
]


def test_audiobook_card_shows_analysis_percent_and_keeps_open_chapter_lists_on_refresh(page) -> None:
    assert not has_text("Background audio analysis")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks', true); await __tick(100);
            const analysis = __text('[data-audiobook-id="5"] .audiobook-preparation');
            const details = id => document.querySelector(`[data-audiobook-id="${id}"] details.audiobook-chapters`);
            details(5).open = true;
            await PudgeMedia.loadAudio(); await __tick(20);                 // a background refresh redraws the cards
            return {analysis, label: PudgeI18n.t('ui.audioAnalysis'), open5: details(5).open, open6: details(6).open};
            """,
            lang=lang,
            api={"audiobook_state": {"books": AUDIO_BOOKS}},
        )
        out = result.value
        assert out["analysis"].replace("\n", " ") == f"{out['label']} 42%"  # no remaining-time estimate
        assert (out["open5"], out["open6"]) == (True, False)


def test_audiobook_stt_finds_the_configured_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
        ffmpeg=str(ffmpeg),
    )
    monkeypatch.setattr(
        service,
        "_extract_global_audio_window",
        lambda _id, destination, *, start, duration: (destination.write_bytes(b"flac"), (start, duration))[1],
    )
    seen: list[dict[str, str]] = []

    def run(command, *, env, **_kwargs):
        seen.append(env)
        Path(command[5]).write_text(
            json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "こんにちは"}]}), encoding="utf-8"
        )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pudge.audiobooks.subprocess.run", run)
    segments, info = service._precision_chapter_start_segments(
        5, tmp_path / "alignment.json", chapter_index=1, reference_time=60.0, total_duration=600.0
    )
    assert segments and info["cached"] is False
    # The STT worker decodes audio with ffmpeg: the configured one comes first on its PATH.
    assert seen[0]["PATH"].split(":")[0] == str(tools.resolve())
    # Without a configured path the service uses "ffmpeg" from PATH.
    assert (
        AudiobookService(
            Database(tmp_path / "db2.sqlite3"),
            ffprobe="ffprobe",
            mpv="mpv",
            cache_dir=tmp_path / "cache2",
            ffmpeg=None,
        ).ffmpeg
        == "ffmpeg"
    )


def _web_api(tmp_path: Path, **tools: str) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    for name, value in tools.items():
        setattr(cfg.tools, name, value)
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_window_resumes_audiobook_analysis_in_the_background_at_start(tmp_path: Path, monkeypatch) -> None:
    started = threading.Event()
    threads: list[str] = []

    def resume(self) -> None:
        threads.append(threading.current_thread().name)
        started.set()

    monkeypatch.setattr(AudiobookService, "resume_pending_transcriptions", resume)
    api = _web_api(tmp_path, ffmpeg="/opt/tools/ffmpeg")
    assert started.wait(5)
    assert threads != [threading.main_thread().name] and "audiobook-stt-resume" in threads[0]
    assert api.audiobooks.ffmpeg == "/opt/tools/ffmpeg"  # Settings' ffmpeg reaches audiobook STT

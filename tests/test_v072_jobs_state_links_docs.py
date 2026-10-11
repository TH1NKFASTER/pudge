"""Episode states, the job center's backend, LN/audiobook links, the two-click Finish Volume,
the bundled Jimaku trial key (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.episode_state import transition_episode_state
from pudge.job_center import JobCenter
from pudge.light_novels import LightNovelService
from pudge.manager_models import LibraryEpisode
from css_support import css_rules
from source_policy import source
from t71g01_support import Recorder, build_release, load_workflow, run_step, stand_in, workflow_step
from tests.i18n_support import en, has_text

from pudge import jimaku_trial


ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    return config


def test_episode_state_machine_protects_scans_and_allows_explicit_repair() -> None:
    assert transition_episode_state("ready", "waiting_subtitles", trigger="scan") == "ready"
    assert transition_episode_state("watched", "local", trigger="scan") == "watched"
    assert (
        transition_episode_state("waiting_text_subtitles", "waiting_subtitles", trigger="scan")
        == "waiting_text_subtitles"
    )
    assert (
        transition_episode_state("ready", "waiting_subtitles", trigger="subtitle_invalidated")
        == "waiting_subtitles"
    )


def test_database_records_explicit_episode_transition(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    database.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Episode",
            episode=1,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    subtitle = tmp_path / "episode.ja.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")
    database.set_subtitle_ready(video, subtitle, origin="external")

    assert database.episode_by_path(video).state == "ready"  # type: ignore[union-attr]
    with database.connect() as conn:
        row = conn.execute(
            "SELECT from_state,to_state,trigger FROM episode_state_history "
            "WHERE video_path=? ORDER BY id DESC LIMIT 1",
            (str(video),),
        ).fetchone()
    assert dict(row) == {
        "from_state": "waiting_subtitles",
        "to_state": "ready",
        "trigger": "subtitle_ready",
    }


def test_job_center_lifecycle_retry_metadata_and_restart_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    center = JobCenter(database)
    first = center.start("import", "Import LN", payload={"paths": ["book.epub"]}, total=2)
    center.update(first, current=1, total=2, message="Imported 1/2")
    assert center.get(first)["progress"] == 0.5  # type: ignore[index]
    center.fail(first, "bad archive")
    assert center.get(first)["can_retry"] is True  # type: ignore[index]

    retry = center.start("import", "Import LN", attempt_of=first)
    center.finish(retry, result={"imported": 1})
    assert center.get(retry)["attempt_of"] == first  # type: ignore[index]
    assert center.get(retry)["can_retry"] is False  # type: ignore[index]

    interrupted = center.start("ocr", "OCR")
    assert center.request_cancel(interrupted) is True
    center.update(interrupted, state="running", current=1)
    assert center.get(interrupted)["state"] == "cancel_requested"  # type: ignore[index]
    recovered = JobCenter(database).get(interrupted)
    assert recovered is not None and recovered["state"] == "failed"
    assert "closed" in recovered["error"].lower()


def test_remove_finished_is_local_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = LightNovelService(config)
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,"
            "anilist_progress_volumes,finished,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            ("Series Vol. 2", str(tmp_path / "v2.epub"), "epub", 2, 1234, 2, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    book = service.set_finished(book_id, False)
    assert book["finished"] == 0
    assert book["anilist_progress_volumes"] == 2


def test_audiobook_auto_links_matching_ln_and_propagates_anilist(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = Database(config.library.database_path)
    novels = LightNovelService(config)
    now = time.time()
    with novels._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,cover_url,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "Spice and Wolf Volume 03",
                str(tmp_path / "spice-v03.epub"),
                "epub",
                3,
                127,
                "https://img.example/127.jpg",
                now,
                now,
            ),
        )
        novel_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    audio = AudiobookService(
        database,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=config.paths.cache_dir,
    )
    book = audio._upsert(
        path=tmp_path / "Spice and Wolf Vol 3.m4b",
        title="Spice and Wolf Vol 3",
        duration=100.0,
        files=[],
        chapters=[],
    )

    linked = audio.auto_link_audiobook(int(book["id"]))
    assert linked is not None and linked["ln_book_id"] == novel_id
    enriched = audio.book(int(book["id"]))
    assert enriched["linked_light_novel"]["id"] == novel_id
    assert enriched["anilist_id"] == 127


def test_inflected_pitch_uses_surface_reading() -> None:
    script = f"""
global.window=global;global.document={{addEventListener(){{}}}};
require({json.dumps(str(ROOT / "pudge/web/reading_tools.js"))});
const card=PudgeReadingTools.study.inflectedPitchCard(
  '食べました',
  {{rubies:[{{start:0,end:1,text:'た'}}]}},
  {{reading:'たべる',pitchAccents:[2]}}
);
process.stdout.write(JSON.stringify(card));
"""
    payload = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert payload["reading"] == "たべました"
    assert payload["pitchAccents"] == [2]
    assert payload["pitchDerived"] is True


LN_SHELF = r"""
__api.light_novel_state = async () => ({version: 1, settings: {}, books: [
  {id: 6, title: 'Novel', volume: 1, finished: 0}, {id: 7, title: 'Done', volume: 2, finished: 1}]});
__api.light_novel_open = async id => ({id, title: 'Novel', finished: 0, current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫'], tokens: [[]], vocabulary: []});
__api.light_novel_finish_volume = async id => ({id, title: 'Novel', finished: 1});
__api.light_novel_set_finished = async (id, finished) => ({id, finished: finished ? 1 : 0});
"""


def test_finish_volume_needs_a_second_click_and_remove_finished_is_offered_for_finished_books(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels'); await __tick(100);
            // Remove Finished in a finished book's menu: only the local mark (AniList progress stays).
            await __fire('[data-ln-book="7"]', 'contextmenu', {clientX: 10, clientY: 10}, 'MouseEvent'); await __tick(10);
            const out = {menu: __text('#contextMenu').split('\n')};
            await __click('[data-ln-context-action="unfinish"]'); await __tick(50);
            await __settle(openLightNovel(6)); await __tick(100);
            const label = () => $('lnFinishVolume').getAttribute('aria-label');
            const finishing = () => __apiCalls.filter(c => c.name === 'light_novel_finish_volume').length;
            out.start = label();
            await __click('#lnFinishVolume'); await __tick(10);
            out.armed = [label(), finishing()];
            await __tick(5200); out.expired = label();   // the second click must come within 5 s
            await __click('#lnFinishVolume'); await __tick(10);
            await __click('#lnFinishVolume'); await __tick(50);
            out.done = label(); out.toasts = __toasts();
            out.texts = {finish: t('ui.finishVolume'), again: t('ui.clickAgainToFinish'), done: t('ui.volumeFinished'),
                         completed: t('ui.volumeCompleted'), remove: t('ui.removeFinished')};
            return out;
            """,
            lang=lang,
            before=LN_SHELF,
            state={"settings": {"review_gate_ln_enabled": False}},
        )
        out, texts = result.value, result.value["texts"]
        assert texts["remove"] in out["menu"]
        assert result.calls_of("light_novel_set_finished") == [[7, False]]
        assert out["start"] == texts["finish"] and out["expired"] == texts["finish"]
        assert out["armed"] == [texts["again"], 0]
        assert result.calls_of("light_novel_finish_volume") == [[6]]
        assert out["done"] == texts["done"] and texts["completed"] in out["toasts"]
    assert (
        en("ui.clickAgainToFinish") == "Click again to finish"
        and en("ui.removeFinished") == "Remove Finished"
    )


def test_job_center_is_gone_and_audiobooks_offer_no_ln_search(page) -> None:
    result = page.run(
        r"""
        const out = {elements: document.querySelectorAll('#openJobCenter, #jobs').length};
        for (const name of ['current', 'lightnovels', 'manga', 'audiobooks', 'planned', 'statistics', 'settings']) {
          setPage(name); await __tick(200);
        }
        await __fire('.audiobook-cover[data-audio-cover-id="3"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
        out.menu = __text('#contextMenu').split('\n');
        out.expected = [t('media.play'), t('action.openAniList'), t('ui.changeAnilist'), t('ui.editVolumeAndSeries'),
                        t('media.showInFinder'), t('manga.removeFromPudge')];
        out.calls = __apiCalls.map(call => call.name).filter(name => name.startsWith('job_center') || name.includes('light_novel_nyaa'));
        return out;
        """,
        api={
            "audiobook_state": {
                "books": [{"id": 3, "title": "Book", "duration": 600, "cover_url": "/a.jpg", "anilist_id": 9}]
            }
        },
    )
    assert result.value["elements"] == 0 and result.value["calls"] == []
    # The audiobook menu has no Light Novel search on Nyaa any more.
    assert result.value["menu"] == result.value["expected"] and not has_text("Find LN on Nyaa")
    # Settings lay their section titles across the whole form grid.
    assert css_rules("index.html").get(".form-grid > .setting-divider-title", "grid-column") == "1 / -1"


def test_release_build_bundles_the_trial_jimaku_key_from_the_ci_secret(tmp_path: Path, monkeypatch) -> None:
    build = build_release(
        tmp_path / "build",
        env={
            "PUDGE_TRIAL_JIMAKU_API_KEY": "trial-123",
            "PUDGE_TRIAL_JIMAKU_PROXY_URL": "https://proxy.invalid/jimaku",
        },
    )
    assert build.result.returncode == 0, build.result.output
    wheel = build.project / "dist/pudge-9.8.7-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(tmp_path / "installed")
        shipped = {name: archive.read(name) for name in archive.namelist()}
    assert shipped["pudge/assets/jimaku-trial-key"] == b"trial-123"
    assert not any(
        b"proxy.invalid" in data for data in shipped.values()
    )  # no proxy: the key is used directly
    # The checkout does not keep the secret after the build.
    assert not (build.project / "pudge/assets/jimaku-trial-key").exists()
    # The installed app reads that asset as its trial key.
    monkeypatch.delenv("PUDGE_BUNDLED_JIMAKU_API_KEY", raising=False)
    monkeypatch.setattr(jimaku_trial.resources, "files", lambda _package: tmp_path / "installed/pudge")
    assert jimaku_trial.bundled_jimaku_api_key() == "trial-123"
    # Without the secret nothing is bundled.
    plain = build_release(tmp_path / "plain")
    assert (
        "pudge/assets/jimaku-trial-key"
        not in zipfile.ZipFile(plain.project / "dist/pudge-9.8.7-py3-none-any.whl").namelist()
    )


def test_release_workflow_refuses_to_build_without_the_trial_key_secret(tmp_path: Path) -> None:
    # policy: the workflow file is configuration for GitHub; its build step is run here.
    step = workflow_step(
        load_workflow(source(".github/workflows/release.yml")), "build-release", "Build release archive"
    )
    assert step["env"]["PUDGE_TRIAL_JIMAKU_API_KEY"] == "${{ secrets.PUDGE_TRIAL_JIMAKU_API_KEY }}"
    calls = Recorder(tmp_path / "ci")
    stand_in(
        tmp_path / "build_release.sh",
        'record build_release "SKIP_TESTS=$SKIP_TESTS" "KEY=$PUDGE_TRIAL_JIMAKU_API_KEY"\n',
    )
    missing = run_step(step, tmp_path, tools=calls.bin, env=calls.env)
    assert missing.returncode != 0 and calls.calls() == []
    built = run_step(
        step,
        tmp_path,
        tools=calls.bin,
        env=calls.env,
        expressions={"secrets.PUDGE_TRIAL_JIMAKU_API_KEY": "trial-123"},
    )
    assert built.returncode == 0, built.stderr
    assert calls.calls("build_release") == [["SKIP_TESTS=1", "KEY=trial-123"]]
    assert (ROOT / "CONTRIBUTING.md").is_file()

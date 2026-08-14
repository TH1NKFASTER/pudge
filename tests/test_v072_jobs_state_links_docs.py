from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.episode_state import transition_episode_state
from pudge.job_center import JobCenter
from pudge.light_novels import LightNovelService
from pudge.manager_models import LibraryEpisode


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
        transition_episode_state(
            "waiting_text_subtitles", "waiting_subtitles", trigger="scan"
        )
        == "waiting_text_subtitles"
    )
    assert (
        transition_episode_state(
            "ready", "waiting_subtitles", trigger="subtitle_invalidated"
        )
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
require({json.dumps(str(ROOT / 'pudge/web/reading_tools.js'))});
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


def test_frontend_bundled_key_and_documentation_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    trial = (ROOT / "pudge/jimaku_trial.py").read_text(encoding="utf-8")
    build = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "Click again to finish" in html and "Remove Finished" in html
    assert ".form-grid > .setting-divider-title { grid-column:1 / -1" in html
    assert "job_center_cancel" in html and "job_center_retry" in html
    assert 'id="openJobCenter"' in html and 'data-page="jobs"' not in html
    assert "if(!book.linked_light_novel)" in media
    assert "Find LN on Nyaa" in media and "audiobook_search_light_novel_nyaa" in media
    assert "jimaku-trial-key" in trial + build
    assert "PUDGE_TRIAL_JIMAKU_API_KEY" in build
    assert "secrets.PUDGE_TRIAL_JIMAKU_API_KEY" in workflow
    assert 'test -n "$PUDGE_TRIAL_JIMAKU_API_KEY"' in workflow
    assert "PUDGE_TRIAL_JIMAKU_PROXY_URL" not in trial + build
    assert (ROOT / "docs/USER_GUIDE.md").is_file()
    assert (ROOT / "docs/ALGORITHMS.md").is_file()

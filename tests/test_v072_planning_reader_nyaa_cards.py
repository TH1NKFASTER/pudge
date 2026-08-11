from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manager_models import NyaaRelease


ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _release(title: str, info_hash: str) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://nyaa.si/view/{info_hash}",
        torrent_url=f"https://nyaa.si/download/{info_hash}.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=1_200_000_000,
        seeders=12,
        leechers=1,
        downloads=50,
        trusted=False,
        remake=False,
    )


def test_literature_search_uses_raw_category_and_target_volume(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeNyaa:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, query: str, *, category: str):
            calls.append((query, category))
            return [
                _release(
                    "[Novel] 狼と香辛料 第01-22巻 [Ookami to Koushinryou vol 01-22]",
                    "batch",
                ),
                _release("狼と香辛料 volume 23", "wrong"),
            ]

    monkeypatch.setattr("pudge.light_novels.NyaaClient", FakeNyaa)
    service = LightNovelService(_config(tmp_path))

    rows = service.search_nyaa(
        "狼と香辛料 light novel volume 01", target_volume=1
    )

    assert calls == [("狼と香辛料", "3_3")]
    assert [row["info_hash"] for row in rows] == ["batch"]
    assert rows[0]["volume_range"] == [1, 22]
    assert rows[0]["contains_target_volume"] is True


def test_batch_file_selection_keeps_only_requested_volume(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))

    class FakeQbt:
        def __init__(self):
            self.calls: list[tuple[list[int], int]] = []

        def files(self, _torrent_hash: str):
            return [
                {"index": 0, "name": "Ookami vol 01-22/Ookami 01.epub"},
                {"index": 1, "name": "Ookami vol 01-22/Ookami 02.epub"},
                {"index": 2, "name": "Ookami vol 01-22/cover.jpg"},
            ]

        def set_file_priority(self, _torrent_hash: str, ids: list[int], priority: int):
            self.calls.append((ids, priority))

    qbt = FakeQbt()
    selected = service._select_torrent_volume_files(qbt, "hash", 2)

    assert selected == ["Ookami vol 01-22/Ookami 02.epub"]
    assert qbt.calls == [([0, 1, 2], 0), ([1], 6)]


def test_single_batch_archive_extracts_only_requested_volume(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    archive_path = service.root / "Ookami vol 01-22.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Ookami 01.epub", b"first")
        archive.writestr("Ookami 02.epub", b"second")

    class FakeQbt:
        def __init__(self):
            self.calls: list[tuple[list[int], int]] = []

        def files(self, _torrent_hash: str):
            return [{"index": 0, "name": archive_path.name}]

        def set_file_priority(self, _torrent_hash: str, ids: list[int], priority: int):
            self.calls.append((ids, priority))

    qbt = FakeQbt()
    selected = service._select_torrent_volume_files(qbt, "hash", 2)
    cleanup = service._materialize_selected_volume(
        [{"index": 0, "name": selected[0], "priority": 6, "progress": 1.0}],
        2,
    )

    assert selected == [archive_path.name]
    assert cleanup == [archive_path.resolve()]
    assert (service.root / "Ookami 02.epub").read_bytes() == b"second"
    assert not (service.root / "Ookami 01.epub").exists()


def test_audiobook_uses_linked_light_novel_cover_and_anilist(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = Database(config.library.database_path)
    novels = LightNovelService(config)
    now = time.time()
    with novels._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "Spice and Wolf Vol. 1",
                str(tmp_path / "spice.epub"),
                "epub",
                1,
                127,
                "https://img.example/spice.jpg",
                now,
                now,
            ),
        )
        novel_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    audio = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=config.paths.cache_dir)
    audiobook = audio._upsert(
        path=tmp_path / "spice.m4b",
        title="Spice and Wolf Vol. 1",
        duration=100.0,
        files=[],
        chapters=[],
    )
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(?,?,'chapter',?,?)",
            (novel_id, int(audiobook["id"]), now, now),
        )

    enriched = audio.book(int(audiobook["id"]))

    assert enriched["cover_url"] == "https://img.example/spice.jpg"
    assert enriched["anilist_id"] == 127
    assert enriched["anilist_site_url"] == "https://anilist.co/manga/127"


def test_matching_kana_pitch_is_rendered_on_the_word() -> None:
    script = f"""
global.window=global;global.document={{addEventListener(){{}}}};
require({json.dumps(str(ROOT / 'pudge/web/reading_tools.js'))});
const yes=PudgeReadingTools.study.inlinePitchOnSurface('かな',{{reading:'カナ',pitchAccents:[1]}});
const no=PudgeReadingTools.study.inlinePitchOnSurface('仮名',{{reading:'かな',pitchAccents:[1]}});
process.stdout.write(JSON.stringify({{yes,no}}));
"""
    payload = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert "pudge-inline-pitch" in payload["yes"]
    assert payload["no"] == ""


def test_pitch_accent_color_is_independent_and_persists(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    saved = service.save_settings(
        {"word_color_due": "#ff0000", "pitch_accent_color": "#12abef"}
    )

    assert saved["word_color_due"] == "#ff0000"
    assert saved["pitch_accent_color"] == "#12abef"
    assert LightNovelService(service.config).settings_payload()["pitch_accent_color"] == "#12abef"


def test_planning_reader_cards_and_dependency_frontend_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")

    assert all(
        item in html
        for item in (
            "plannedDifficultyFilter",
            "plannedLengthFilter",
            "plannedKnownFilter",
            "uniqueWords",
            "queueAllPlanningJiten",
            "lnrPitchColor",
            "writeLnManagedColorCss",
            "syncLnColorPickersFromCss",
            "Change character names",
            "name-cues",
            "syncLnReaderControlAvailability",
            'data-ln-card-jiten',
        )
    )
    assert "lnCharacterNames" not in html
    assert "settings-unavailable" in html and "semantic.checked=false" in html
    assert '<ol class="meanings">' in html
    assert "open-audio-cover" in media
    assert ".audiobook-card.has-cover" in css and ".audiobook-scrubber" in css

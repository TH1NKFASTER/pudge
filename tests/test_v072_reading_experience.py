from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from pudge.audiobooks import AudiobookService
from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.light_novels import LightNovelService, _is_technical_epub_section
from pudge.manga import MangaService, _box_overlap, _merge_text_regions
from pudge.manga_ocr_worker import _crop_region, _recognize_regions
from pudge.metadata_cache import MetadataCache
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel,
    light_novel_position_for_audio,
    normalize_reading_text,
)


def test_manga_regions_merge_vertical_lines_and_crop_with_vision_coordinates() -> None:
    regions = _merge_text_regions(
        [
            {"text": "猫", "x": 0.60, "y": 0.30, "width": 0.04, "height": 0.20},
            {"text": "です", "x": 0.52, "y": 0.31, "width": 0.04, "height": 0.18},
        ]
    )
    assert len(regions) == 1
    assert regions[0]["text"] == "猫です"
    assert regions[0]["orientation"] == "vertical"

    image = Image.new("RGB", (100, 200), "white")
    crop = _crop_region(
        image,
        {"x": 0.2, "y": 0.1, "width": 0.3, "height": 0.2},
    )
    assert crop.width >= 30
    assert crop.height >= 40

    stacked_glyphs = _merge_text_regions(
        [
            {"text": "縦", "x": 0.61, "y": 0.40, "width": 0.025, "height": 0.027},
            {"text": "書", "x": 0.61, "y": 0.365, "width": 0.025, "height": 0.027},
            {"text": "き", "x": 0.61, "y": 0.33, "width": 0.025, "height": 0.027},
        ]
    )
    assert len(stacked_glyphs) == 1
    assert stacked_glyphs[0]["text"] == "縦書き"
    assert stacked_glyphs[0]["orientation"] == "vertical"
    assert _box_overlap(
        {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
        {"x": 0.12, "y": 0.12, "width": 0.05, "height": 0.05},
    ) > 0.99


def test_manga_ocr_calls_model_per_region_not_for_the_full_page() -> None:
    sizes: list[tuple[int, int]] = []

    def model(image: Image.Image) -> str:
        sizes.append(image.size)
        return f"region-{len(sizes)}"

    image = Image.new("RGB", (1000, 1200), "white")
    recognized = _recognize_regions(
        model,
        image,
        [
            {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            {"x": 0.6, "y": 0.5, "width": 0.1, "height": 0.3},
        ],
    )
    assert [item["text"] for item in recognized] == ["region-1", "region-2"]
    assert len(sizes) == 2
    assert all(width < image.width and height < image.height for width, height in sizes)


def test_stt_alignment_maps_real_ln_chapters_and_word_clock() -> None:
    first = "吾輩は猫である。名前はまだない。どこで生まれたか見当がつかぬ。"
    second = "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    spoken = normalize_reading_text(first + second)
    alignment = align_light_novel_to_transcript(
        [
            {"chapter_index": 0, "title": "第一章", "text": first},
            {"chapter_index": 3, "title": "第二章", "text": second},
        ],
        [{"start": 10.0, "end": 50.0, "text": spoken}],
        duration=60.0,
        model="test-model",
    )
    assert [row["chapter_index"] for row in alignment["chapters"]] == [0, 3]
    start = audio_position_for_light_novel(alignment, 3, 0.5)
    assert start is not None and 10 < start < 60
    mapped = light_novel_position_for_audio(alignment, start)
    assert mapped is not None
    assert mapped["chapter_index"] == 3
    assert 0.40 <= mapped["chapter_progress"] <= 0.60


def test_epub_technical_sections_are_removed_but_afterword_is_kept() -> None:
    rights = (
        "本書の著作権およびその他の権利は正当な権利を有する第三者に帰属します。"
        "無断転載および複製・転載を禁じます。"
    )
    assert _is_technical_epub_section("奥付", "発行 2026年") is True
    assert _is_technical_epub_section("Chapter 1", rights) is True
    assert _is_technical_epub_section("あとがき", "読者のみなさん、ありがとうございました。") is False


def test_audiobook_bookmarks_sleep_and_light_novel_link(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda _path: (600.0, []))
    book = service.import_file(source)
    service.set_position(book["id"], 120)

    bookmark = service.add_bookmark(book["id"], "Good scene")
    assert bookmark["book"]["bookmarks"][0]["title"] == "Good scene"
    assert bookmark["book"]["bookmarks"][0]["position"] == 120
    timer = service.set_sleep_timer(book["id"], seconds=900)
    assert 895 <= timer["book"]["sleep_timer_seconds"] <= 900

    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE ln_chapters ("
            "id INTEGER PRIMARY KEY,book_id INTEGER,chapter_index INTEGER,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        novel_id = 7
        for index in range(3):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) "
                "VALUES(?,?,?,?,?)",
                (novel_id, index, f"Chapter {index + 1}", "text", f"hash-{index}"),
            )
    link = service.link_light_novel(novel_id, book["id"])
    assert link["link"]["book"]["id"] == book["id"]
    audio_id, position = service._paired_audio_position(novel_id, 1, 0.5)
    assert audio_id == book["id"]
    assert position == 300


def test_audiobook_service_prefers_cached_stt_alignment_over_chapter_counts(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
        stt_model="test-model",
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (120.0, []))
    audiobook = service.import_file(source)
    first = "吾輩は猫である。名前はまだない。どこで生まれたか見当がつかぬ。"
    second = "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE ln_chapters ("
            "id INTEGER PRIMARY KEY,book_id INTEGER,chapter_index INTEGER,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )
        for index, title, text in ((0, "第一章", first), (4, "第二章", second)):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) "
                "VALUES(?,?,?,?,?)",
                (9, index, title, text, f"hash-{index}"),
            )
    service.link_light_novel(9, audiobook["id"])
    alignment = align_light_novel_to_transcript(
        [
            {"chapter_index": 0, "title": "第一章", "text": first},
            {"chapter_index": 4, "title": "第二章", "text": second},
        ],
        [{"start": 10.0, "end": 110.0, "text": first + second}],
        duration=120.0,
        model="test-model",
    )
    path = service._alignment_path(9, audiobook["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alignment, ensure_ascii=False), encoding="utf-8")

    audio_id, exact = service._paired_audio_position(9, 4, 0.5)
    assert audio_id == audiobook["id"]
    service.set_position(audio_id, exact)
    state = service.paired_state(9)
    assert state["alignment_mode"] == "stt"
    assert state["ln_chapter_index"] == 4
    assert 0.40 <= state["chapter_progress"] <= 0.60


def test_character_placeholders_survive_spacing_and_cache_is_replaceable(tmp_path: Path) -> None:
    glossary = [{"source": "千早", "preferred": "Chihaya"}]
    protected, replacements = LightNovelService._protect_character_names(
        "千早は走った。", glossary
    )
    assert "千早" not in protected
    restored = LightNovelService._restore_character_names(
        protected.replace("PUDGEZXQ", "pudge z x q "), replacements
    )
    assert restored == "Chihayaは走った。"

    cache = MetadataCache(tmp_path, "test", schema="v2")
    cache.put({"path": "replaceable"}, {"value": 1})
    assert cache.get({"path": "replaceable"}, ttl_seconds=60) == {"value": 1}
    path = next((tmp_path / "metadata" / "test").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "v2"


def test_manga_series_repairs_legacy_link_and_can_unlink_all_volumes(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    now = 123.0
    with db.connect() as conn:
        for title in ("One Piece Vol. 1", "Vol2 One Piece"):
            conn.execute(
                "INSERT INTO manga_books(path,title,page_count,position,reading_direction,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(tmp_path / f"{title}.cbz"), title, 10, 0, "rtl", now, now),
            )
        first_id, second_id = [
            int(row[0])
            for row in conn.execute("SELECT id FROM manga_books ORDER BY id").fetchall()
        ]
        conn.execute(
            "UPDATE manga_books SET anilist_id=30013,cover_url='cover',site_url='site' "
            "WHERE id=?",
            (first_id,),
        )

    service = MangaService(db, cache_dir=tmp_path / "cache")
    books = service.state()["books"]
    assert {book["anilist_id"] for book in books} == {30013}

    service.unbind_anilist(second_id)
    books = service.state()["books"]
    assert {book["anilist_id"] for book in books} == {None}
    assert all(not book["site_url"] for book in books)


def test_schema_and_frontend_contracts_cover_v072_features() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    audio = (root / "pudge/web/media.js").read_text(encoding="utf-8")
    media_css = (root / "pudge/web/media.css").read_text(encoding="utf-8")
    assert LATEST_SCHEMA_VERSION == 4
    assert 'data-manga-context-action="score"' in manga
    assert 'data-manga-context-action="ocr-book"' in manga
    assert "data-manga-score-book" not in manga
    assert "Preparing Jiten" in manga
    assert "ocrWholeBook" not in manga
    assert "parseRegionsSequentially" in manga
    assert "cachedOnly: true" in manga
    assert "pollCurrentBookPreparation" in manga
    assert "closeRegionsOutsidePointer" in manga
    assert "PAGE_CACHE_LIMIT = 8" in manga
    assert "mokuro-regions-v4" in (root / "pudge/manga.py").read_text(encoding="utf-8")
    assert "if (content) content.remove()" in manga
    assert "showLiteratureScoreModal" in html
    assert "planning_search_anilist" in (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "audiobook-scrubber" in audio
    assert "data-audio-sleep" in audio
    assert 'data-media-action="unlink-manga-anilist"' in audio
    assert "audiobook-chapters-open" in audio
    assert ".audiobook-chapters summary" in media_css
    assert "planned-suggestion-info" in html
    assert "description(asHtml:false)" in (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "ln-paired-current" in html
    assert "lnPairedScrubber" in html
    assert "lnPairedSpeed" in html
    assert "lnPairedAlign" not in html
    assert "chapter_char_offset" in html
    assert "light_novel_prepare_audio_alignment" in (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "word_timestamps=word_timestamps" in (root / "pudge/subtitles/stt_worker.py").read_text(encoding="utf-8")
    assert "suppressContinueClickUntil" not in html
    reading_tools = (root / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert reading_tools.count("document.addEventListener('pointerover'") == 1


def test_media_tabs_only_reload_after_actual_navigation() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (root / "pudge/web/media.js").read_text(encoding="utf-8")
    manga = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "if(!force&&ui.page===page)return false" in html
    assert "requestAnimationFrame(()=>loadActivatedPage(page))" in html
    assert "if(b.dataset.page==='lightnovels')void loadLightNovels()" not in html
    assert "loadActivatedPage(page)" in html
    assert "const nav = event.target.closest('.nav button[data-page]')" not in media
    assert "nav?.dataset.page === 'manga'" not in manga

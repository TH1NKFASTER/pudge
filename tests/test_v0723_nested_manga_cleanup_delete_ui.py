from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.manga import MangaService, _natural_key


ROOT = Path(__file__).parents[1]


def _write_image(path: Path, shade: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (3, 3), (shade, shade, shade))
    data = io.BytesIO()
    image.save(data, format="JPEG")
    path.write_bytes(data.getvalue())


def test_nested_mang_zip_pack_imports_one_book_per_volume(tmp_path: Path) -> None:
    pack = tmp_path / "Mang-Zip.info_Akagi Genius Darkness vol 01-36"
    bundle = pack / "Mang-Zip.info_Akagi Genius Darkness v01-05"
    vol1 = bundle / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第01巻"
    vol2 = bundle / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第02巻"

    for index, name in enumerate(
        ["Mang-Zip.info_AKAGI_017.jpg", "Mang-Zip.info_AKAGI_018-019.jpg", "Mang-Zip.info_AKAGI_020.jpg"]
    ):
        _write_image(vol1 / name, 250 - index)
    for index, name in enumerate(
        ["Mang-Zip.info_AKAGI_001.jpg", "Mang-Zip.info_AKAGI_002.jpg"]
    ):
        _write_image(vol2 / name, 240 - index)

    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")

    found = service.discover_image_files(pack)
    books = service.import_image_groups(found)

    assert len(found) == 5
    assert len(books) == 2
    assert [book["volume"] for book in books] == [1, 2]
    assert [book["page_count"] for book in books] == [3, 2]
    assert books[0]["series_key"] == books[1]["series_key"]
    assert "Mang-Zip.info" not in books[0]["title"]
    assert "福本伸行" not in books[0]["title"]
    assert books[0]["series_title"] == "アカギ ~闇に降り立った天才~"

    names = [
        "Mang-Zip.info_AKAGI_020.jpg",
        "Mang-Zip.info_AKAGI_018-019.jpg",
        "Mang-Zip.info_AKAGI_017.jpg",
    ]
    assert sorted(names, key=_natural_key) == [
        "Mang-Zip.info_AKAGI_017.jpg",
        "Mang-Zip.info_AKAGI_018-019.jpg",
        "Mang-Zip.info_AKAGI_020.jpg",
    ]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    cfg.agent.delete_after_watched_hours = 24
    return AnimeManager(cfg, log=lambda _message: None)


def test_cleanup_removes_managed_episode_strictly_behind_anilist_progress(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=200637,
            title="100 Girlfriends S3",
            status="CURRENT",
            progress=7,
            episodes=12,
            format="TV",
        )
    )

    managed6 = tmp_path / "library" / "S03E06.mkv"
    managed7 = tmp_path / "library" / "S03E07.mkv"
    unmanaged5 = tmp_path / "external-S03E05.mkv"
    for path in (managed6, managed7, unmanaged5):
        path.write_bytes(b"video")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=6,
            media_episode=6,
            release_episode=6,
            video_path=managed6,
            state="ready",
        ),
        downloaded_at=1,
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=7,
            media_episode=7,
            release_episode=7,
            video_path=managed7,
            state="ready",
        ),
        downloaded_at=1,
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=5,
            media_episode=5,
            release_episode=5,
            video_path=unmanaged5,
            state="ready",
        )
    )

    deleted = manager.cleanup()

    assert deleted == 1
    assert not managed6.exists()
    assert managed7.exists()
    assert unmanaged5.exists()


def test_delete_actions_are_red_everywhere() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-destructive-red-v1" in html
    assert 'class="danger-action" data-action="torrent-remove"' in html
    assert 'class="danger-action" data-action="torrent-remove-files"' in html
    assert 'class="danger-action" data-ln-context-action="delete"' in html
    assert 'class="danger-action" data-manga-context-action="remove-series"' in manga
    assert "button.danger-action,button.selection-delete,button.uninstall-pudge" in html


def test_drop_router_uses_recursive_grouped_manga_import() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "self.manga.discover_image_files(path, limit=50000)" in source
    assert "self.manga.import_image_groups(image_paths)" in source


def test_jiten_difficulty_palette_is_shared_by_planning_ln_and_manga() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-jiten-difficulty-colours-v1" in html
    assert "jitenDifficultyClass(difficulty)" in html
    assert "data-ln-card-jiten" in html
    assert "data-library-card-jiten" in html
    assert "rgba(21,128,61,.8)" in html
    assert "rgba(34,197,94,.8)" in html
    assert "rgba(6,182,212,.8)" in html
    assert "rgba(217,119,6,.8)" in html
    assert "rgba(220,38,38,.8)" in html
    assert "#86efac" in html
    assert "#bbf7d0" in html
    assert "#67e8f9" in html
    assert "#fcd34d" in html
    assert "#fca5a5" in html


def test_whole_series_hides_length_and_tooltip_but_volume_stats_keep_them() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-hide-series-length-tooltip-v4" in html
    assert "data-ln-series-jiten" in html
    assert "data-manga-series-jiten" in manga
    assert "const wholeSeries=region.hasAttribute('data-ln-series-jiten')||region.hasAttribute('data-manga-series-jiten');" in html
    assert "const displayedLength=wholeSeries?'':length;" in html
    assert "{text:displayedLength,className:''}" in html
    assert "if(wholeSeries){" in html
    assert "region.innerHTML='<button data-action=\"url\" data-url=\"'+escapeHtml(shown.url||'')+'\">'+chips+'</button>';" in html
    assert "shown.word_count" in html
    assert "shown.unique_word_count" in html


def test_nested_mangazip_aliases_become_one_series_and_reimport_migrates_legacy(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    root = tmp_path / "Mang-Zip.info_Akagi Genius Darkness vol 01-36"
    folders = [
        root / "Mang-Zip.info_Akagi Genius Darkness v01-05" / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第01巻",
        root / "Mang-Zip.info_Akagi Genius Darkness v01-05" / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第02巻",
        root / "Mang-Zip.info_Akagi Genius Darkness v06-10" / "Mang-Zip.info_ƒAƒJƒM ‘æ06Ša",
        root / "Mang-Zip.info_Akagi Genius Darkness v06-10" / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第09巻",
        root / "Mang-Zip.info_Akagi Genius Darkness v28",
        root / "Mang-Zip.info_Akagi Genius Darkness v30-31" / "Mang-Zip.info_30",
        root / "Mang-Zip.info_Akagi Genius Darkness v32",
    ]
    pages: list[Path] = []
    for index, folder in enumerate(folders, 1):
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / f"page-{index:02d}.jpg"
        page.write_bytes(b"not-a-real-jpeg")
        pages.append(page)
    legacy = service.import_images([pages[2]], title=folders[2].name)
    legacy_id = int(legacy["id"])
    books = service.import_image_groups(pages, source_root=root)
    assert len(books) == 7
    assert sorted(int(book["volume"]) for book in books) == [1, 2, 6, 9, 28, 30, 32]
    assert len({book["series_key"] for book in books}) == 1
    assert {book["series_title"] for book in books} == {"アカギ ~闇に降り立った天才~"}
    assert any(int(book["id"]) == legacy_id and int(book["volume"]) == 6 for book in books)
    assert not any("ƒAƒJƒM" in str(book["title"]) for book in books)
    assert not any(str(book["title"]).strip() in {"30", "31"} for book in books)


def test_manga_import_button_uses_recursive_folder_picker() -> None:
    web = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "def choose_manga_folder(self)" in web
    assert "webview.FOLDER_DIALOG" in web
    assert 'directory=str(Path.home() / "Downloads")' in web
    assert "self.manga.discover_image_files(source_root, limit=50000)" in web
    assert "self.manga.import_image_groups(" in web
    assert "source_root=source_root" in web
    assert 'path.suffix.casefold() in {".cbz", ".zip"}' in web
    assert "API().choose_manga_folder()" in manga


def test_manga_series_selection_context_and_score_scope_are_series_aware() -> None:
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-manga-series-context-spread-v1" in manga
    assert 'data-manga-series-key="${esc(group.key)}"' in manga
    assert 'data-manga-series-ids="${seriesIds}"' in manga
    assert "toggleSeriesSelection(group.books)" in manga
    assert "showMangaSeriesContextMenu(group" in manga
    assert 'data-manga-context-action="ocr-series"' in manga
    assert 'data-manga-context-action="score-series"' in manga
    assert 'data-manga-context-action="score">' not in manga
    assert "startLibrarySeriesOcr(series.books" in manga


def test_manga_mean_score_is_persisted_and_personal_score_is_series_scoped(
    tmp_path: Path,
) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(manga_books)").fetchall()}
    assert "mean_score" in columns

    service = MangaService(db, cache_dir=tmp_path / "cache")
    paths = [tmp_path / "Score Series Vol. 1.cbz", tmp_path / "Score Series Vol. 2.cbz"]
    for path in paths:
        with zipfile.ZipFile(path, "w") as archive:
            buffer = io.BytesIO()
            Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
            archive.writestr("001.jpg", buffer.getvalue())

    one = service.import_file(paths[0])
    two = service.import_file(paths[1])
    service.bind_anilist(
        int(one["id"]),
        12345,
        site_url="https://anilist.co/manga/12345",
        user_score=7.0,
        mean_score=84.0,
    )

    linked_ids = {int(one["id"]), int(two["id"])}
    linked = [book for book in service.state()["books"] if int(book["id"]) in linked_ids]
    assert {book["mean_score"] for book in linked} == {84.0}
    assert {book["user_score"] for book in linked} == {7.0}

    service.set_score(int(two["id"]), 9.0)
    linked = [book for book in service.state()["books"] if int(book["id"]) in linked_ids]
    assert {book["user_score"] for book in linked} == {9.0}


def test_manga_anilist_search_requests_and_binds_public_mean_score() -> None:
    web = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "status meanScore title{" in web
    assert '"mean_score": float(media.get("meanScore"))' in web
    assert 'mean_score=item.get("mean_score")' in web


def test_ln_and_manga_series_cards_share_selection_hover_context_and_score_model() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-ln-manga-series-parity-score-v1" in html
    assert "pudge-v0.7.23-manga-series-score-parity-v1" in manga
    assert 'data-ln-series-ids="${seriesIds}"' in html
    assert 'data-ln-series-select="${escapeHtml(key)}"' in html
    assert "toggleLnSeriesSelection(group)" in html
    assert "showLnSeriesMenu(group" in html
    assert 'data-ln-context-action="score-series"' in html
    assert 'data-ln-context-action="score">' not in html
    assert 'data-manga-context-action="score-series"' in manga
    assert ".ln-series-group:hover:not(:has(.ln-entry:hover))" in html
    assert ".ln-series-group.series-selected" in html
    assert "ln-series-stats" in html
    assert "ln-series-stats" in manga
    assert "const seriesMean = Number(first.mean_score || 0);" in manga
    assert "const score = Number(book.mean_score || 0);" in manga


def test_anilist_scores_use_ten_point_colored_face_chips_everywhere() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "function anilistScoreChip(value,scale='auto')" in html
    assert "function anilistScoreEmoji(value,scale='auto')" in html
    assert "score.toFixed(1)" in html
    assert "🤩" in html and "😊" in html and "🙂" in html
    assert "😐" in html and "🙁" in html and "😞" in html
    assert "anilist-score-chip" in html
    assert "anilistScoreChip(a.mean_score,'percent')" in html
    assert "anilistScoreChip(row.mean_score,'percent')" in html
    assert "anilistScoreChip(score,'percent')" in html
    assert "window.PudgeAniListScore.chip(score, 'percent')" in manga
    assert "AniList ${Math.round(score)}%" not in html
    assert "${Math.round(Number(row.mean_score))}%" not in html
    assert "AniList ${score.toFixed(score % 1 ? 1 : 0)}/10" not in manga



def test_existing_linked_manga_can_backfill_public_anilist_mean_score(tmp_path: Path) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    path = tmp_path / "Backfill Series Vol. 1.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
        archive.writestr("001.jpg", buffer.getvalue())

    book = service.import_file(path)
    service.bind_anilist(int(book["id"]), 99999, site_url="https://anilist.co/manga/99999")
    assert service._payload(service._book(int(book["id"])))["mean_score"] is None

    assert service.set_mean_scores({99999: 84.0}) == 1
    updated = service._payload(service._book(int(book["id"])))
    assert updated["mean_score"] == 84.0


def test_manga_state_backfills_missing_public_scores_once_per_process() -> None:
    web = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-manga-mean-score-backfill-v1" in web
    assert "media(id_in:$ids,type:MANGA){id meanScore}" in web
    assert 'book.get("mean_score") is None' in web
    assert "return self._backfill_manga_mean_scores(self.manga.state())" in web


def test_series_stats_use_remaining_header_width_before_wrapping() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-series-stats-width-v1" in html
    assert ".ln-series-title-block{flex:1 1 0;width:auto}" in html
    assert ".ln-series-head>.ln-series-count{flex:0 0 auto}" in html
    assert ".ln-series-stats{width:100%;min-width:0}" in html


def test_manga_progress_bar_tooltip_matches_ln_style() -> None:
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-manga-progress-tooltip-v1" in manga
    assert "`${read} / ${total} ${ru() ? 'страниц' : 'pages'}`" in manga
    assert '<div class="ln-card-progress" data-tooltip="${esc(progressTitle)}" aria-label="${esc(progressTitle)}">' in manga
    assert '<div class="ln-card-progress" title="${esc(progressTitle)}">' not in manga



def test_popular_media_score_faces_use_distribution_aware_boundaries() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-popular-score-distribution-v1" in html
    assert "if(score>=8.0)return '🤩';" in html
    assert "if(score>=7.5)return '😊';" in html
    assert "if(score>=6.8)return '🙂';" in html
    assert "if(score>=6.1)return '😐';" in html
    assert "if(score>=5.4)return '🙁';" in html
    assert "(score-5.4)/(8.4-5.4)*120" in html
    assert "if(score>=9)return '🤩';" not in html
    assert "if(score>=8)return '😊';" not in html


def test_series_score_wraps_only_at_full_card_right_edge_and_never_overlaps_volumes() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-series-stats-full-width-v2" in html
    assert ".ln-series-head{position:relative;display:block;min-width:0}" in html
    assert ".ln-series-title-block>strong{display:block;box-sizing:border-box;padding-right:120px}" in html
    assert ".ln-series-head>.ln-series-count{position:absolute;right:0;top:0;z-index:1;white-space:nowrap}" in html
    assert ".ln-series-stats{display:flex;width:100%;max-width:none;min-width:0;" in html
    assert ".ln-series-stats>.planned-jiten{flex:0 0 auto;width:max-content;max-width:none;" in html
    assert ".ln-series-stats>.planned-jiten button{width:max-content;max-width:none;flex-wrap:nowrap}" in html


def test_manga_double_mode_keeps_physical_spreads_alone(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    source = tmp_path / "spread-test"
    source.mkdir()

    portrait = source / "017.jpg"
    spread = source / "018-019.jpg"
    after = source / "020.jpg"
    Image.new("RGB", (600, 900), "white").save(portrait)
    Image.new("RGB", (1200, 900), "white").save(spread)
    Image.new("RGB", (600, 900), "white").save(after)

    book = service.import_images([portrait, spread, after], title="Spread Test Vol. 1")
    first = service.page(int(book["id"]), 0)
    middle = service.page(int(book["id"]), 1)
    last = service.page(int(book["id"]), 2)

    assert first["spread"] is False
    assert middle["spread"] is True
    assert "018-019" in middle["name"]
    assert last["spread"] is False

    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "if (settings.mode === 'double' && !isDoublePage(firstPage)" in manga
    assert "if (!isDoublePage(secondPage)) loaded.push(secondPage);" in manga
    assert "pagedVisibleCount = Math.max(1, loaded.length);" in manga
    assert "settings.mode === 'double' && !spreadFrame" in manga



def test_ln_volume_cards_hide_chapter_count_but_keep_other_facts() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "pudge-v0.7.23-ln-hide-volume-chapter-count-v1" in html
    assert "`${chapterCount} ${ui.lang==='ru'?'глав':'chapters'}`" not in html
    assert "anilistScoreChip(score,'percent')" in html


def test_manga_page_fetch_is_read_only_and_read_pages_are_monotonic(tmp_path: Path) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(manga_books)").fetchall()}
    assert "read_pages" in columns

    service = MangaService(db, cache_dir=tmp_path / "cache")
    path = tmp_path / "Progress Series Vol. 1.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(3):
            buffer = io.BytesIO()
            Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
            archive.writestr(f"{index + 1:03d}.jpg", buffer.getvalue())

    book = service.import_file(path)
    book_id = int(book["id"])
    assert service._payload(service._book(book_id))["read_pages"] == 0

    service.page(book_id, 1)
    fetched = service._payload(service._book(book_id))
    assert fetched["read_pages"] == 0
    assert fetched["position"] == 0

    service.set_position(book_id, 1)
    positioned = service._payload(service._book(book_id))
    assert positioned["position"] == 1
    assert positioned["read_pages"] == 0

    service.mark_read(book_id, 1)
    assert service._payload(service._book(book_id))["read_pages"] == 2
    service.mark_read(book_id, 0)
    assert service._payload(service._book(book_id))["read_pages"] == 2
    service.mark_read(book_id, 2)
    assert service._payload(service._book(book_id))["read_pages"] == 3


def test_manga_reader_marks_only_flipped_or_final_pages_read() -> None:
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    web = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    service = (ROOT / "pudge" / "manga.py").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-manga-explicit-read-progress-v1" in manga
    assert "def manga_set_position(self, book_id: int, page_index: int)" in web
    assert "def manga_mark_read(self, book_id: int, page_index: int)" in web
    assert "def mark_read(self, book_id: int, page_index: int)" in service
    page_start = service.index("    def page(self, book_id: int, page_index: int)")
    page_end = service.index("    def set_position(", page_start)
    assert "self.set_position(book_id, index)" not in service[page_start:page_end]
    assert "await markReadThrough(visibleEnd);" in manga
    assert "if (visibleEnd >= currentPageCount - 1) await markReadThrough(currentPageCount - 1);" in manga
    assert "if (best.index > currentPage) void markReadThrough(best.index - 1);" in manga


def test_primary_click_on_ln_and_manga_series_selects_all_volumes() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-series-primary-click-select-all-v1" in html
    assert "pudge-v0.7.23-series-primary-click-select-all-v1" in manga
    assert "const series=e.target.closest?.('[data-ln-series-key]'),volume=e.target.closest?.('[data-ln-book]')" in html
    assert "toggleLnSeriesSelection(group)" in html
    assert "const seriesCard = event.target.closest?.('[data-manga-series-key]');" in manga
    assert "const seriesVolume = event.target.closest?.('[data-manga-book]');" in manga
    assert "toggleSeriesSelection(group.books);" in manga


def test_manga_library_avoids_redundant_rebuilds_and_jiten_refocus_jank() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")

    assert "pudge-v0.7.23-manga-library-stability-v1" in manga
    assert "function mangaLibraryRenderSignature(books)" in manga
    assert "renderSignature === libraryRenderSignature" in manga
    assert "previousScrollTop" in manga
    assert "region.closest?.('[data-series-scroll]')" not in html
    assert "Async Jiten hydration must not repeatedly resize/refocus" in html

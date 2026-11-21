"""Code review fixes for the app backend (web_app.py) and the window pages.

What went wrong before, and what happens now:

1. active-playbacks.json survives a reboot. After one, its launcher PID could belong
   to any other process: it looked alive, the row was a day old, and the startup
   timeout sent SIGTERM to that unrelated process. Now each row records the boot it
   was written in; a row from another boot is forgotten without a signal, and before
   any signal the PID's command line must be Pudge's launcher (pudge.cli), otherwise
   the row is just forgotten (also for rows written by older versions).
2. Background recovery imported the audio still in a light novel's "Volume XX"
   folder and linked it again after the user unlinked the audiobook or deleted it
   from the library (and restarted transcription/alignment). Now unlink/delete is
   remembered per book and recovery skips that book; a new link or download by the
   user clears it.
3. Dropping an audiobook folder with cover.jpg also created a one-page manga. Now a
   folder with audio files is only an audiobook.
4. A speaker-markup import that failed on a later volume (missing volume, changed
   text) had already overwritten the earlier chapters' files. Now everything is
   checked first and nothing is written when the import fails.
5. The local asset server checked the Host header (DNS rebinding) only for manga
   pages: index.html (with the session token) and other API routes answered any
   Host. Now every GET (and HEAD) from another Host gets 403; the app's own window
   (127.0.0.1:port) still loads.
6. Enter in the global search opened the first row on screen, which could still be
   the previous query's (the search runs 180 ms after the last keystroke). Now Enter
   runs the search for the text in the box first.
7. A slow Nyaa audiobook search rendered its rows into whatever modal was open by
   then. Now the result belongs to one open of that modal and closing it forgets it.
8. The debug overlay showed a slower answer for the anime opened before ("Fresh
   subtitles" then acted on that anime's video). Now only the latest answer counts.
9. Error-message translation ran several lazy-wildcard regexes over any text; a few
   KB of near-matching text froze the window for a minute. Now texts over 500
   characters are shown as they are; shorter ones are translated as before.
10. A late light-novel bookmark reply was merged into the book open by then (and its
   library entry). Now it only updates the book it was saved for.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pudge.web_app import _start_asset_server
from test_web_app import make_api


# 1. Stale playback registry --------------------------------------------------------------


def _registry_row(api, process: subprocess.Popen, tmp_path: Path, **extra) -> str:
    key = str(tmp_path / "video.mkv")
    api._play_registry = {
        key: {
            "pid": process.pid,
            "started_at": time.time() - 86400,
            "ack_file": str(tmp_path / "missing-ack.json"),
            **extra,
        }
    }
    return key


def _still_running(process: subprocess.Popen) -> bool:
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        return True
    return False


@pytest.mark.parametrize("boot", ["other_boot", "no_marker"])
def test_stale_registry_row_never_signals_an_unrelated_process(tmp_path: Path, boot: str) -> None:
    api = make_api(tmp_path)
    victim = subprocess.Popen(["sleep", "60"])
    try:
        extra = {"boot": api._play_boot_marker() - 86400} if boot == "other_boot" else {}
        key = _registry_row(api, victim, tmp_path, **extra)
        with api._play_lock:
            state = api._restored_play_state_locked(key)
        assert state is None
        assert _still_running(victim)
        assert key not in api._play_registry
        assert key not in json.loads(api._play_registry_path.read_text(encoding="utf-8"))
    finally:
        victim.kill()
        victim.wait()


def test_restored_launcher_from_this_boot_still_times_out(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    launcher = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "pudge.cli", "--fast-play"]
    )
    try:
        time.sleep(0.2)
        key = _registry_row(api, launcher, tmp_path, boot=api._play_boot_marker())
        with api._play_lock:
            state = api._restored_play_state_locked(key)
        assert state["reason"] == "startup_timeout"
        assert launcher.wait(timeout=5) == -15
    finally:
        launcher.kill()
        launcher.wait()


# 2. Recovery re-linked an audiobook the user detached ------------------------------------


def _ln_with_audiobook(api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "book.txt"
    source.write_text("第一章\n架空の物語です。", encoding="utf-8")
    ln_id = int(api.light_novels.import_file(source)["id"])
    audio_path = tmp_path / "book.m4b"
    audio_path.write_bytes(b"audio")
    audio_id = int(
        api.audiobooks._upsert(
            path=audio_path,
            title="Book",
            duration=120.0,
            files=[
                {
                    "index": 0,
                    "path": str(audio_path),
                    "title": "Book",
                    "duration": 120.0,
                    "start": 0.0,
                    "end": 120.0,
                }
            ],
            chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 120.0}],
        )["id"]
    )
    monkeypatch.setattr(api.audiobooks, "prepare_transcription", lambda *a, **k: None)
    monkeypatch.setattr(api.audiobooks, "prepare_alignment", lambda *a, **k: None)
    api.audiobooks.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    # The book's managed "Volume 01" folder still holds the downloaded audio.
    destination = tmp_path / "Volume 01"
    destination.mkdir()
    (destination / "a.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(
        api.light_novels, "state", lambda *a, **k: {"books": [{"id": ln_id, "volume": 1, "title": "X"}]}
    )
    monkeypatch.setattr(api, "_light_novel_audiobook_download_destination", lambda book: destination)
    monkeypatch.setattr(api, "_recoverable_light_novel_audiobook_paths", lambda book, d: [d / "a.mp3"])
    monkeypatch.setattr(api, "_import_recovered_light_novel_audiobook", lambda book, d, p: {"id": 4242})
    relinked: list[tuple[int, int]] = []
    return ln_id, audio_id, relinked


def _record_relinks(api, monkeypatch: pytest.MonkeyPatch, relinked: list) -> None:
    monkeypatch.setattr(
        api.audiobooks,
        "link_light_novel",
        lambda ln, audio, **k: relinked.append((ln, audio)) or {"ok": True},
    )


@pytest.mark.parametrize("detach", ["unlink", "delete", "delete_many"])
def test_recovery_does_not_relink_an_audiobook_the_user_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detach: str
) -> None:
    api = make_api(tmp_path / "app")
    ln_id, audio_id, relinked = _ln_with_audiobook(api, tmp_path, monkeypatch)
    if detach == "unlink":
        api.light_novel_unlink_audiobook(ln_id)
    elif detach == "delete":
        api.audiobook_delete(audio_id)
    else:
        api.audiobook_delete_many([audio_id])
    assert api.audiobooks.link_for_light_novel(ln_id, include_alignment=False) is None
    _record_relinks(api, monkeypatch, relinked)

    result = api._recover_light_novel_audiobook_downloads()

    assert relinked == [] and result["recovered"] == 0


def test_a_new_link_by_the_user_lets_recovery_work_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(tmp_path / "app")
    ln_id, audio_id, relinked = _ln_with_audiobook(api, tmp_path, monkeypatch)
    api.light_novel_unlink_audiobook(ln_id)
    api.light_novel_link_audiobook(ln_id, audio_id)
    # The link disappears without the user (its audiobook row is gone).
    api.audiobooks.unlink_light_novel(ln_id)
    _record_relinks(api, monkeypatch, relinked)

    result = api._recover_light_novel_audiobook_downloads()

    assert relinked == [(ln_id, 4242)] and result["recovered"] == 1


# 3. Audiobook folder drop also made a manga -----------------------------------------------


def test_dropped_audiobook_folder_with_cover_is_not_also_a_manga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(tmp_path / "app")
    book = tmp_path / "drop" / "Book"
    book.mkdir(parents=True)
    (book / "01.mp3").write_bytes(b"mp3")
    (book / "cover.jpg").write_bytes(b"jpg")
    pages = tmp_path / "drop" / "Manga"
    pages.mkdir()
    (pages / "001.jpg").write_bytes(b"jpg")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        api.audiobooks,
        "import_folder",
        lambda path, **k: calls.append(("audiobook", path.name)) or {"id": 1, "title": "Book"},
    )
    monkeypatch.setattr(
        api.manga,
        "import_image_groups",
        lambda paths: calls.append(("manga", sorted(p.name for p in paths))) or [{"id": 9, "title": "Manga"}],
    )

    result = api.import_dropped_paths([str(book)])
    assert calls == [("audiobook", "Book")]
    assert [row["kind"] for row in result["imports"]] == ["audiobook"]

    calls.clear()
    api.import_dropped_paths([str(pages)])  # a folder of pages is still a manga
    assert calls == [("manga", ["001.jpg"])]


# 4. Speaker markup import that fails partway ----------------------------------------------


def _insert_volume(api, tmp_path: Path, *, volume: int, text: str) -> int:
    source_file = tmp_path / f"volume-{volume}.txt"
    source_file.write_text(text, encoding="utf-8")
    with api.light_novels._connect() as conn:
        cur = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"Series Vol. {volume}", str(source_file), "txt", volume, 55555, 1.0, 1.0),
        )
        book_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Chapter 1", text, f"hash-{volume}"),
        )
    return book_id


@pytest.mark.parametrize("failure", ["changed_text", "missing_volume"])
def test_failed_speaker_markup_import_writes_no_chapter_files(tmp_path: Path, failure: str) -> None:
    from pudge.light_novels import LightNovelError

    api = make_api(tmp_path / "app")
    first_text = "彼女は言った。「一巻よ」そして去った。"
    second_text = "彼女は戻った。「二巻よ」静かに笑った。"
    first = _insert_volume(api, tmp_path, volume=1, text=first_text)
    second = _insert_volume(api, tmp_path, volume=2, text=second_text)

    def chapter(text: str, digest: str) -> dict:
        unit = next(int(row["id"]) for row in api._audiobook_voice_units(text) if row["dialogue"])
        return {
            "chapter_index": 0,
            "source_sha256": digest,
            "assignments": [{"id": unit, "speaker": "綾乃", "caption": "若い女性の声"}],
        }

    second_row = {"book_id": second, "chapters": [chapter(second_text, "0" * 64)]}
    if failure == "missing_volume":
        second_row = {"book_id": 999999, "chapters": []}
    annotations = {
        "schema": 1,
        "kind": "pudge-speaker-annotations",
        "series_key": api.light_novels.book(first)["series_key"],
        "books": [
            {
                "book_id": first,
                "chapters": [chapter(first_text, api._speaker_markup_chapter_digest(first_text))],
            },
            second_row,
        ],
    }
    output = tmp_path / "pudge-speaker-annotations.json"
    output.write_text(json.dumps(annotations, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LightNovelError):
        api.import_light_novel_speaker_markup(first, str(output))
    book_dir = Path(api._audiobook_speaker_cache_context(first)["book_dir"])
    assert list(book_dir.glob("manual-chapter-*.json")) == []


# 5. Asset server Host check -------------------------------------------------------------


def test_asset_server_answers_only_its_own_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path)
    status_calls: list[int] = []
    monkeypatch.setattr(api, "torrent_traffic_status", lambda: status_calls.append(1) or {"enabled": False})
    server, base_url = _start_asset_server(api)
    port = server.server_address[1]
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        for method, path in [
            ("GET", "/index.html"),
            ("GET", "/"),
            ("GET", "/api/torrents/status"),
            ("GET", "/api/covers/manga/1/0123456789abcdef0123"),
            ("HEAD", "/index.html"),
        ]:
            connection.request(method, path, headers={"Host": f"rebind.example:{port}"})
            response = connection.getresponse()
            body = response.read()
            assert response.status == 403, (method, path)
            assert api._http_session_token.encode() not in body
        assert status_calls == []

        # The app's own window: base_url is http://127.0.0.1:<port>/k/<per-launch key>.
        key = api._http_page_key
        assert base_url == f"http://127.0.0.1:{port}/k/{key}"
        connection.request("GET", f"/k/{key}/index.html")
        response = connection.getresponse()
        assert response.status == 200 and b'id="plannedFilter"' in response.read()
        connection.request("GET", "/api/torrents/status", headers={"X-Pudge-Token": api._http_session_token})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"enabled": False}
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


# 6. Global search Enter -----------------------------------------------------------------


def test_enter_in_global_search_opens_the_result_of_the_typed_query(page) -> None:
    result = page.run(
        r"""
        openGlobalSearch();
        await __input('#globalSearchInput', 'frieren');
        await __tick(250); await __flush();
        const first = __text('#globalSearchResults');
        await __input('#globalSearchInput', 'bleach');   // Enter within 180 ms
        await __tick(50);
        await __key('Enter', {}, '#globalSearchInput');
        await __tick(50); await __flush();
        return {first};
        """,
        before=r"""
        __api.global_media_search = async (q) => q.startsWith('frieren')
          ? [{kind: 'episode', episode: 1, title: 'Frieren', video_path: '/v/frieren-01.mkv'}]
          : [{kind: 'episode', episode: 1, title: 'Bleach', video_path: '/v/bleach-01.mkv'}];
        __api.planning_search_anilist = () => new Promise(() => {});   // AniList never answers
        __api.play = async () => ({});
        """,
    )
    assert "Frieren" in result.value["first"]
    assert [call[0] for call in result.calls_of("play")] == ["/v/bleach-01.mkv"]
    assert result.calls_of("global_media_search") == [["frieren", 40], ["bleach", 40]]


# 7. Late Nyaa audiobook result ----------------------------------------------------------


def test_late_nyaa_audiobook_result_does_not_overwrite_another_modal(page) -> None:
    result = page.run(
        r"""
        await showLnAudiobookNyaa({id: 7, volume: 1, title: 'Book'});
        await __flush();
        closeModal();
        $('modalTitle').textContent = 'Other dialog';
        $('modalBody').innerHTML = '<div id="otherDialog">other</div>';
        $('modalBackdrop').classList.add('open');
        window.__resolveNyaa({matches: [{title: 'Audiobook torrent', target_volume: 1, seeders: 3}], target_volume: 1});
        await __flush(); await __tick(10);
        return {title: $('modalTitle').textContent, other: !!document.getElementById('otherDialog'),
                body: $('modalBody').textContent};
        """,
        before=r"""
        __api.light_novel_cached_audiobook_nyaa = async () => ({matches: []});
        __api.light_novel_search_audiobook_nyaa = () => new Promise(res => { window.__resolveNyaa = res; });
        """,
    )
    assert result.value == {"title": "Other dialog", "other": True, "body": "other"}


# 8. Debug overlay with a slower answer for the previous anime ------------------------------


def test_debug_overlay_ignores_the_previous_animes_late_answer(page) -> None:
    result = page.run(
        r"""
        const pA = window.PudgeDebug.open({media_id: 1, title: 'Anime A'});
        await __flush();
        window.PudgeDebug.close();
        const pB = window.PudgeDebug.open({media_id: 2, title: 'Anime B'});
        await __flush();
        window.__resolvers[2]({anime: {media_id: 2, title: 'Anime B'}, selected_episode: 5,
                               selected_local_episode: {video_path: '/b/ep5.mkv'}, available_episodes: []});
        await __flush();
        window.__resolvers[1]({anime: {media_id: 1, title: 'Anime A'}, selected_episode: 3,
                               selected_local_episode: {video_path: '/a/ep3.mkv'}, available_episodes: []});
        await __flush(); await pA; await pB;
        const body = document.getElementById('pudgeDebugBody').textContent;
        return {title: document.getElementById('pudgeDebugTitle').textContent,
                a: body.includes('/a/ep3.mkv'), b: body.includes('/b/ep5.mkv')};
        """,
        before=r"""
        window.__resolvers = {};
        __api.anime_debug_snapshot = (id) => new Promise(res => { window.__resolvers[id] = res; });
        """,
    )
    assert result.value["title"].endswith("Anime B")
    assert result.value["a"] is False and result.value["b"] is True


# 9. Error-message translation on long text ----------------------------------------------


def test_long_error_text_is_not_pattern_matched(page) -> None:
    result = page.run(
        r"""
        const exec = RegExp.prototype.exec;
        let longExecs = 0;
        RegExp.prototype.exec = function (text) {
          if (typeof text === 'string' && text.length > 500) longExecs++;
          return exec.call(this, text);
        };
        try {
          const plain = 'x'.repeat(600);
          const plainOut = PudgeI18n.message(plain);
          // Near-matches of a template with several wildcards (minutes of backtracking before).
          const crafted = 'Shortcut ' + ' is assigned to both  and  ('.repeat(160);
          const craftedOut = PudgeI18n.message(crafted);
          const template = PudgeI18n.t('error.ln.speakerMissingVolume', {bookId: 12}, 'en');
          return {plain: plainOut === plain, crafted: craftedOut === crafted, longExecs,
                  short: PudgeI18n.message(template, 'ru'), expected: PudgeI18n.t('error.ln.speakerMissingVolume', {bookId: 12}, 'ru')};
        } finally {
          RegExp.prototype.exec = exec;
        }
        """,
    )
    out = result.value
    assert out["plain"] is True and out["crafted"] is True and out["longExecs"] == 0
    assert out["short"] == out["expected"] and out["short"] != "error.ln.speakerMissingVolume"


# 10. Late bookmark reply ----------------------------------------------------------------


def test_late_bookmark_reply_updates_only_its_own_book(page) -> None:
    result = page.run(
        r"""
        ui.lnState = {settings: {}, books: [{id: 7, title: 'A', progress_percent: 10}, {id: 8, title: 'B', progress_percent: 20}]};
        ui.lnBook = {id: 7, title: 'A', progress_percent: 10};
        ui.lnChapter = {chapter_index: 0};
        const saving = saveLnBookmark('auto');
        await __flush();
        ui.lnBook = {id: 8, title: 'B', progress_percent: 20};   // the user opened another book
        window.__resolveBookmark({ok: true, bookmark: {chapter_index: 0, offset: 0.5}, progress_percent: 55});
        await __settle(saving);
        return {open: ui.lnBook, a: ui.lnState.books[0].progress_percent, b: ui.lnState.books[1].progress_percent};
        """,
        before=r"""
        __api.light_novel_bookmark = () => new Promise(res => { window.__resolveBookmark = res; });
        """,
    )
    assert result.value["open"] == {"id": 8, "title": "B", "progress_percent": 20}
    assert result.value["a"] == 55 and result.value["b"] == 20
    assert result.calls_of("light_novel_bookmark")[0][0] == 7

"""Audio analysis status, Jimaku/AniList credential steps, resumable STT chunks and
countdowns (behaviour; audit 71Б rewrite: page harness and AudiobookService instead of source text)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from tests.i18n_support import en, has_text, ru

# Light novel 6 with linked audiobook 75 whose audio analysis is in ALIGNMENT (not playing).
READER = """
const alignment = ALIGNMENT;
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, audiobook_id: 75, alignment_mode: 'stt', alignment, book: {id: 75, duration: 3600, playing: false, player_running: false}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫です。'], tokens: [[{start: 0, end: 1, surface: '猫'}]]});
__api.light_novel_paired_state = async () => ({linked: true, audiobook_id: 75, ln_book_id: 6, playing: false, player_running: false, paused: false,
  position: 0, duration: 3600, speed: 1, alignment});
"""
LN = {
    "state": {"settings": {"review_gate_ln_enabled": False}},
    "api": {"startup_maintenance": {"running": False}},
}


def reader_button_text(page, alignment: dict, lang: str) -> str:
    """The text of the reader's Listen button while the linked audiobook is being prepared."""
    result = page.run(
        "await __settle(openLightNovel(6)); await __tick(300); return __text('#lnPairedAudio');",
        lang=lang,
        before=READER.replace("ALIGNMENT", json.dumps(alignment)),
        **LN,
    )
    return result.value


def audiobook_cards(page, books: list[dict], lang: str) -> dict:
    """{book id: the audio analysis line of its card} on the Audiobooks page."""
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(300);
        return Object.fromEntries([...document.querySelectorAll('.audiobook-card[data-audiobook-id]')]
          .map(card => [card.dataset.audiobookId, card.querySelector('.audiobook-preparation')?.innerText ?? null]));
        """,
        lang=lang,
        api={"audiobook_state": {"books": books}},
    )
    return result.value


TRANSCRIBING = {
    "status": "transcribing",
    "ready": False,
    "progress_percent": 41.6,
    "remaining_audio_seconds": 7300,
    "elapsed_seconds": 650,
    "processed_audio_seconds": 5200,
    "total_duration": 12500,
}


def test_audio_analysis_is_a_compact_percentage_without_time_estimates(page) -> None:
    assert (en("ui.audioAnalysis"), ru("ui.audioAnalysis")) == ("Audio analysis", "Разбор аудио")
    assert not has_text("Background audio analysis") and not has_text("Фоновый разбор аудио")
    book = {
        "id": 4,
        "title": "Book",
        "duration": 12500,
        "position": 0,
        "speed": 1,
        "chapters": [],
        "transcription": TRANSCRIBING,
    }
    for lang in ("en", "ru"):
        label = en("ui.audioAnalysis") if lang == "en" else ru("ui.audioAnalysis")
        cards = audiobook_cards(page, [book], lang)
        # Card: the label and the percentage only — no remaining/elapsed time.
        assert cards["4"].split() == [*label.split(), "42%"]
        # Reader: the Listen button says the same in one line.
        assert reader_button_text(page, TRANSCRIBING, lang) == f"{label} · 42%"


def test_jimaku_account_link_and_automatic_anilist_step_are_current(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(500);
        const links = [...document.querySelectorAll('#settingsContent [data-url]')].map(b => b.dataset.url);
        const account = [...document.querySelectorAll('#settingsContent [data-url]')].find(b => /jimaku/.test(b.dataset.url) && b.innerText === t('uiB.jimaku.account'));
        account.click(); await __tick(10);
        return {links, steps: [...document.querySelectorAll('#settingsContent .anilist-step p')].map(p => p.innerText),
                step: t('ui.openAnilistAuthorizationAllowAccess')};
        """,
        api={"open_url": {"ok": True}},
    )
    out = result.value
    assert [url for url in out["links"] if "jimaku.cc" in url] == [
        "https://jimaku.cc/login",
        "https://jimaku.cc/account",
    ]
    assert result.calls_of("open_url") == [["https://jimaku.cc/account"]]
    assert out["step"] in out["steps"]
    assert "AniList data updates automatically" in en("ui.openAnilistAuthorizationAllowAccess")


def test_readme_points_to_the_same_jimaku_page_and_anilist_behaviour() -> None:
    # policy: setup instructions users follow by hand; there is nothing to run.
    from source_policy import source

    readme = source("README.md")
    assert "https://jimaku.cc/account" in readme and "https://jimaku.cc/profile" not in readme
    assert "updates AniList data automatically" in readme


# --- STT checkpoints -------------------------------------------------------------------------


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _long_book(service: AudiobookService, tmp_path: Path, name: str) -> int:
    source = tmp_path / f"{name}.m4b"
    source.write_bytes(name.encode())
    book = service._upsert(
        path=source,
        title=name,
        duration=650.0,
        files=[
            {"index": 0, "path": str(source), "title": name, "duration": 650.0, "start": 0.0, "end": 650.0}
        ],
        chapters=[{"index": 0, "title": name, "start": 0.0, "end": 650.0}],
    )
    return int(book["id"])


class _Worker:
    """The STT worker: answers each chunk request (or dies on the chunks in `crash_on`)."""

    def __init__(self, submitted: list[int], crash_on=()) -> None:
        self.process = SimpleNamespace(poll=lambda: None)
        self.submitted = submitted
        self.crash_on = set(crash_on)
        self.answers: list = []

    def submit(self, request):
        self.submitted.append(int(request["id"]))
        if request["id"] in self.crash_on:
            self.answers.append(None)
            return True
        Path(request["output"]).write_text(
            json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "x"}]}), encoding="utf-8"
        )
        self.answers.append({"id": request["id"], "ok": True})
        return True

    def answer(self, timeout):
        return self.answers.pop(0) if self.answers else False

    def stderr_tail(self):
        return "MLX crashed"

    def stop(self, timeout=15.0):
        pass

    def kill(self):
        pass


def _transcribe(service: AudiobookService, book_id: int, crash_on=()) -> list[int]:
    submitted: list[int] = []
    service._start_stt_worker = lambda environment, path: _Worker(submitted, crash_on)
    service._transcription_cancel_events[book_id] = threading.Event()
    service._transcribe_worker(book_id, service._transcript_path(book_id), threading.Event())
    return submitted


def test_interrupted_transcription_resumes_from_its_own_valid_chunks(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    first, second = _long_book(service, tmp_path, "first"), _long_book(service, tmp_path, "second")
    monkeypatch.setattr(
        service, "_extract_stt_chunk", lambda _s, destination, **_k: destination.write_bytes(b"chunk")
    )
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")

    # Chunk 3 fails twice: an error, but chunks 1 and 2 are kept for the next run.
    assert _transcribe(service, first, crash_on={3}) == [1, 2, 3, 3]
    assert service.transcription_status(first)["status"] == "error"
    assert not service._transcript_path(first).exists()
    # The next run recognizes only what is missing.
    assert _transcribe(service, first) == [3]
    assert service.transcription_status(first)["status"] == "ready"
    payload = json.loads(service._transcript_path(first).read_text(encoding="utf-8"))
    assert [row["start"] for row in payload["segments"]] == [1.0, 301.0, 601.0]
    # Another book (other audio, other fingerprint) never reuses these chunks.
    assert service._transcript_path(second) != service._transcript_path(first)
    assert _transcribe(service, second) == [1, 2, 3]

    # A damaged checkpoint is recognized again instead of being trusted.
    service._transcript_path(first).unlink()
    damaged = sorted(service._transcription_checkpoint_dir(service._transcript_path(first)).glob("*1.json"))
    assert damaged
    damaged[0].write_text("{not json", encoding="utf-8")
    assert _transcribe(service, first) == [1]


def test_failed_final_write_leaves_no_truncated_transcript(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    book_id = _long_book(service, tmp_path, "book")
    monkeypatch.setattr(
        service, "_extract_stt_chunk", lambda _s, destination, **_k: destination.write_bytes(b"chunk")
    )
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")
    output = service._transcript_path(book_id)
    original = Path.write_text

    def interrupted(self, data, *args, **kwargs):
        if self.parent == output.parent and self.name.startswith(output.stem):
            original(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError("disk full")
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupted)
    _transcribe(service, book_id)
    monkeypatch.setattr(Path, "write_text", original)
    assert service.transcription_status(book_id)["status"] == "error"
    assert not output.exists()


# --- countdowns and settings -----------------------------------------------------------------


def test_long_countdowns_show_days_only_and_settings_have_no_playback_tab(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(300);
        return {long: formatRemaining(2 * 86400 + 5 * 3600 + 7 * 60), hours: formatRemaining(5 * 3600 + 7 * 60), minutes: formatRemaining(7 * 60 + 5),
                expected: [`2${t('remaining.day')}`, `5${t('remaining.hour')} 7${t('remaining.minute')}`, `7${t('remaining.minute')}`],
                tabs: [...document.querySelectorAll('button[data-settings-category]')].map(b => b.innerText),
                categories: ['settingsUi.essential', 'nav.library', 'settingsUi.subtitles', 'ui.downloads', 'ui.advanced'].map(k => t(k)),
                playbackControl: Boolean(document.getElementById('s_playback_enabled'))};
        """,
    )
    out = result.value
    assert [out["long"], out["hours"], out["minutes"]] == out["expected"]
    assert out["tabs"] == out["categories"]
    assert out["playbackControl"] is False
    config = AppConfig()
    assert config.playback.enabled is True
    assert config.playback.rewind_seconds == 15.0

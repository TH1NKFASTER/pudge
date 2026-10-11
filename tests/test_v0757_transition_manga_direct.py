"""Read together: a live player is paused/resumed in place, but the first Listen of a reading
session starts at the reader's position; the backend tells a pause from a file transition.
Manga: bubbles are read top-down then right to left, and a word opens with one click
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from t71g21_support import READER, paired
from t71g23_support import paired_book
from tests.t71g06_support import column, reader, vertical
from tests.t71g07_support import CLICKS


def test_audiobook_pause_uses_explicit_mpv_property(tmp_path: Path, monkeypatch) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    process = SimpleNamespace(poll=lambda: None)
    socket_path = tmp_path / "mpv.sock"
    service._players[7] = process
    service._ipc_paths[7] = socket_path

    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: False if name == "pause" else None)
    assert service.is_paused(7) is False

    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: True if name == "pause" else None)
    assert service.is_paused(7) is True


def test_opening_the_reader_with_a_paused_player_offers_resume_in_place(page) -> None:
    # A paused player of this audiobook is running when the book is opened: the session is
    # already going, the button says Resume and resumes that player (no restart).
    # The first Listen of a new session restarts at the reader's position instead:
    # tests/test_v0749_ln_listen_resume_and_furigana_timing.py::test_first_listen_starts_where_the_reader_is_not_at_an_old_paused_player
    # (and Listen -> Pause -> Resume of a live player: ::test_button_says_listen_then_pause_and_resume_for_the_session).
    result = page.run(
        READER
        + r"""
        await openReader(); await __tick(1000);
        const labels = [__text('#lnPairedAudio')];
        await __click('#lnPairedAudio'); await __tick(1000);
        labels.push(__text('#lnPairedAudio'));
        return {labels, texts: [t('ui.resume'), t('ui.pause')]};
        """,
        **paired(extra="Object.assign(AUDIO, {running: true, position: 140});"),
    )
    assert result.value["labels"] == result.value["texts"]
    assert result.calls_of("audiobook_set_paused") == [[75, False]]
    assert not result.called("light_novel_play_paired") and not result.called("audiobook_stop")


def test_paired_state_tells_a_pause_from_a_file_transition(tmp_path: Path, monkeypatch) -> None:
    audio, ln_id, audio_id = paired_book(tmp_path, [("One", "猫が好きです。")], duration=60.0)
    audio._players[audio_id] = SimpleNamespace(poll=lambda: None)
    audio._ipc_paths[audio_id] = tmp_path / "mpv.sock"
    monkeypatch.setattr(audio, "_global_position", lambda *_args: None)
    mpv: dict[str, object] = {}
    monkeypatch.setattr(audio, "_ipc_get", lambda _path, name: mpv.get(name))

    # mpv switches to the next file of the audiobook: idle for a moment, not paused.
    mpv.update({"pause": False, "idle-active": True})
    state = audio.paired_state(ln_id)
    assert (state["player_running"], state["playing"], state["paused"], state["playback_active"]) == (
        True,
        True,
        False,
        False,
    )
    # The user paused.
    mpv.update({"pause": True, "idle-active": False})
    state = audio.paired_state(ln_id)
    assert (state["player_running"], state["playing"], state["paused"], state["playback_active"]) == (
        True,
        False,
        True,
        False,
    )
    # No player.
    audio._players.pop(audio_id)
    state = audio.paired_state(ln_id)
    assert (state["player_running"], state["playing"], state["paused"], state["playback_active"]) == (
        False,
        False,
        False,
        False,
    )


# Two rows (y from the bottom): top row A (right) and B (left, 3 % lower: the same row);
# bottom row C (right) and D (left, slightly higher).
ROWS = [
    vertical("たかい", 0.10, 0.62, 0.08, 0.25),  # B: top .13
    vertical("ひくい", 0.75, 0.05, 0.08, 0.25),  # C: top .70
    vertical("みぎうえ", 0.70, 0.65, 0.08, 0.25),  # A: top .10
    vertical("ひだりした", 0.15, 0.07, 0.08, 0.25),  # D: top .68
    vertical("まんなか", 0.45, 0.40, 0.08, 0.10),  # E: top .50, alone on its row
]


def test_manga_bubbles_are_read_top_down_then_right_to_left(page) -> None:
    result = reader(page, "return regions().map(r => r.text);", regions={0: ROWS})
    assert result.value == ["みぎうえ", "たかい", "まんなか", "ひくい", "ひだりした"]


def test_a_word_opens_with_one_click_without_activating_the_bubble(page) -> None:
    bubble = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)
    result = reader(
        page,
        CLICKS
        + r"""
        const before = regionNode(0).className;
        const first = await clickText(.725, .575);
        return {first, before, after: regionNode(0).className};
        """,
        regions={0: [bubble]},
        parse={"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]},
    )
    out = result.value
    # The first click opens the word's card; the bubble is not "activated" (no box appears).
    assert out["first"] == "好き" and out["after"] == out["before"]
    rules = css_rules("manga_reader_v2.css")
    assert not rules.declarations(".manga-v2-text-region:hover", media=None)
    assert not rules.declarations(".manga-v2-text-region.active", media=None)

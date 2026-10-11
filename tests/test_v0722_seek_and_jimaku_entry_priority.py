from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig
from pudge.filename import VideoIdentity
from pudge.models import AniListAnime, SubtitleCandidate
from pudge.providers.jimaku import JimakuClient, JimakuEntry, JimakuFile

# The LN reader listening together: three words of 10 characters; the audio is at `position`
# seconds and the backend's paired state says which character it reads. Arrow keys seek
# the audiobook; the window waits for the backend's new state (answered by the test).
LISTENING = r"""
const reader = document.getElementById('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
  + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('')
  + '</p>';
$('lnReaderShell').classList.add('open');
ui.lnBook = {id: 7, paired_audio: true};
ui.lnChapter = {chapter_index: 0};
ui.lnPairedExpanded = true;
const state = (offset, position) => ({audiobook_id: 3, alignment_mode: 'stt', alignment: {ready: true}, playing: true,
  paused: false, ln_chapter_index: 0, position, chapter_char_offset_exact: offset, speed: 1});
const playAt = (offset, position) => { ui.lnPairedState = state(offset, position); renderLnPairedPosition(state(offset, position), offset, {speechActive: true}); };
// "*" marks the highlighted word, then its filled part.
const words = () => [...reader.querySelectorAll('.ln-word')].map(word =>
  (word.classList.contains('ln-paired-word-current') ? '*' : '') + (word.style.getPropertyValue('--ln-paired-word-progress') || '-'));
const answers = [];
__api.audiobook_seek = async () => ({ok: true});
__api.light_novel_paired_state = () => new Promise(resolve => answers.push(resolve));
const answer = async (index, offset, position) => { answers[index](state(offset, position)); await __flush(); await __flush(); };
"""


def test_arrow_seek_forces_fresh_paint_and_drops_stale_responses(page) -> None:
    result = page.run(
        LISTENING
        + r"""
        const out = {};
        // Near the chapter start Left lands 0.3 s back: too small to look like a seek, still painted anew.
        playAt(1.5, 0.3);
        await __key('ArrowLeft'); await __flush();
        await answer(0, 0, 0);
        out.start = words();
        // From the second word back into the first: nothing of the second stays filled.
        playAt(15, 10.0); playAt(16, 10.2);
        await __key('ArrowLeft'); await __flush();
        await answer(1, 5, 5.2);
        out.back = words();
        // Two quick presses: the late answer to the first must not move the highlight back.
        await __key('ArrowRight'); await __flush();
        await __key('ArrowRight'); await __flush();
        await answer(3, 25, 15.2);
        out.second = words();
        await answer(2, 12, 10.2);
        out.late = words();
        return out;
        """
    )
    assert result.calls_of("audiobook_seek") == [[3, -5], [3, -5], [3, 5], [3, 5]]
    out = result.value
    assert out["start"] == ["*0.0%", "-", "-"]
    assert out["back"] == ["*50.0%", "-", "-"]
    assert out["second"] == out["late"] == ["-", "-", "*50.0%"]


def test_exact_anime_title_entry_is_not_hidden_behind_four_id_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = "token"
    video = tmp_path / "[SubsPlease] Hyakkano - 31 (1080p) [31BF37F3].mkv"
    video.write_bytes(b"video")
    title = "Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season"
    target_entry = JimakuEntry(
        id=12234,
        name=title,
        english_name="The 100 Girlfriends Who Really Love You Season 3",
        japanese_name="君のことが大大大大大好きな100人の彼女 第3期",
        anilist_id=None,
        flags={},
    )
    linked_duplicates = [
        JimakuEntry(
            id=12000 + index,
            name=f"Hyakkano mirror {index}",
            english_name=None,
            japanese_name=None,
            anilist_id=200637,
            flags={},
        )
        for index in range(4)
    ]
    target_file = JimakuFile(
        url="https://jimaku.cc/entry/12234/download/episode-07.srt",
        name=(
            "[shincaps] Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin "
            "no Kanojo 3rd Season - 07 (AT-X 1440x1080 MPEG2 AAC).srt"
        ),
        size=1000,
        last_modified="",
    )
    requested_entries: list[int] = []

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            if anilist_id is not None:
                return list(linked_duplicates)
            return [target_entry] if query else []

        def rank_entries(self, entries, identity, anilist_id):
            return JimakuClient.rank_entries(object.__new__(JimakuClient), entries, identity, anilist_id)

        def files_for_episode(self, entry_id, _episode, alternative_episodes=()):
            requested_entries.append(entry_id)
            return [target_file] if entry_id == target_entry.id else []

        def rank_files(self, files, identity, target_video, **kwargs):
            return JimakuClient.rank_files(
                object.__new__(JimakuClient), files, identity, target_video, **kwargs
            )

        def close(self):
            pass

    subtitle = tmp_path / "episode-07.srt"
    subtitle.write_text("日本語", encoding="utf-8")

    def fake_materialize(_client, item, *_args, **_kwargs):
        return [
            SubtitleCandidate(
                path=subtitle,
                source="jimaku",
                score=item.score,
                name=item.name,
                verified_japanese=True,
            )
        ]

    monkeypatch.setattr("pudge.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr("pudge.cli.materialize_jimaku_files", fake_materialize)
    monkeypatch.setattr("pudge.cli._jimaku_episode_aliases", lambda *_args: (31,))
    anime = AniListAnime(
        id=200637,
        titles=[title, "君のことが大大大大大好きな100人の彼女 第3期"],
        synonyms=["Hyakkano 3"],
        season_year=2026,
        episodes=12,
        format="TV",
    )

    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Hyakkano", episode=7),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )

    assert len(result) == 1
    assert result[0].name == target_file.name
    assert requested_entries[-1] == 12234
    assert result[0].score > cfg.matching.jimaku_min_score

"""WEBFLOW-10: a shared subtitle folder no longer offers other shows' episode 3 through
romaji particles, and names are scored before contents are read or archives unpacked.
WEBFLOW-15: the hidden-file filter looks only below the searched folder."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pudge import local_search
from pudge.local_search import _iter_files, find_local_subtitles
from pudge.models import VideoIdentity

LINE = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、元気ですか。今日はいい天気ですね。\n"
MIN_SCORE = 68.0


def _video(tmp_path: Path, title: str, episode: int) -> Path:
    folder = tmp_path / "library" / title
    folder.mkdir(parents=True)
    video = folder / f"[SubsPlease] {title} - {episode:02d} (1080p).mkv"
    video.write_bytes(b"x")
    return video


@pytest.fixture
def reads(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = local_search.is_japanese_subtitle

    def counting(path):
        seen.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(local_search, "is_japanese_subtitle", counting)
    return seen


def test_other_shows_with_the_same_episode_are_not_accepted(tmp_path: Path) -> None:
    video = _video(tmp_path, "Kimetsu no Yaiba", 3)
    shared = tmp_path / "kitsunekko"
    shared.mkdir()
    for title in ("Kimi no Na wa", "Boku no Hero Academia", "Kusuriya no Hitorigoto", "Shingeki no Kyojin"):
        (shared / f"{title} - 03.ja.srt").write_text(LINE, encoding="utf-8")
    (shared / "Kimetsu no Yaiba - 03.ja.srt").write_text(LINE, encoding="utf-8")
    found = find_local_subtitles(
        video, VideoIdentity(title="Kimetsu no Yaiba", episode=3), [shared], tmp_path / "cache", 1000
    )
    accepted = [candidate.name for candidate in found if candidate.score >= MIN_SCORE]
    assert accepted == ["Kimetsu no Yaiba - 03.ja.srt"]


def test_related_names_outside_the_folder_still_match(tmp_path: Path) -> None:
    video = _video(tmp_path, "Sousou no Frieren", 5)
    shared = tmp_path / "subs"
    shared.mkdir()
    (shared / "Frieren - 05.ja.srt").write_text(LINE, encoding="utf-8")
    (shared / "Sousou no Frieren - 05.ja.ass").write_text(
        "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは、元気ですか。今日はいい天気ですね。\n",
        encoding="utf-8",
    )
    found = find_local_subtitles(
        video, VideoIdentity(title="Sousou no Frieren", episode=5), [shared], tmp_path / "cache", 1000
    )
    assert {candidate.name for candidate in found if candidate.score >= MIN_SCORE} == {
        "Frieren - 05.ja.srt",
        "Sousou no Frieren - 05.ja.ass",
    }


def test_names_are_scored_before_contents_are_read(tmp_path: Path, reads) -> None:
    video = _video(tmp_path, "Target Show", 3)
    shared = tmp_path / "kitsunekko"
    for show in range(30):
        folder = shared / f"Show Number {show}"
        folder.mkdir(parents=True)
        for episode in range(1, 11):
            (folder / f"Show Number {show} - {episode:02d}.ja.srt").write_text(LINE, encoding="utf-8")
    (shared / "Target Show - 03.ja.srt").write_text(LINE, encoding="utf-8")
    (shared / "Target Show - 04.ja.srt").write_text(LINE, encoding="utf-8")
    identity = VideoIdentity(title="Target Show", episode=3)
    found = find_local_subtitles(video, identity, [shared], tmp_path / "cache", 8000, min_score=MIN_SCORE)
    assert [candidate.name for candidate in found if candidate.score >= MIN_SCORE] == [
        "Target Show - 03.ja.srt"
    ]
    assert reads == ["Target Show - 03.ja.srt"]  # before: all 302 files were read
    reads.clear()
    unfiltered = find_local_subtitles(video, identity, [shared], tmp_path / "cache-2", 8000)
    assert [c.name for c in unfiltered if c.score >= MIN_SCORE] == ["Target Show - 03.ja.srt"]
    assert len(reads) == 302  # without a threshold the old behaviour stays


def test_archive_members_that_cannot_pass_are_not_unpacked(tmp_path: Path) -> None:
    video = _video(tmp_path, "Target Show", 3)
    shared = tmp_path / "packs"
    shared.mkdir()
    for pack in range(5):
        with zipfile.ZipFile(shared / f"pack{pack}.zip", "w") as archive:
            for episode in range(1, 13):
                archive.writestr(f"Pack {pack} - {episode:02d}.srt", LINE)
    with zipfile.ZipFile(shared / "target.zip", "w") as archive:
        archive.writestr("Target Show - 03.ja.srt", LINE)
        archive.writestr("Target Show - 04.ja.srt", LINE)
    cache = tmp_path / "cache"
    found = find_local_subtitles(
        video, VideoIdentity(title="Target Show", episode=3), [shared], cache, 8000, min_score=MIN_SCORE
    )
    assert [c.name for c in found if c.score >= MIN_SCORE] == ["Target Show - 03.ja.srt"]
    unpacked = sorted(path.name for path in cache.rglob("*") if path.is_file())
    assert unpacked == ["Target Show - 03.ja.srt"]  # before: 8 members of every zip


def test_a_folder_below_a_dot_folder_is_searched(tmp_path: Path) -> None:
    root = tmp_path / ".config" / "subs"
    (root / "Show").mkdir(parents=True)
    wanted = root / "Show" / "Show - 01.ja.srt"
    wanted.write_text(LINE, encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "Show - 01.ja.srt").write_text(LINE, encoding="utf-8")
    (root / "Show" / "._Show - 01.ja.srt").write_text("junk", encoding="utf-8")
    assert list(_iter_files([root], 100)) == [wanted]

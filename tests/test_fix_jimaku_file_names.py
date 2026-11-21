"""Reading Jimaku file names, and keeping downloaded files apart.

- ``Title 05.ja.srt`` had no episode: every such file fitted every episode, so one episode
  downloaded and aligned the whole entry.
- ``Title 2nd Season 05.srt`` read as season 5 and the right file was rejected outright.
- ``Kaiju No. 8 - 02`` read as the episode range 2-8 (start and end swapped; the 8 is the
  title), and a date such as ``[2023-10-06]`` as the range 6-10.
- A file replaced on Jimaku under the same name was never downloaded again, and the
  members of a replaced zip were never unpacked again.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import httpx

from pudge.filename import parse_anime_filename
from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import (
    JimakuClient,
    _extract_archive_all,
    explicit_episode_range,
    explicit_season_hint,
)

SRT = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは、元気ですか？\n\n" * 20


def test_a_language_tag_before_the_extension_does_not_hide_the_episode() -> None:
    for name, title in (
        ("Sousou no Frieren 05.ja.srt", "Sousou no Frieren"),
        ("[Group] Sousou no Frieren 05 [1080p].ja.srt", "Sousou no Frieren"),
        ("Bocchi the Rock! 05.jpn.ass", "Bocchi the Rock!"),
        ("Sousou no Frieren 05.Japanese.srt", "Sousou no Frieren"),
        ("Sousou no Frieren - 05.ja.srt", "Sousou no Frieren"),
    ):
        parsed = parse_anime_filename(name)
        assert (parsed.title, parsed.episode) == (title, 5), name
    assert parse_anime_filename("Show.S02E05.jpn.srt").episode == 5
    # Not a subtitle: nothing is taken off.
    assert parse_anime_filename("Ninja.ja.mkv").title == "Ninja ja"


class _Entry:
    """Unbound JimakuClient methods with canned file lists."""

    def __init__(self, unfiltered: list[JimakuFile], filtered: dict[int, list[JimakuFile]]):
        self.unfiltered = unfiltered
        self.filtered = filtered

    def files(self, _entry_id: int, episode: int | None) -> list[JimakuFile]:
        return list(self.unfiltered if episode is None else self.filtered.get(int(episode), []))


def _file(name: str) -> JimakuFile:
    return JimakuFile(url=f"https://jimaku.cc/entry/1/download/{name}", name=name, size=10, last_modified="")


def test_one_episode_gets_its_file_not_the_whole_entry() -> None:
    files = [_file(f"[Group] Sousou no Frieren {number:02d} [1080p].ja.srt") for number in range(1, 29)]
    entry = _Entry(files, {5: [files[4]]})
    assert [item.name for item in JimakuClient._files_for_episode_unchecked(entry, 1, 5)] == [files[4].name]


def test_names_without_an_episode_are_only_a_last_resort() -> None:
    hidden = [_file("Sousou no Frieren [WEB] (A).srt"), _file("Sousou no Frieren [WEB] (B).srt")]
    numbered = _file("Sousou no Frieren - 05.srt")
    entry = _Entry([*hidden, numbered], {5: []})
    assert [item.name for item in JimakuClient._files_for_episode_unchecked(entry, 1, 5)] == [numbered.name]
    # Jimaku's own episode index knows a file whose name hides it: that one, not all of them.
    entry = _Entry(hidden, {5: [hidden[1]]})
    assert [item.name for item in JimakuClient._files_for_episode_unchecked(entry, 1, 5)] == [hidden[1].name]
    # Nothing names episode 5 and the index does not know it: the unnamed files are tried.
    entry = _Entry(hidden, {5: []})
    assert len(JimakuClient._files_for_episode_unchecked(entry, 1, 5)) == 2


def test_nth_season_is_the_season_and_the_number_after_it_the_episode() -> None:
    name = "Kaguya-sama wa Kokurasetai 2nd Season 05.srt"
    parsed = parse_anime_filename(name)
    assert (parsed.season, parsed.episode) == (2, 5)
    assert explicit_season_hint(name, parsed.title) == 2
    assert explicit_season_hint("Kaguya-sama wa Kokurasetai 2nd Season - 05.srt") == 2
    # Two seasons in one name: unknown, never a hard reject.
    assert explicit_season_hint("Show S01 2nd Season 05.srt") is None
    client = JimakuClient("https://jimaku.cc", "key")
    try:
        [ranked] = client.rank_files(
            [_file(name)],
            VideoIdentity(title="Kaguya-sama wa Kokurasetai", episode=5, season=2),
            Path("[SubsPlease] Kaguya-sama wa Kokurasetai S2 - 05 (1080p).mkv"),
        )
    finally:
        client.close()
    assert ranked.details["season_match"] == "exact" and ranked.score > 45


def test_episode_ranges_are_read_forwards_after_the_title_and_never_from_dates() -> None:
    for name in (
        "Kaiju No. 8 - 02.srt",
        "[Group] Kaiju No. 8 - 02 [1080p].srt",
        "Mob Psycho 100 - 03.srt",
        "Steins;Gate 0 - 05.srt",
        "Kaguya-sama wa Kokurasetai 2 - 05.srt",
        "Title - 05 (1080p) [2023-10-06].srt",
        "Title - 05 [06.10.2023].srt",
        "Title 12-01.srt",
    ):
        assert explicit_episode_range(name) is None, name
    assert explicit_episode_range("Title 1-12 [BD].zip") == (1, 12)
    assert explicit_episode_range("Title - 05 - 06.srt") == (5, 6)
    assert explicit_episode_range("Kaiju No. 8 - 05 - 06.srt") == (5, 6)
    assert explicit_episode_range("Show S01E01-E12.zip") == (1, 12)
    assert explicit_episode_range("[Grp] Show (01-12) [BD].zip") == (1, 12)


def test_other_episodes_of_a_title_ending_in_a_number_are_not_candidates() -> None:
    files = [_file(f"Kaiju No. 8 - {number:02d}.srt") for number in range(1, 7)]
    client = JimakuClient("https://jimaku.cc", "key")
    try:
        ranked = client.rank_files(
            files, VideoIdentity(title="Kaiju No. 8", episode=5), Path("Kaiju No. 8 - 05.mkv")
        )
    finally:
        client.close()
    assert [item.name for item in ranked if item.score >= 45] == ["Kaiju No. 8 - 05.srt"]
    assert {item.details["episode_match"] for item in ranked if item.name != "Kaiju No. 8 - 05.srt"} == {
        "mismatch"
    }


def test_a_replaced_file_is_downloaded_again(tmp_path: Path) -> None:
    bodies = iter([SRT.encode(), (SRT + "fixed\n").encode()])
    client = JimakuClient("https://jimaku.cc", "key")
    client.client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=next(bodies)))
    )
    try:
        url = "https://jimaku.cc/entry/1/download/Show - 05.srt"
        first = client.download(
            JimakuFile(url=url, name="Show - 05.srt", size=10, last_modified="2024-01-01T00:00:00Z"), tmp_path
        )
        again = client.download(
            JimakuFile(url=url, name="Show - 05.srt", size=10, last_modified="2024-01-01T00:00:00Z"), tmp_path
        )
        replaced = client.download(
            JimakuFile(url=url, name="Show - 05.srt", size=10, last_modified="2024-02-01T00:00:00Z"), tmp_path
        )
    finally:
        client.close()
    assert first == again and first.read_text(encoding="utf-8") == SRT
    assert replaced != first and replaced.read_text(encoding="utf-8").endswith("fixed\n")


def test_a_replaced_zip_is_unpacked_again(tmp_path: Path) -> None:
    archive = tmp_path / "Show.zip"
    out = tmp_path / "out"
    out.mkdir()
    identity = VideoIdentity(title="Show", episode=5)
    video = Path("Show - 05.mkv")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Show - 05.ja.srt", SRT)
    [(first, _name, _score)] = _extract_archive_all(archive, identity, video, out, True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Show - 05.ja.srt", SRT + "fixed\n")
    stat = archive.stat()
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    [(second, _name, _score)] = _extract_archive_all(archive, identity, video, out, True)
    assert second != first and second.read_text(encoding="utf-8").endswith("fixed\n")

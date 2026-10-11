from __future__ import annotations

from pudge.providers.jimaku import JimakuClient, JimakuFile


def _file(name: str) -> JimakuFile:
    return JimakuFile(url=f"https://jimaku.test/{name}", name=name, size=1, last_modified="")


class Client:
    def __init__(self, entry_files):
        self.entry_files = entry_files
        self.calls = []

    def files(self, entry_id, episode):
        self.calls.append(episode)
        from pudge.providers.jimaku import parse_anime_filename

        if episode is None:
            return list(self.entry_files)
        return [item for item in self.entry_files if parse_anime_filename(item.name).episode == episode]


# Seihantai na Kimi to Boku 2nd Season (13 eps, prequel 12): Jimaku entry 12212
# numbers the sequel 13.. so "S02E13"/"- 13" is the sequel's FIRST episode.
ABSOLUTE_ENTRY = [
    _file("正反対な君と僕.S02E13.クリスマスイヴ.WEBRip.DMMTV.ja[cc].srt"),
    _file("正反対な君と僕.S02E13.クリスマスイヴ.WEBRip.Netflix.ja[cc].srt"),
    _file(
        "[Nekomoe kissaten&LoliHouse] Seihantai na Kimi to Boku - 13 [WebRip 1080p HEVC-10bit AAC ASSx2][JPN].ass"
    ),
    _file("[SweetSub] Seihantai na Kimi to Boku - 13 [WebRip 1080p HEVC-10bit AAC][JPN].ass"),
    _file("正反対な君と僕.S02E14.WEBRip.Netflix.ja[cc].srt"),
    _file("正反対な君と僕.S02E24.WEBRip.Netflix.ja[cc].srt"),
]


def test_sequel_finale_rejects_absolute_numbered_first_episode():
    client = Client(ABSOLUTE_ENTRY)
    assert JimakuClient.files_for_episode(client, 12212, 13, alternative_episodes=(25,)) == []  # type: ignore[arg-type]


def test_sequel_finale_keeps_the_real_absolute_file():
    finale = _file("正反対な君と僕.S02E25.WEBRip.Netflix.ja[cc].srt")
    client = Client([*ABSOLUTE_ENTRY, finale])
    result = JimakuClient.files_for_episode(client, 12212, 13, alternative_episodes=(25,))  # type: ignore[arg-type]
    assert result == [finale]


def test_relative_entry_keeps_episode_13():
    relative = [_file(f"正反対な君と僕.S02E{n:02d}.WEBRip.Netflix.ja[cc].srt") for n in range(1, 14)]
    client = Client(relative)
    result = JimakuClient.files_for_episode(client, 12212, 13, alternative_episodes=(25,))  # type: ignore[arg-type]
    assert [item.name for item in result] == ["正反対な君と僕.S02E13.WEBRip.Netflix.ja[cc].srt"]


def test_lone_ambiguous_file_is_not_treated_as_absolute():
    lone = [_file("正反対な君と僕.S02E13.WEBRip.Netflix.ja[cc].srt")]
    client = Client(lone)
    assert JimakuClient.files_for_episode(client, 12212, 13, alternative_episodes=(25,)) == lone  # type: ignore[arg-type]


def test_episode_inside_prequel_range_never_collides():
    # Requested relative 5 (absolute 17): "5" cannot be an absolute sequel number.
    client = Client([_file("Show S02E05.srt"), _file("Show S02E17.srt"), _file("Show S02E18.srt")])
    result = JimakuClient.files_for_episode(client, 1, 5, alternative_episodes=(17,))  # type: ignore[arg-type]
    assert {item.name for item in result} == {"Show S02E05.srt", "Show S02E17.srt"}
    # One full-list request (audit 21B); the collision check adds none.
    assert client.calls.count(None) == 1

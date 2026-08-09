from __future__ import annotations

import zipfile
from pathlib import Path

from anime_mpv.models import JimakuFile, VideoIdentity
from anime_mpv.providers.jimaku import materialize_jimaku_files


class FakeJimakuClient:
    def __init__(self, archive: Path):
        self.archive = archive

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        return self.archive


def _srt(text: str) -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def test_zip_exposes_all_japanese_variants_for_episode(tmp_path: Path):
    archive = tmp_path / "subs.zip"
    japanese = "これは日本語の字幕です。今日はとてもいい天気ですね。" * 4
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Anime - 04 [A].srt", _srt(japanese))
        zf.writestr("Anime - 04 [B].srt", _srt(japanese + "別の版です。"))
        zf.writestr("Anime - 05.srt", _srt(japanese))
        zf.writestr("Anime - 04 [EN].srt", _srt("English subtitle line" * 10))

    item = JimakuFile(
        url="https://example.test/subs.zip",
        name="subs.zip",
        size=1,
        last_modified="",
        score=50.0,
    )
    candidates = materialize_jimaku_files(
        FakeJimakuClient(archive),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Anime", episode=4),
        tmp_path / "Anime - 04.mkv",
        tmp_path / "cache",
    )

    assert len(candidates) == 2
    assert {candidate.name for candidate in candidates} == {
        "Anime - 04 [A].srt",
        "Anime - 04 [B].srt",
    }


def test_movie_archive_file_gets_confidence_without_episode_number(tmp_path: Path):
    from anime_mpv.providers.jimaku import JimakuClient

    item = JimakuFile(
        url="https://example.test/movie.sup.7z",
        name=(
            "Demon.Slayer.Kimetsu.no.Yaiba.Infinity.Castle.2025."
            "1080p.BDRip.AAC5.1.10bits.x265-Rapta.sup.7z"
        ),
        size=1,
        last_modified="",
    )
    client = object.__new__(JimakuClient)
    ranked = JimakuClient.rank_files(
        client,
        [item],
        VideoIdentity(
            title="Demon Slayer: Kimetsu no Yaiba - Infinity Castle",
            year=2025,
        ),
        tmp_path
        / "Demon.Slayer.Kimetsu.no.Yaiba.Infinity.Castle.2025.1080p.CR.WEB-DL.mkv",
    )

    assert ranked[0].score >= 45


def test_7z_archive_exposes_sup_subtitle(tmp_path: Path, monkeypatch):
    archive = tmp_path / "movie.sup.7z"
    archive.write_bytes(b"fake archive")
    payload = tmp_path / "payload.sup"
    payload.write_bytes(b"PG subtitle payload")

    tool = tmp_path / "7zz"
    tool.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in -o*) out=${arg#-o} ;; esac\n"
        "done\n"
        "mkdir -p \"$out\"\n"
        "cp \"$FAKE_7Z_PAYLOAD\" \"$out/Infinity Castle Japanese.sup\"\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_7Z_PAYLOAD", str(payload))
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ.get('PATH', '')}")

    item = JimakuFile(
        url="https://example.test/movie.sup.7z",
        name="Demon Slayer Infinity Castle Japanese.sup.7z",
        size=1,
        last_modified="",
        score=70.0,
    )
    candidates = materialize_jimaku_files(
        FakeJimakuClient(archive),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Demon Slayer Infinity Castle"),
        tmp_path / "Demon Slayer Infinity Castle.mkv",
        tmp_path / "cache",
    )

    assert len(candidates) == 1
    assert candidates[0].path.suffix == ".sup"
    assert candidates[0].verified_japanese is True


def test_jimaku_exact_episode_uploaded_before_airing_gets_soft_penalty(tmp_path: Path):
    from anime_mpv.providers.jimaku import JimakuClient

    item = JimakuFile(
        url="https://example.test/episode.srt",
        name="Seihantai na Kimi to Boku 2nd Season - 05.srt",
        size=1,
        last_modified="2026-07-01T12:00:00Z",
    )
    client = object.__new__(JimakuClient)
    expected_airing_at = 1785600000  # 2026-08-01 UTC-ish; exact date is not important here.
    ranked = JimakuClient.rank_files(
        client,
        [item],
        VideoIdentity(title="Seihantai na Kimi to Boku 2nd Season", episode=5),
        tmp_path / "Seihantai na Kimi to Boku 2nd Season - 05.mkv",
        expected_airing_at=expected_airing_at,
    )

    assert ranked[0].details["airing_sanity"] == "before_airing_exact_episode"
    assert ranked[0].score >= 45


def test_files_for_episode_falls_back_to_unfiltered_entry_files():
    from anime_mpv.providers.jimaku import JimakuClient

    target = JimakuFile(
        url="https://example.test/carameliser-05.srt",
        name="[shincaps] Otome Kaijuu Carameliser - 05 (AT-X 1440x1080 MPEG2 AAC).srt",
        size=123,
        last_modified="",
    )

    class Client:
        def __init__(self):
            self.calls: list[int | None] = []

        def files(self, entry_id: int, episode: int | None):
            assert entry_id == 42
            self.calls.append(episode)
            return [] if episode == 5 else [target]

    client = Client()
    result = JimakuClient.files_for_episode(client, 42, 5)  # type: ignore[arg-type]

    assert result == [target]
    assert client.calls == [5, None]


def test_carameliser_atx_exact_episode_survives_airing_date_penalty(tmp_path: Path):
    from anime_mpv.providers.jimaku import JimakuClient

    item = JimakuFile(
        url="https://example.test/carameliser-05.srt",
        name="[shincaps] Otome Kaijuu Carameliser - 05 (AT-X 1440x1080 MPEG2 AAC).srt",
        size=123,
        last_modified="2026-07-20T12:00:00Z",
    )
    client = object.__new__(JimakuClient)
    ranked = JimakuClient.rank_files(
        client,
        [item],
        VideoIdentity(title="Otome Kaijuu Caraméliser", episode=5),
        tmp_path / "[Erai-raws] Otome Kaijuu Carameliser - 05 [1080p CR WEB-DL AVC AAC][MultiSub].mkv",
        expected_airing_at=1785600000,
    )

    assert ranked[0].details["episode_match"] == "exact"
    assert ranked[0].details["airing_sanity"] == "before_airing_exact_episode"
    assert ranked[0].score >= 45


def test_jimaku_rank_exposes_diagnostic_score_components(tmp_path: Path):
    from anime_mpv.providers.jimaku import JimakuClient

    item = JimakuFile(
        url="https://example.test/carameliser-05.srt",
        name="[shincaps] Otome Kaijuu Carameliser - 05 (AT-X 1440x1080 MPEG2 AAC).srt",
        size=123,
        last_modified="",
    )
    client = object.__new__(JimakuClient)
    ranked = JimakuClient.rank_files(
        client,
        [item],
        VideoIdentity(title="Otome Kaijuu Caraméliser", episode=5),
        tmp_path / "[Erai-raws] Otome Kaijuu Carameliser - 05 [1080p].mkv",
    )

    details = ranked[0].details
    assert details["parsed_episode"] == 5
    assert details["episode_match"] == "exact"
    assert details["title_similarity"] > 90
    assert "format_bonus" in details
    assert "release_token_overlap" in details



def test_files_for_episode_rejects_explicit_range_outside_requested_episode():
    from anime_mpv.providers.jimaku import JimakuClient

    wrong_pack = JimakuFile(
        url="https://example.test/mushoku-01-02.srt",
        name=(
            "[shincaps] Mushoku Tensei III ~Isekai Ittara Honki Dasu~ "
            "- 01-02 (AT-X 1440x1080 MPEG2 AAC).srt"
        ),
        size=123,
        last_modified="",
    )

    class Client:
        def files(self, entry_id: int, episode: int | None):
            assert entry_id == 12216
            return [] if episode == 6 else [wrong_pack]

    result = JimakuClient.files_for_episode(Client(), 12216, 6)  # type: ignore[arg-type]

    assert result == []


def test_jimaku_rank_hard_rejects_explicit_range_outside_episode(tmp_path: Path):
    from anime_mpv.providers.jimaku import JimakuClient

    wrong_pack = JimakuFile(
        url="https://example.test/mushoku-01-02.srt",
        name=(
            "[shincaps] Mushoku Tensei III ~Isekai Ittara Honki Dasu~ "
            "- 01-02 (AT-X 1440x1080 MPEG2 AAC).srt"
        ),
        size=123,
        last_modified="",
    )
    client = object.__new__(JimakuClient)
    ranked = JimakuClient.rank_files(
        client,
        [wrong_pack],
        VideoIdentity(
            title="Mushoku Tensei III: Isekai Ittara Honki Dasu",
            episode=6,
        ),
        tmp_path / "[Erai-raws] Mushoku Tensei III - 06 [1080p].mkv",
    )

    assert ranked[0].details["explicit_episode_range"] == [1, 2]
    assert ranked[0].details["episode_match"] == "range_mismatch"
    assert ranked[0].details["hard_reject_reason"] == "episode_outside_explicit_range"
    assert ranked[0].score < 45


def test_files_for_episode_expands_sparse_server_results_to_all_entry_variants():
    from anime_mpv.providers.jimaku import JimakuClient

    netflix = JimakuFile(
        url="https://example.test/netflix-02.srt",
        name="Odd.Taxi.S01E02.WEBRip.Netflix.ja[cc].srt",
        size=1,
        last_modified="",
    )
    amazon = JimakuFile(
        url="https://example.test/amazon-02.srt",
        name="オッドタクシー.S01E02.長い夜の過ごし方.WEBRip.Amazon.ja-jp[sdh].srt",
        size=1,
        last_modified="",
    )
    bluray = JimakuFile(
        url="https://example.test/bd-02.ass",
        name="[Nekomoe kissaten] ODDTAXI [02][BDRip].JPSC.ass",
        size=1,
        last_modified="",
    )

    class Client:
        def __init__(self):
            self.calls: list[int | None] = []

        def files(self, entry_id: int, episode: int | None):
            assert entry_id == 308
            self.calls.append(episode)
            return [netflix] if episode == 2 else [netflix, amazon, bluray]

    client = Client()
    result = JimakuClient.files_for_episode(client, 308, 2)  # type: ignore[arg-type]

    assert {item.url for item in result} == {netflix.url, amazon.url, bluray.url}
    assert client.calls == [2, None]

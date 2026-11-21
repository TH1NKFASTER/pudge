"""Two copies of one episode: Kusuriya no Hitorigoto 3rd Season #3.

User: "for some reason I have two episodes of The Apothecary Diaries". The race had taken
"[AnoZu] The Apothecary Diaries S03E03 1080p CR WEB-DL" (Erai-raws had not started); 12 minutes
later the upgrade check scheduled "[Erai-raws] ... - 03 [1080p CR WEB-DL]" for a score gain of
47 (trusted group, MultiSub tag, 4040 seeders) - the same Crunchyroll stream, 1434 MiB both. And
as Jimaku had no subtitles for the episode yet, the old copy was "kept until the upgrade has
Japanese subtitles": both stayed.
"""

from __future__ import annotations

from pathlib import Path

from pudge.providers.nyaa import same_web_encode
from test_fix_upgrade_and_watched_import import _new_copy, _release, _upgrade_manager

MIB = 1024 * 1024
ANOZU = "The.Apothecary.Diaries.2023.S03E03.1080p.CR.WEB-DL.AAC2.0.H.264-AnoZu.mkv"
ERAI = "[Erai-raws] Kusuriya no Hitorigoto 3rd Season - 03 [1080p CR WEB-DL AVC AAC][MultiSub][A08588DE]"


def test_same_service_web_dl_of_the_same_size_is_the_same_stream() -> None:
    assert same_web_encode(ANOZU, 1434 * MIB, ERAI, 1434 * MIB + 300_000)
    assert not same_web_encode(ANOZU, 1434 * MIB, ERAI.replace("CR", "NF"), 1434 * MIB)  # another service
    assert not same_web_encode(ANOZU, 1434 * MIB, ERAI, 2100 * MIB)  # another encode
    assert not same_web_encode("[Grp] Show - 03 [1080p WEBRip x265]", 700 * MIB, ERAI, 700 * MIB)
    assert not same_web_encode(ANOZU, 0, ERAI, 1434 * MIB)


def test_no_upgrade_to_the_same_stream(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    current = _release("oldhash", 80.0)
    current.title = "[AnoZu] Upgrade Show S01E05 1080p CR WEB-DL AAC 2.0 H.264"
    current.size_bytes = 1434 * MIB
    better = _release("newhash", 200.0)
    better.title = "[Erai-raws] Upgrade Show - 05 [1080p CR WEB-DL AVC AAC][MultiSub]"
    better.size_bytes = 1434 * MIB + 250_000
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [better, current])
    assert manager.auto_upgrade_downloaded() == 0
    assert client.added == [] and not manager.db.has_pending_upgrade(700, 5)

    # Another encode (a BD, a bigger file) is still an upgrade.
    other = _release("newhash", 200.0)
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [other, current])
    assert manager.auto_upgrade_downloaded(force=True) == 1


def _old_copy_without_subtitles(manager) -> None:
    # Jimaku had nothing for it yet (a Ready row never steps back through upsert_episode).
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET state='waiting_subtitles' WHERE torrent_hash='oldhash'")


def test_a_complete_upgrade_replaces_a_copy_that_has_no_subtitles_either(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    _old_copy_without_subtitles(manager)
    _new_copy(manager, tmp_path, state="waiting_subtitles", subtitle=False)

    assert manager.finalize_ready_upgrades() == 1
    assert client.deleted == ["oldhash"]
    assert not (tmp_path / "Upgrade Show - 05.mkv").exists()
    assert manager.db.episode_for_torrent(700, 5, "newhash") is not None


def test_an_unfinished_upgrade_does_not_replace_anything(tmp_path: Path, monkeypatch) -> None:
    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    _old_copy_without_subtitles(manager)
    new_video = _new_copy(manager, tmp_path, state="waiting_subtitles", subtitle=False)
    new_video.with_name(new_video.name + ".aria2").write_bytes(b"control")  # still downloading
    assert manager.finalize_ready_upgrades() == 0
    assert client.deleted == [] and (tmp_path / "Upgrade Show - 05.mkv").is_file()


def test_looking_for_aria2_control_files_stops_at_the_root_folder() -> None:
    from pudge.library import aria2_control_file

    # Five levels deep and no library root: the walk reached "/" and raised ValueError.
    assert aria2_control_file(Path("/Users/me/Movies/pudge/Show - 01.mkv")) is None

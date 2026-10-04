import logging
from dataclasses import replace

import pytest

from pudge import episode_numbering as en
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa
from pudge.release_parser import parse_release_name
from test_v0730_acquisition_safety import manager_for
from test_v0730_acquisition_safety import Backend
from pudge.manager import ManagerError
from pudge.manager_models import DownloadItem, LibraryEpisode


def stage_fixture(manager):
    anime = LibraryAnime(210482, "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE", titles=["JoJo no Kimyou na Bouken: Steel Ball Run"], episodes=11, format="ONA", season_year=2026)
    manager.db.upsert_anime(anime)
    graph = {"root_id": 210482, "nodes": [
        {"media_id": 146722, "title": "JoJo no Kimyou na Bouken: Stone Ocean Part 2", "episodes": 26, "format": "ONA", "season_year": 2022},
        {"media_id": 190327, "title": "JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE", "episodes": 1, "format": "ONA", "season_year": 2026},
        {"media_id": 210482, "title": anime.title, "episodes": 11, "format": "ONA", "season_year": 2026}],
        "edges": [{"source": 146722, "target": 190327, "relation_type": "SEQUEL"}, {"source": 190327, "target": 210482, "relation_type": "SEQUEL"}]}
    manager.db.store_relation_graph(graph, refreshed_at=1, next_refresh_at=9999999999)
    return anime, graph


def release_for(number, *, trusted=True):
    return NyaaRelease(f"[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - {number:02} [1080p WEB-DL]", "", "", str(number), "1 GiB", 1024**3, 10, 0, 0, trusted, False, group="Erai-raws")


def search(client, anime, episode, aliases):
    return nyaa.search_ranked(client, anime, episode=episode, batch=False, trusted_groups=[], preferred_groups=[], blocked_groups=[], preferred_resolution="1080p", min_seeders=1, target_episode_min_bytes=1, target_episode_max_bytes=2 * 1024**3, alternative_episodes=aliases)


def test_stage_graph_stops_at_another_storyline(tmp_path):
    manager = manager_for(tmp_path)
    anime, graph = stage_fixture(manager)
    result = en.episode_numbering_from_graph(graph, anime, 2)
    assert (result.release_episode, result.aliases, result.chain) == (3, (3,), (190327, 210482))


def test_stage_numbering_cache_keeps_rule_for_other_episode(tmp_path):
    manager = manager_for(tmp_path)
    anime, graph = stage_fixture(manager)
    initial = en.episode_numbering_from_graph(graph, anime, 1)
    en._write_caches(manager.config, anime, initial)
    cached = en._read_cache(en._cache_paths(manager.config, anime.media_id)[0], media_episode=2)
    assert cached.aliases == (3,)
    assert cached.rule == initial.rule and cached.rule
    assert cached.release_episode == 3


@pytest.mark.parametrize("requested,want", [(1, True), (2, False)])
def test_same_stage_release_maps_to_only_one_requested_episode(tmp_path, requested, want):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    context = en.resolve_episode_numbering(anime, requested, manager.config, manager.logger, db=manager.db, allow_network=False)
    class Client:
        def search(self, query):
            return [release_for(2)]
    ranked = search(Client(), anime, requested, context.aliases)
    assert bool(ranked) is want
    if ranked:
        assert ranked[0].mapped_media_episode == 1
        assert ranked[0].raw_release_episode == 2


@pytest.mark.parametrize("title,want", [
    ("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run S99E03 [1080p]", "ambiguous"),
    ("[DifferentGroup] JoJo no Kimyou na Bouken: Steel Ball Run - 03 [1080p]", "ambiguous"),
    ("[Erai-raws] Unrelated Story - 03 [1080p]", "rejected"),
    ("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run S00E03 [1080p]", "rejected"),
])
def test_stage_rule_is_scoped_to_release_storyline_uploader_and_season(tmp_path, title, want):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    context = en.resolve_episode_numbering(anime, 2, manager.config, manager.logger, db=manager.db, allow_network=False)
    match = en.match_release_episode(anime, parse_release_name(title), 2, context, trusted_source=True)
    assert match.status == want


def test_stage_special_inverse_mapping_is_always_rejected(tmp_path):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    assert en.media_episode_from_release(anime, 2, manager.config, manager.logger, db=manager.db, release_season=0) is None


@pytest.mark.parametrize("name,want", [
    ("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 [1080p].mkv", 1),
    ("[Unknown] JoJo no Kimyou na Bouken: Steel Ball Run - 02 [1080p].mkv", None),
    ("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run S00E02 [1080p].mkv", None),
])
def test_import_inverse_uses_filename_source_scope(tmp_path, name, want):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    assert manager._media_episode_from_release(anime, 2, release_name=name) == want


@pytest.mark.parametrize("group,want", [("Erai-raws", 1), ("Unknown", None)])
def test_library_scan_applies_filename_scope_before_creating_ready_identity(tmp_path, monkeypatch, group, want):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    folder = manager.config.library.root_dir / "Steel Ball Run"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text(str(anime.media_id))
    video = folder / f"[{group}] JoJo no Kimyou na Bouken: Steel Ball Run - 02 [1080p].mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("embedded", None, 11))
    manager.scan_library()
    row = manager.db.episode_by_path(video)
    assert (row.media_episode if row is not None else None) == want


def test_qbittorrent_race_revalidates_stage_candidates_before_network(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    stage_fixture(manager)
    backend = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    manager._race_add_candidates(210482, [release_for(2), release_for(3), replace(release_for(3), info_hash="another-encode")], episode=2, batch=False, fast_seconds=0, total_seconds=0)
    assert "2" not in backend.added


def test_numbering_change_invalidates_scored_release_before_add(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="aria2")
    anime, graph = stage_fixture(manager)
    context = en.resolve_episode_numbering(anime, 2, manager.config, manager.logger, db=manager.db, allow_network=False)
    match = en.match_release_episode(anime, parse_release_name(release_for(3).title), 2, context)
    scored = replace(release_for(3), numbering_status="resolved", mapped_media_episode=2, numbering_revision=match.rule_revision)
    graph["nodes"][2]["episodes"] = 12
    manager.db.upsert_anime(replace(anime, episodes=12))
    manager.db.store_relation_graph(graph, refreshed_at=1, next_refresh_at=9999999999)
    backend = Backend("aria2")
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    with pytest.raises(ManagerError, match="revision"):
        manager.add_release(210482, scored, episode=2, batch=False)
    assert backend.added == []


def test_overlapping_stage_numbers_reverse_map_before_local_range(tmp_path):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    assert en.media_episode_from_release(anime, 2, manager.config, manager.logger, db=manager.db) == 1
    assert en.media_episode_from_release(anime, 2, manager.config, manager.logger, db=manager.db, requested_media_episode=2) is None
    assert en.media_episode_from_release(anime, 3, manager.config, manager.logger, db=manager.db, requested_media_episode=2) == 2


def test_unknown_uploader_overlap_requires_manual_identity(tmp_path):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    class Client:
        def search(self, query):
            return [release_for(2, trusted=False)]
    ranked = search(Client(), anime, 1, (2,))
    assert ranked
    assert ranked[0].numbering_status == "ambiguous"
    assert not manager._release_has_safe_episode_identity(ranked[0])


def test_reset_uploader_remains_manual_instead_of_borrowing_continuity(tmp_path):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    class Client:
        def search(self, query):
            return [replace(release_for(1), title=release_for(1).title.replace("Erai-raws", "ResetGroup"), group="ResetGroup")]
    ranked = search(Client(), anime, 1, (2,))
    assert ranked and ranked[0].numbering_status == "ambiguous"
    assert ranked[0].mapped_media_episode is None
    assert not manager._release_has_safe_episode_identity(ranked[0])


def test_numeric_title_range_does_not_hide_explicit_final_episode():
    name = "[Group] Ranma 1-2 (2024) - 05 [1080p].mkv"
    assert nyaa.release_episode(name) == 5
    assert parse_release_name(name).episode == 5
    assert nyaa.release_episode("[Group] Show - 01-12 [1080p].mkv") is None
    assert nyaa.release_episode("[Group] Show - 12.5 [1080p].mkv") is None


def test_empty_mirrors_probe_backed_off_primary_once(monkeypatch):
    client = nyaa.NyaaClient("https://nyaa.si", proxy_mode="direct")
    client._mirror_backoff = {"https://nyaa.si": nyaa.time.monotonic() + 300}
    urls = []
    def get(url, proxy):
        urls.append(url)
        if url.startswith("https://nyaa.si/"):
            return '<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item><title>Show - 01</title><link>https://nyaa.si/view/1</link><nyaa:infoHash>one</nyaa:infoHash></item></channel></rss>'
        return "<rss><channel/></rss>"
    monkeypatch.setattr(client, "_get", get)
    assert len(client.search("Show")) == 1
    assert sum(url.startswith("https://nyaa.si/") for url in urls) == 1
    client.close()


def test_add_release_revalidates_stage_identity_before_backend_add(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="aria2")
    stage_fixture(manager)
    backend = Backend("aria2")
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    with pytest.raises(ManagerError, match="identity"):
        manager.add_release(210482, release_for(2), episode=2, batch=False)
    assert backend.added == []


@pytest.mark.parametrize("provider", ["subsplease", "shana"])
def test_other_feeds_reject_previous_stage_release(tmp_path, provider):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    class Client:
        def releases(self, *args):
            return [release_for(2)]
    method = nyaa.search_subsplease_ranked if provider == "subsplease" else nyaa.search_shana_ranked
    ranked = method(Client(), anime, episode=2, batch=False, trusted_groups=[], preferred_groups=[], blocked_groups=[], preferred_resolution="1080p", min_seeders=1, target_episode_min_bytes=1, target_episode_max_bytes=2 * 1024**3, alternative_episodes=(3,))
    assert ranked == []


def test_identity_repair_is_dry_run_conflict_checked_and_persistent(tmp_path):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    anime = replace(anime, progress=1)
    manager.db.upsert_anime(anime)
    video = manager.config.library.root_dir / "Steel Ball Run - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"unchanged media content")
    manager.db.upsert_episode(LibraryEpisode(anime.media_id, anime.title, 2, video, media_episode=2, release_episode=2, torrent_hash="anchor", state="ready", playback_position=80.998, playback_duration=1516.098, embedded_subtitle_id=11), downloaded_at=1)
    manager.db.record_playback(video, 80.998, 1516.098)
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET playback_updated_at=1 WHERE video_path=?", (str(video),))
    manager.db.upsert_download(DownloadItem("anchor", release_for(2).title, "complete", 1, str(video.parent), str(video), media_id=anime.media_id, episode=2, release_episode=2, raw={"backend": "aria2"}))
    manager.db.record_release("anchor", anime.media_id, 2, release_for(2).title, 300, release_episode=2)
    selected = replace(release_for(2), info_hash="anchor")
    manager.download_intents.begin(anime.media_id, 2, False, [selected], backend="aria2")
    manager.download_intents.update(anime.media_id, 2, False, state="complete", selected=selected)
    plan = manager.db.plan_episode_identity_repair("anchor", video, media_id=anime.media_id, expected_media_episode=2, media_episode=1, release_episode=2)
    assert plan["conflicts"] == []
    assert manager.db.episode_by_path(video).media_episode == 2
    manager.db.apply_episode_identity_repair(plan)
    corrected = manager.db.episode_by_path(video)
    assert (corrected.media_episode, corrected.release_episode, corrected.playback_position, corrected.embedded_subtitle_id) == (1, 2, 80.998, 11)
    assert corrected.delete_after is None
    assert video.read_bytes() == b"unchanged media content"
    assert manager.db.download_by_hash("anchor").media_episode == 1
    assert manager.db.get_anime(anime.media_id).progress == 1
    assert manager.db.release_metadata_by_hash("anchor")[:3] == (210482, 1, 2)
    manager.db.record_release("anchor", anime.media_id, 2, release_for(2).title, 300, release_episode=2)
    assert manager.db.release_metadata_by_hash("anchor")[:3] == (210482, 1, 2)
    manager.db.schedule_cleanup(video, 0)
    assert manager.cleanup() == 0
    assert video.exists()
    assert manager.download_intents.get(anime.media_id, 2, False)["state"] == "waiting"
    stale = DownloadItem("anchor", release_for(2).title, "complete", 1, str(video.parent), str(video), media_id=anime.media_id, episode=2, release_episode=2, raw={"backend": "aria2"})
    manager._resolve_download_media(stale)
    assert stale.media_episode == 1
    manager.db.upsert_download(stale)
    assert manager.reconcile_completed_download_rows() == 0
    assert manager.db.episode_by_path(video).media_episode == 1
    assert list(tmp_path.glob("library.sqlite3.identity-repair-*.backup"))


def test_acquisition_persists_numbering_revision_in_intent_download_and_history(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    anime, _graph = stage_fixture(manager)
    release = manager._validated_release_identity(anime, release_for(3), 2, False)
    backend = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    manager._race_add_candidates(210482, [release, replace(release, info_hash="another")], episode=2, batch=False, fast_seconds=0, total_seconds=0)
    intent = manager.download_intents.get(210482, 2, False)
    assert intent["candidates"][0]["numbering_revision"] == release.numbering_revision
    row = manager.db.download_by_hash("3")
    assert row.raw["_release_numbering"]["numbering_revision"] == release.numbering_revision
    manager.db.record_release("3", 210482, 2, release.title, 300, release_episode=3, numbering_metadata=row.raw["_release_numbering"])
    import json
    history = json.loads(manager.db.get_state("release_numbering:3"))
    assert history["numbering_revision"] == release.numbering_revision
    assert history["numbering_scheme"] == release.numbering_scheme


def test_identity_repair_refuses_newer_playback_or_existing_local_target(tmp_path):
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET playback_position=9,updated_at=updated_at+1 WHERE video_path=?", (str(video),))
    with pytest.raises(ValueError, match="changed"):
        manager.db.apply_episode_identity_repair(plan)
    target = video.parent / "Example - 01.mkv"
    target.write_bytes(b"other")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 1, target))
    conflict = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    assert conflict["conflicts"]
    with pytest.raises(ValueError, match="conflict"):
        manager.db.apply_episode_identity_repair(conflict)


def test_identity_repair_refuses_active_playback(tmp_path):
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    manager.db.record_playback(video, 10, 1000)
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    assert plan["conflicts"]


@pytest.mark.parametrize("with_ledger", [False, True])
def test_identity_repair_moves_file_dependencies_and_locks_correct_canonical_identity(tmp_path, with_ledger):
    from pudge.identity import MediaIdentity
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    if with_ledger:
        manager.identity_resolver.record(MediaIdentity(77, 2, 2, video, "one", "automatic"))
    manager.db.record_subtitle_history(video_path=video, media_id=77, episode=2, source="embedded", candidate_name="sid11", status="selected")
    manager.db.create_playlist(name="Queue", kind="manual", media_id=77, items=[{"media_id":77, "episode":2, "video_path":str(video)}])
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    manager.db.apply_episode_identity_repair(plan)
    ledger = manager.identity_resolver.lookup(video_path=video)
    assert (ledger["canonical_id"], ledger["media_episode"], ledger["locked"]) == ("anime:77:episode:1", 1, 1)
    with manager.db.connect() as conn:
        assert conn.execute("SELECT episode FROM subtitle_history WHERE video_path=?", (str(video),)).fetchone()[0] == 1
        assert conn.execute("SELECT episode FROM playlist_items WHERE video_path=?", (str(video),)).fetchone()[0] == 1


@pytest.mark.parametrize("history_kind", ["consumption", "mobile"])
def test_identity_repair_refuses_unreviewed_synced_history(tmp_path, history_kind):
    from pudge.consumption import ConsumptionLedger
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    if history_kind == "consumption":
        ConsumptionLedger(manager.db).anime_episode_media(video)
    else:
        with manager.db.connect() as conn:
            conn.execute("INSERT INTO sync_entities(entity_id,kind,local_key,created_at,updated_at) VALUES('synced','anime_episode','77:2',1,1)")
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    assert plan["conflicts"]
    with pytest.raises(ValueError, match="conflict"):
        manager.db.apply_episode_identity_repair(plan)


def test_identity_repair_cli_requires_reviewed_plan_and_does_not_touch_progress(tmp_path, capsys):
    from pudge.cli import main
    import json

    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    plan_file = tmp_path / "reviewed-plan.json"
    common = ["repair-episode-identity", "--database", str(manager.db.path)]
    assert main(common + ["--video", str(video), "--torrent-hash", "one", "--media-id", "77", "--expected-media-episode", "2", "--media-episode", "1", "--release-episode", "2", "--plan-file", str(plan_file)]) == 0
    assert json.loads(plan_file.read_text())["conflicts"] == []
    assert manager.db.episode_by_path(video).episode == 2
    assert not list(tmp_path.glob("*.backup"))
    assert main(common + ["--apply-plan", str(plan_file)]) == 0
    assert manager.db.episode_by_path(video).episode == 1
    assert manager.db.get_anime(77).progress == 0
    assert main(common + ["--apply-plan", str(plan_file)]) == 0
    assert len(list(tmp_path.glob("*.backup"))) == 1


@pytest.mark.parametrize("mutation", ["history", "intent", "job", "bytes", "lock"])
def test_reviewed_identity_plan_rejects_changed_sources(tmp_path, mutation):
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    manager.db.record_release("one", 77, 2, video.name, 1, release_episode=2)
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    if mutation == "history":
        manager.db.record_release("one", 77, 2, video.name, 9, release_episode=2)
    elif mutation == "intent":
        manager.download_intents.begin(77, 2, False, [], backend="aria2")
    elif mutation == "job":
        manager.db.queue_subtitle_job(video, 77, 2)
    elif mutation == "bytes":
        video.write_bytes(b"changed bytes")
    else:
        manager.db.set_state("release_identity:one", '{"media_id":77,"media_episode":2,"release_episode":2}')
    with pytest.raises(ValueError, match="changed"):
        manager.db.apply_episode_identity_repair(plan)
    assert manager.db.episode_by_path(video).episode == 2


def test_identity_repair_migrate_history_relabels_single_entry_and_keeps_it_consistent(tmp_path):
    from pudge.consumption import ConsumptionLedger
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    ledger = ConsumptionLedger(manager.db)
    before = ledger.anime_episode_media(video)
    plain = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2)
    assert any("--migrate-history" in item for item in plain["conflicts"])
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2, migrate_history=True)
    assert plan["conflicts"] == [] and plan["migrate_history"] is True
    manager.db.apply_episode_identity_repair(plan)
    assert manager.db.episode_by_path(video).media_episode == 1
    with manager.db.connect() as conn:
        row = conn.execute("SELECT * FROM consumption_media WHERE media_uuid=?", (before.media_uuid,)).fetchone()
        aliases = {(r["alias_type"], r["alias_value"]) for r in conn.execute("SELECT * FROM consumption_media_aliases WHERE media_uuid=?", (before.media_uuid,))}
    assert row["current_library_id"] == "77:1" and row["title_snapshot"].endswith("· 1")
    assert ("external", "anilist:77:episode:1") in aliases and ("external", "anilist:77:episode:2") not in aliases
    # The next playback of the file resolves to the same history entry, no alias conflict.
    assert ledger.anime_episode_media(video).media_uuid == before.media_uuid


def test_identity_repair_migrate_history_never_overrides_mobile_sync(tmp_path):
    from pudge.consumption import ConsumptionLedger
    manager = manager_for(tmp_path)
    video = manager.config.library.root_dir / "Example - 02.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"file")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 2, video, torrent_hash="one"))
    ConsumptionLedger(manager.db).anime_episode_media(video)
    with manager.db.connect() as conn:
        conn.execute("INSERT INTO sync_entities(entity_id,kind,local_key,created_at,updated_at) VALUES('synced','anime_episode','77:2',1,1)")
    plan = manager.db.plan_episode_identity_repair("one", video, media_id=77, expected_media_episode=2, media_episode=1, release_episode=2, migrate_history=True)
    assert plan["conflicts"]

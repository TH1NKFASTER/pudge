from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.providers import anime_mappings as am

FIXTURES = Path(__file__).parent / "fixtures" / "anime_mappings"
FRIBB = (FIXTURES / "fribb-sample.json").read_bytes()
LISTS = (FIXTURES / "anime-lists-sample.xml").read_bytes()


class FakeHub:
    def __init__(self, fribb=FRIBB, lists=LISTS):
        self.bodies = {am.FRIBB_URL: fribb, am.ANIME_LISTS_URL: lists}
        self.calls: list[tuple[str, dict]] = []
        self.status: dict[str, int] = {}

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        status = self.status.get(url, 200)
        if headers.get("If-None-Match") == f"etag-{len(self.bodies[url])}" and status == 200:
            return 304, b"", {}
        return status, self.bodies[url] if status == 200 else b"", {"etag": f"etag-{len(self.bodies[url])}"}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _mappings(tmp_path, hub=None, clock=None):
    return am.AnimeMappings(tmp_path, fetch=hub or FakeHub(), now=clock or Clock(), min_rows={"fribb": 1, "anime_lists": 1})


def test_without_index_everything_is_empty_and_harmless(tmp_path):
    m = _mappings(tmp_path)
    assert m.available is False
    assert m.lookup_anilist(21) == [] and m.anilist_ids_for_tvdb(81797) == []


def test_build_and_lookup_with_real_schema(tmp_path):
    m = _mappings(tmp_path)
    assert m.refresh_if_due()["status"] == "rebuilt"
    [one_piece] = m.lookup_anilist(21)
    assert (one_piece.anidb_id, one_piece.tvdb_id, one_piece.mal_id) == (69, 81797, 21)
    assert one_piece.tvdb_absolute is True and one_piece.tvdb_season is None
    assert any(rule.target == "tmdb" and rule.start == 62 and rule.offset == -61 for rule in one_piece.rules)
    specials = am.episode_pairs(one_piece.rules, "tvdb")
    assert specials[(0, 1)] == (0, 27)
    assert (0, 6) not in specials, "a '6-0' pair means no TVDB equivalent"
    assert one_piece.provenance["ids"] == "Fribb/anime-lists"
    [bebop] = m.lookup_anilist(1)
    assert bebop.tvdb_id == 76885 and bebop.tvdb_season == 1


def test_one_tvdb_series_keeps_several_anilist_entries_apart(tmp_path):
    m = _mappings(tmp_path)
    m.refresh_if_due()
    ids = m.anilist_ids_for_tvdb(70973)  # 3x3 Eyes OVA 1 and 2 share one TVDB series
    assert len(ids) == 2 and len(set(ids)) == 2
    seasons = {mapping.tvdb_season for i in ids for mapping in m.lookup_anilist(i)}
    assert seasons == {1, 2}


def test_sentinel_and_offset_values(tmp_path):
    rows = am.parse_anime_lists(
        b'<anime-list><anime anidbid="7" tvdbid="movie" defaulttvdbseason="1" episodeoffset=""><name>M</name></anime>'
        b'<anime anidbid="8" tvdbid="12345" defaulttvdbseason="0" episodeoffset="11"><name>S</name>'
        b'<mapping-list><mapping anidbseason="1" tvdbseason="0">;1-4+5;2-0;</mapping></mapping-list></anime>'
        b'<anime anidbid="9" tvdbid="weird" defaulttvdbseason=""><name>U</name></anime></anime-list>'
    )
    by_id = {row["anidb_id"]: row for row in rows}
    assert by_id[7]["tvdb_id"] is None and by_id[7]["tvdb_sentinel"] == "movie"
    assert by_id[8]["tvdb_offset"] == 11 and by_id[8]["rules"][0]["pairs"] == ((1, (4, 5)), (2, (0,)))
    assert by_id[9]["tvdb_sentinel"] == "unknown"


def test_daily_conditional_refresh(tmp_path):
    hub, clock = FakeHub(), Clock()
    m = _mappings(tmp_path, hub, clock)
    m.refresh_if_due()
    assert m.refresh_if_due()["status"] == "fresh" and len(hub.calls) == 2
    clock.t += am.REFRESH_SECONDS + 1
    assert m.refresh_if_due()["status"] == "not_modified"
    assert all(call[1].get("If-None-Match") for call in hub.calls[2:]), "conditional requests after the first build"


def test_broken_update_keeps_the_working_index(tmp_path):
    hub, clock = FakeHub(), Clock()
    m = _mappings(tmp_path, hub, clock)
    m.refresh_if_due()
    before = m.index_path.read_bytes()
    hub.bodies[am.FRIBB_URL] = b"{not json"
    hub.bodies[am.ANIME_LISTS_URL] = b"<broken"
    clock.t += am.REFRESH_SECONDS + 1
    result = m.refresh_if_due()
    assert result["status"] == "error" and result["kept_index"] is True
    assert m.index_path.read_bytes() == before
    assert m.lookup_anilist(21)[0].tvdb_id == 81797
    assert "last_error" in m.status()


def test_network_failure_without_index_backs_off_then_retries(tmp_path):
    hub, clock = FakeHub(), Clock()
    hub.status[am.FRIBB_URL] = 503
    m = _mappings(tmp_path, hub, clock)
    assert m.refresh_if_due()["status"] == "error"
    assert m.refresh_due() is False
    assert m.refresh_if_due()["status"] == "fresh"
    assert len(hub.calls) == 1
    hub.status.clear()
    clock.t += 5 * 60 + 1
    assert m.refresh_if_due()["status"] == "rebuilt"


def test_failed_index_build_keeps_raw_sources_for_conditional_retry(tmp_path, monkeypatch):
    hub, clock = FakeHub(), Clock()
    m = _mappings(tmp_path, hub, clock)
    build = m._build
    def unavailable_builder(_payloads):
        raise OSError("temporary disk failure")
    monkeypatch.setattr(m, "_build", unavailable_builder)
    assert m.refresh_if_due()["status"] == "error"
    assert (m.root / "raw" / "fribb.bin").read_bytes() == FRIBB
    assert (m.root / "raw" / "anime_lists.bin").read_bytes() == LISTS
    assert m.refresh_if_due()["status"] == "fresh"
    assert len(hub.calls) == 2
    clock.t += 5 * 60 + 1
    monkeypatch.setattr(m, "_build", build)
    assert m.refresh_if_due()["status"] == "rebuilt"
    assert all(headers.get("If-None-Match") for _url, headers in hub.calls[2:])
    assert m.lookup_anilist(21)[0].tvdb_id == 81797


def test_small_or_truncated_source_is_rejected(tmp_path):
    m = am.AnimeMappings(tmp_path, fetch=FakeHub(), now=Clock())  # real thresholds
    result = m.refresh_if_due()
    assert result["status"] == "error" and "suspiciously small" in result["error"]
    assert m.available is False


def test_concurrent_updater_skips(tmp_path):
    m = _mappings(tmp_path)
    with m._process_lock() as first:
        assert first
        other = _mappings(tmp_path)
        assert other.refresh_if_due(force=True)["status"] == "busy"


def test_module_never_touches_library_or_identity():
    source = Path(am.__file__).read_text(encoding="utf-8")
    for forbidden in ("pudge.database", "IdentityResolver", "anilist_tracking", "..database", "..identity"):
        assert forbidden not in source


def test_background_refresh_is_off_in_tests_and_on_by_default(tmp_path, monkeypatch):
    m = _mappings(tmp_path)
    assert am.refresh_in_background(m) is None  # conftest sets PUDGE_ANIME_MAPPINGS=0
    monkeypatch.setenv("PUDGE_ANIME_MAPPINGS", "1")
    thread = am.refresh_in_background(m)
    assert thread is not None
    thread.join(5)
    assert m.available


def test_manager_maintenance_kicks_refresh_without_blocking(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pudge.manager import AnimeManager

    kicked = []
    monkeypatch.setattr(am, "refresh_in_background", lambda mappings, logger=None: kicked.append(mappings))
    fake = SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path)), logger=SimpleNamespace(warning=lambda *a: None))
    AnimeManager._kick_anime_mappings_refresh(fake)
    AnimeManager._kick_anime_mappings_refresh(fake)
    assert len(kicked) == 2 and kicked[0] is kicked[1]

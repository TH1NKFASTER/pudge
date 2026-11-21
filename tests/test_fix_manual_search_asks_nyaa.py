"""Find episode (a search the user starts) always asks Nyaa too, also when SubsPlease's feed has
a good release, and lists both (review r2-manager-1 #10). Automatic searches still stop at the
feed's release (behaviour; invented releases)."""

from __future__ import annotations

from test_subsplease_rss_preference_v0602 import _manager, _release


def _sources(monkeypatch) -> list[str]:
    calls: list[str] = []

    def rss(*args, **kwargs):
        calls.append("rss")
        return [_release("rss", score=150.0)]

    def nyaa(*args, **kwargs):
        calls.append("nyaa")
        return [_release("nyaa", score=120.0)]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)
    return calls


def test_find_episode_lists_nyaa_beside_a_good_feed_release(monkeypatch) -> None:
    calls = _sources(monkeypatch)
    releases = _manager().search_releases(135865, episode=5)
    assert calls == ["rss", "nyaa"]
    assert [release.group for release in releases] == ["SubsPlease", "Erai-raws"]


def test_an_automatic_search_still_stops_at_a_good_feed_release(monkeypatch) -> None:
    calls = _sources(monkeypatch)
    manager = _manager()
    releases = manager.search_releases(135865, episode=5, automatic=True)
    assert calls == ["rss"]
    assert [release.group for release in releases] == ["SubsPlease"]

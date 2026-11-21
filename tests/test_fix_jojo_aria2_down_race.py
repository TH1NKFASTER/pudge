"""aria2 down without the idle marker (gone after sleep or a crash) stopped every new
episode: the race's read-only status probe refused a stopped aria2 before the add that
would have started it, so the episode was found on Nyaa but never added."""

from __future__ import annotations

from test_fix_planning_downloads_oct15 import _SwarmAria2, _aria2_manager, _release, _snap

from pudge.providers.aria2 import Aria2Error


class _StoppedAria2(_SwarmAria2):
    def __init__(self, clock, scripts):
        super().__init__(clock, scripts)
        self.running = False
        self.started = 0

    def ensure_running(self):
        self.running = True
        self.started += 1

    def torrent_status(self, hash_):
        if not self.running:
            raise Aria2Error("aria2 RPC is not running")
        return super().torrent_status(hash_)

    def add_release(self, release, **kwargs):
        assert self.running, "added to a stopped aria2"
        return super().add_release(release, **kwargs)


def _alive(_age):
    return _snap(total=1024**3, connections=8, downloaded=4 * 1024**2, speed=900_000)


def test_race_starts_a_stopped_aria2_and_adds_the_episode(tmp_path, monkeypatch):
    release = _release("[Group] Shouwa Genroku Rakugo Shinjuu - 08 (1080p)", "aaa", 50, 300.0)
    manager, _backend, clock = _aria2_manager(tmp_path, monkeypatch, {"aaa": _alive})
    stopped = _StoppedAria2(clock, {"aaa": _alive})
    monkeypatch.setattr(manager, "qbt_client", lambda: stopped)

    winner = manager._race_add_candidates(20972, [release], episode=8, batch=False)

    assert winner is not None and winner.info_hash == "aaa"
    assert stopped.started >= 1
    assert stopped.added == ["aaa"]


def test_a_start_failure_still_ends_the_race_with_a_reason(tmp_path, monkeypatch, caplog):
    release = _release("[Group] Shouwa Genroku Rakugo Shinjuu - 08 (1080p)", "aaa", 50, 300.0)
    manager, _backend, clock = _aria2_manager(tmp_path, monkeypatch, {"aaa": _alive})

    class _Broken(_StoppedAria2):
        def ensure_running(self):
            raise Aria2Error("aria2 binary missing")

    broken = _Broken(clock, {"aaa": _alive})
    monkeypatch.setattr(manager, "qbt_client", lambda: broken)

    assert manager._race_add_candidates(20972, [release], episode=8, batch=False) is None
    assert broken.added == []


def test_idle_stop_that_outlives_the_shutdown_wait_still_leaves_the_marker(tmp_path, monkeypatch):
    from pudge.providers import aria2 as aria2_module
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    alive = {"value": True}
    monkeypatch.setattr(aria2_module.time, "sleep", lambda _s: alive.__setitem__("value", False))
    monkeypatch.setattr(client, "is_idle", lambda: True)
    monkeypatch.setattr(client, "shutdown", lambda **_k: False)  # exited only after its wait
    monkeypatch.setattr(client, "_probe", lambda: alive["value"])
    try:
        assert client.stop_when_idle() is True
        assert client.idle_stopped() is True
    finally:
        client.close()

"""A release naming this very stage resolves without a learned rule; an update
relaunch keeps Torrents On."""

import logging
import threading

import pytest

from pudge import episode_numbering as en
from pudge import web_app
from pudge.release_parser import parse_release_name
from test_v0730_acquisition_safety import manager_for
from test_v0730_release_identity import stage_fixture

STAGE_TITLED = (
    "Bizarre Saga S06E08 Part Two 1080p WEB-DL H.264-NEWGROUP "
    "(JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE, Multi-Subs)"
)


def _match(tmp_path, title, requested=7, trusted=True):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    context = en.resolve_episode_numbering(
        anime, requested, manager.config, manager.logger, db=manager.db, allow_network=False
    )
    return en.match_release_episode(
        anime, parse_release_name(title), requested, context, trusted_source=trusted
    )


def test_release_naming_this_stage_maps_by_the_relation_graph(tmp_path):
    match = _match(tmp_path, STAGE_TITLED)
    assert match.status == "resolved"
    assert match.mapped_media_episode == 7


def test_release_naming_this_stage_resolves_from_an_untrusted_uploader_too(tmp_path):
    assert _match(tmp_path, STAGE_TITLED, trusted=False).status == "resolved"


def test_release_naming_this_stage_still_rejects_another_episode(tmp_path):
    match = _match(tmp_path, STAGE_TITLED.replace("S06E08", "S06E09"))
    assert match.status == "rejected"


@pytest.mark.parametrize(
    "title",
    [
        "Bizarre Saga S06E08 1080p WEB-DL (JoJo no Kimyou na Bouken: Steel Ball Run) [NEWGROUP]",
        "Bizarre Saga S06E08 1080p WEB-DL (JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE) [NEWGROUP]",
    ],
)
def test_storyline_or_other_stage_only_stays_a_proposal(tmp_path, title):
    assert _match(tmp_path, title).status == "ambiguous"


class _Api:
    resume_torrents_after_update = web_app.WebAppApi.resume_torrents_after_update

    def __init__(self, flag):
        self._resume_torrents_after_update = flag
        self.logger = logging.getLogger("t")
        self.calls = []
        self.done = threading.Event()

    def set_torrents_enabled(self, enabled):
        self.calls.append(enabled)
        self.done.set()


def test_update_relaunch_turns_torrents_back_on_once():
    api = _Api(True)
    assert api.resume_torrents_after_update() is True
    assert api.done.wait(30)
    assert api.calls == [True]
    assert api.resume_torrents_after_update() is False
    assert api.calls == [True]


def test_ordinary_launch_keeps_torrents_off():
    api = _Api(False)
    assert api.resume_torrents_after_update() is False
    assert api.calls == []


@pytest.mark.parametrize(
    "previous,environ,want",
    [
        (True, {"PUDGE_LAUNCH_BACKGROUND": "1"}, True),
        (False, {"PUDGE_LAUNCH_BACKGROUND": "1"}, False),
        (True, {}, False),
    ],
)
def test_only_a_background_relaunch_of_an_on_session_resumes(previous, environ, want):
    assert web_app._torrents_resume_after_relaunch(previous, environ) is want


def test_aria2_down_on_purpose_reads_as_idle_not_as_an_error(tmp_path, monkeypatch):
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, source_proxy_mode="direct")
    monkeypatch.setattr(client, "_probe", lambda: False)
    assert client.idle_stopped() is False
    assert client.mark_deliberately_stopped() is True
    assert client.idle_stopped() is True


def test_running_aria2_is_not_marked_stopped(tmp_path, monkeypatch):
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, source_proxy_mode="direct")
    monkeypatch.setattr(client, "_probe", lambda: True)
    assert client.mark_deliberately_stopped() is False
    assert not (tmp_path / "aria2" / "idle-stopped").exists()

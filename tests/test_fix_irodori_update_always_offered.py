"""Update Irodori is offered for every working install (behaviour).

The user had Irodori installed (commit 61012c76…) and no way to update it: Settings showed
the button only when GitHub's main had moved on. Now an installed, working Irodori always
shows Update Irodori with its revision and when it was last checked; Update asks GitHub
first and says "already newest" (or that GitHub did not answer) without touching the
running server. Installs from before last-good.json existed get their commit recorded on
the first status read; fresh installs take the commit audiobook generation was checked with.
git's network side, uv and the server are the stand-ins of test_fix_mokuro_irodori_tested_versions.
"""

from __future__ import annotations

import json
import logging

import pytest
from test_fix_locked_dev_installs import _install
from test_fix_mokuro_irodori_tested_versions import IRODORI_SETTINGS, _update_irodori
from test_fix_mokuro_irodori_tested_versions import irodori as irodori_fixture
from test_v089_irodori_managed_install import IRODORI_API, IRODORI_ON, _api

import pudge.web_app as web_app_module

irodori = irodori_fixture
TESTED = "61012c760f22f7b4a6c21c5c5f8f9e148120b6f9"


def _stops(api, monkeypatch) -> list[bool]:
    stops: list[bool] = []
    monkeypatch.setattr(api, "_stop_managed_irodori_server", lambda *args, **kwargs: stops.append(True))
    return stops


def test_an_install_without_a_last_good_record_adopts_its_commit(irodori, caplog) -> None:
    api, stand_in = irodori
    api._irodori_last_good_path().unlink()  # installed before Pudge recorded one

    status = api.irodori_install_status()

    assert api._irodori_last_good()["revision"] == stand_in.first == stand_in.head()
    assert status["last_good_revision"] == stand_in.first[:8]
    # Nothing was checked or installed yet: no dates to show.
    assert (status["last_checked_at"], status["last_updated_at"]) == (0.0, 0.0)
    assert f"RESULT step=irodori.last_good result=adopted revision={stand_in.first}" in caplog.text
    # Repair now puts this commit back, not the pin or main.
    api._irodori_managed_python().unlink()
    stand_in.calls.clear()
    api.install_irodori_tts()
    api._irodori_install_thread.join(30)
    repo = str(api._irodori_repo_dir())
    assert ["/usr/bin/git", "-C", repo, "reset", "--hard", stand_in.first] in stand_in.calls


def test_update_with_nothing_new_on_main_leaves_the_install_alone(irodori, monkeypatch, caplog) -> None:
    api, stand_in = irodori
    api._irodori_last_good_path().unlink()
    api.irodori_install_status()  # adopted
    stand_in.fetch_main_to = stand_in.first  # GitHub's main is the installed commit
    stops = _stops(api, monkeypatch)

    status = _update_irodori(api)

    assert stops == [] and stand_in.calls == [] and stand_in.synced_at == []  # no fetch, no uv sync
    assert status["state"] == "ready" and status["update_available"] is False
    assert status["detail"] == web_app_module.t("web.irodoriUpToDate", revision=stand_in.first[:8])
    assert status["last_checked_at"] > 0 and status["last_updated_at"] == 0.0
    assert f"RESULT step=irodori.update result=current revision={stand_in.first}" in caplog.text


def test_update_says_when_github_does_not_answer(irodori, monkeypatch, caplog) -> None:
    api, stand_in = irodori
    checked_before = api._irodori_last_good()["checked_at"]
    monkeypatch.setattr(api, "_irodori_remote_revision", lambda **_kwargs: "")
    stops = _stops(api, monkeypatch)

    status = _update_irodori(api)

    assert stops == [] and stand_in.calls == [] and stand_in.head() == stand_in.first
    assert status["state"] == "ready" and status["installed"] is True
    assert status["detail"] == web_app_module.err("setup.irodoriUpdateUnchecked", revision=stand_in.first[:8])
    assert api._irodori_last_good()["checked_at"] == checked_before
    assert "FAIL step=irodori.update reason=main_unavailable" in caplog.text


@pytest.mark.skipif(not web_app_module.shutil.which("git"), reason="git required")
def test_a_fresh_install_takes_the_tested_commit(tmp_path, monkeypatch, caplog) -> None:
    assert web_app_module.IRODORI_TTS_COMMIT == TESTED
    api = _api(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)

    calls, _cloned = _install(api, monkeypatch)

    repo = str(api._irodori_repo_dir())
    assert ["/usr/bin/git", "-C", repo, "fetch", "--depth", "1", "origin", TESTED] in calls
    assert ["/usr/bin/git", "-C", repo, "reset", "--hard", TESTED] in calls
    assert f"pinned={TESTED} source=pin" in caplog.text


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_offer_update_irodori_with_its_revision_and_last_check(page, lang) -> None:
    installed = {
        "state": "ready",
        "installed": True,
        "update_available": False,
        "revision": "61012c76",
        "last_checked_at": 1790000000,
        "last_updated_at": 0,
    }
    result = page.run(
        IRODORI_SETTINGS
        + r"""
        const out = {texts, view: view(), version: __text('#irodoriVersion'),
                     revision: t('ui.irodoriRevision', {revision: '61012c76'}), checked: t('settings.toolLastChecked', {date: ''}).trim(),
                     updated: t('settings.toolLastUpdated', {date: ''}).trim()};
        await __click('#installIrodoriTts'); await __tick(10);
        return out;
        """,
        lang=lang,
        state=IRODORI_ON,
        before=f"window.STATUS = [{json.dumps(installed)}];"
        + IRODORI_API
        + "__api.update_irodori_tts = async () => ({ok: true});",
    )
    out = result.value
    assert out["view"]["button"] == out["texts"]["update"] and out["view"]["disabled"] is False
    assert out["version"].startswith(out["revision"] + " · ") and out["checked"] in out["version"]
    assert out["updated"] not in out["version"]
    assert result.calls_of("update_irodori_tts") == [[]] and result.calls_of("install_irodori_tts") == []

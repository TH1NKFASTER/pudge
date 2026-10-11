"""Update Mokuro switches only for a newer Mokuro or MangaOCR (behaviour).

The user's log said `result=updated from=0.2.5 to=0.2.5`: a fresh environment with other
dependency releases (certifi, regex …) counted as an update and was checked and switched to.
Update now asks the package index first (`pip index versions` in the working environment):
both newest already → "already the newest", no environment is built; the index unreachable
→ "couldn't check", nothing changes; only a changed Mokuro or MangaOCR is an update.
pip, venv and the Mokuro checks are the stand-ins of test_fix_mokuro_irodori_tested_versions.
"""

from __future__ import annotations

from test_fix_mokuro_irodori_tested_versions import _update
from test_fix_mokuro_irodori_tested_versions import mokuro as mokuro_fixture

import pudge.web_app as web_app_module

mokuro = mokuro_fixture


def _built(stand_in) -> bool:
    return any(command[1:3] == ["-m", "venv"] for command in stand_in.calls)


def _churn(tested: dict[str, str]) -> dict[str, str]:
    """A new environment with the same Mokuro and MangaOCR, other dependencies newer."""
    return {**tested, "certifi": "2099.1.1", "regex": "2099.1.1"}


def test_newest_mokuro_and_manga_ocr_need_no_new_environment(mokuro, caplog) -> None:
    api, stand_in = mokuro
    tested = dict(api._mokuro_tested_set()[1])
    root = api._mokuro_ocr_root()
    stand_in.index = {"mokuro": "0.2.5", "manga-ocr": "0.1.16"}
    stand_in.newest = _churn(tested)  # what a new environment would get

    status = _update(api)

    assert not _built(stand_in) and stand_in.checked == []
    assert not any(command[1:4] == ["-m", "pip", "install"] for command in stand_in.calls)
    assert [
        command[5] for command in stand_in.calls if command[1:5] == ["-m", "pip", "index", "versions"]
    ] == [
        "mokuro",
        "manga-ocr",
    ]
    assert stand_in.packages(root / "venv") == tested
    assert status["state"] == "ready" and status["updatable"] is True
    assert status["detail"] == web_app_module.t(
        "web.mokuroNewestAlready", version="0.2.5", manga_ocr="0.1.16"
    )
    assert status["last_checked_at"] > 0 and status["last_updated_at"] == 0.0
    assert "RESULT step=mokuro.update result=current version=0.2.5 manga_ocr=0.1.16" in caplog.text
    assert "result=updated" not in caplog.text


def test_only_other_dependencies_changing_is_not_an_update(mokuro, caplog) -> None:
    # The index names a newer MangaOCR, but pip installs the same Mokuro and MangaOCR
    # (e.g. the new release needs another Python): the working environment stays.
    api, stand_in = mokuro
    tested = dict(api._mokuro_tested_set()[1])
    root = api._mokuro_ocr_root()
    stand_in.index = {"mokuro": "0.2.5", "manga-ocr": "0.1.17"}
    stand_in.newest = _churn(tested)

    status = _update(api)

    assert _built(stand_in)
    assert stand_in.checked == []  # no 30-minute check of an environment that is not used
    assert stand_in.packages(root / "venv") == tested
    assert not (root / "venv-update").exists()
    assert status["detail"] == web_app_module.t(
        "web.mokuroNewestAlready", version="0.2.5", manga_ocr="0.1.16"
    )
    assert status["last_updated_at"] == 0.0
    assert "result=current version=0.2.5 manga_ocr=0.1.16 newest=manga-ocr=0.1.17" in caplog.text
    assert "result=updated" not in caplog.text
    assert api._mokuro_tested_set()[1] == tested


def test_a_newer_manga_ocr_alone_is_an_update(mokuro, caplog) -> None:
    api, stand_in = mokuro
    tested = dict(api._mokuro_tested_set()[1])
    root = api._mokuro_ocr_root()
    stand_in.index = {"mokuro": "0.2.5", "manga-ocr": "0.1.17"}
    stand_in.newest = {**_churn(tested), "manga-ocr": "0.1.17"}

    status = _update(api)

    assert [env for env, _ in stand_in.checked] == [root / "venv-update"] * 2
    assert stand_in.packages(root / "venv")["manga-ocr"] == "0.1.17"
    assert (status["mokuro_version"], status["manga_ocr_version"]) == ("0.2.5", "0.1.17")
    assert status["detail"] == web_app_module.t(
        "web.mokuroUpdatedPackages", changes="MangaOCR 0.1.16 → 0.1.17"
    )
    assert status["last_updated_at"] > 0
    assert "result=updated from=0.2.5 to=0.2.5 manga_ocr=0.1.17" in caplog.text
    assert "changed=manga-ocr:0.1.16->0.1.17" in caplog.text


def test_update_says_when_it_could_not_check_for_a_newer_mokuro(mokuro, caplog) -> None:
    api, stand_in = mokuro
    tested = dict(api._mokuro_tested_set()[1])
    root = api._mokuro_ocr_root()
    stand_in.index = None  # no connection to the package index

    status = _update(api)

    assert not _built(stand_in) and stand_in.checked == []
    assert stand_in.packages(root / "venv") == tested
    assert status["state"] == "ready" and status["running"] is False and status["updatable"] is True
    assert status["detail"] == web_app_module.err(
        "setup.mokuroUpdateUnchecked", kept="0.2.5", reason="ERROR: No matching distribution found for mokuro"
    )
    # Not checked: "Last checked" keeps its date.
    assert status["last_checked_at"] == 0.0 and status["last_updated_at"] == 0.0
    assert "FAIL step=mokuro.update reason=index_unavailable version=0.2.5" in caplog.text


def test_an_older_release_on_the_index_is_not_newer() -> None:
    newer = web_app_module._release_is_newer
    assert newer("0.2.6", "0.2.5") and newer("0.10.0", "0.9.9") and newer("1.0", "0.2.5")
    assert not newer("0.2.5", "0.2.5") and not newer("0.2.4", "0.2.5") and not newer("", "0.2.5")
    assert newer("0.2.5", "")  # not installed in the environment

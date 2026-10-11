from __future__ import annotations

from pathlib import Path

import pudge.first_experience as first

from tests.i18n_support import ru


def _fake_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "data" / "jiten-mpv",
        tmp_path / "mpv" / "scripts" / "jiten-mpv.lua",
        tmp_path / "config" / "jiten-mpv" / "config.json",
    )


def _installed_status() -> dict[str, object]:
    return {
        "mpv": {"installed": True},
        "jiten_mpv": {"installed": True},
    }


def test_jiten_version_marker_and_legacy_tls_verdict(monkeypatch, tmp_path: Path) -> None:
    paths = _fake_paths(tmp_path)
    monkeypatch.setattr(first, "_jiten_paths", lambda: paths)
    paths[2].parent.mkdir(parents=True)
    (paths[2].parent / "tls-probe-verdict").write_text("0.2.4|27.0|ok\n", encoding="utf-8")
    assert first._read_jiten_installed_version() == "0.2.4"

    first._write_jiten_installed_version("v0.2.5")
    assert (paths[0] / first.JITEN_MPV_VERSION_MARKER).read_text(encoding="utf-8").strip() == "0.2.5"
    assert first._read_jiten_installed_version() == "0.2.5"


def test_check_only_checks_and_never_runs_installer(monkeypatch, tmp_path: Path) -> None:
    paths = _fake_paths(tmp_path)
    monkeypatch.setattr(first, "_jiten_paths", lambda: paths)
    paths[0].mkdir(parents=True)
    first._write_jiten_installed_version("0.2.4")
    monkeypatch.setattr(first, "dependency_status", lambda **_kwargs: _installed_status())
    monkeypatch.setattr(first, "_latest_jiten_release", lambda: ("0.2.5", "v0.2.5"))
    monkeypatch.setattr(
        first,
        "_run_jiten_mpv_installer",
        lambda: (_ for _ in ()).throw(AssertionError("installer ran during check")),
    )

    result = first.check_jiten_mpv_update()
    assert result["update_available"] is True
    assert result["current_version"] == "0.2.4"
    assert result["latest_version"] == "0.2.5"


def test_check_reports_up_to_date_and_unreachable_distinctly(monkeypatch, tmp_path: Path) -> None:
    paths = _fake_paths(tmp_path)
    monkeypatch.setattr(first, "_jiten_paths", lambda: paths)
    paths[0].mkdir(parents=True)
    first._write_jiten_installed_version("0.2.5")
    monkeypatch.setattr(first, "dependency_status", lambda **_kwargs: _installed_status())
    monkeypatch.setattr(first, "_latest_jiten_release", lambda: ("0.2.5", "v0.2.5"))
    current = first.check_jiten_mpv_update()
    assert current["reachable"] is True
    assert current["update_available"] is False

    def unreachable() -> tuple[str, str]:
        raise first.FirstExperienceError("offline")

    monkeypatch.setattr(first, "_latest_jiten_release", unreachable)
    offline = first.check_jiten_mpv_update()
    assert offline["reachable"] is False
    assert offline["update_available"] is False


def test_update_runs_official_installer_only_on_explicit_update(monkeypatch, tmp_path: Path) -> None:
    paths = _fake_paths(tmp_path)
    monkeypatch.setattr(first, "_jiten_paths", lambda: paths)
    paths[0].mkdir(parents=True)
    paths[1].parent.mkdir(parents=True)
    paths[1].write_text("-- installed", encoding="utf-8")
    first._write_jiten_installed_version("0.2.4")
    calls: list[str] = []
    monkeypatch.setattr(first, "dependency_status", lambda **_kwargs: _installed_status())
    monkeypatch.setattr(first, "_latest_jiten_release", lambda: ("0.2.5", "v0.2.5"))
    monkeypatch.setattr(first, "_run_jiten_mpv_installer", lambda: calls.append("install"))
    monkeypatch.setattr(first, "_write_jiten_api_key", lambda _value: None)

    first.update_jiten_mpv()
    assert calls == ["install"]
    assert first._read_jiten_installed_version() == "0.2.5"


# Settings → Jiten: one button that installs, checks or updates JitenMPV.
JITEN_BUTTON = r"""
const view = () => ({button: __text('#jitenMpvAction'), status: __text('#s_jiten_mpv_update_status')});
const press = async () => { await __click('#jitenMpvAction'); await __tick(10); return view(); };
const texts = {check: t('ui.checkJitenmpvUpdate'), update: t('ui.updateJitenmpv'), install: t('ui.installJitenmpv'),
               updated: t('ui.jitenmpvUpdated'), installed: t('ui.jitenmpvInstalled'), offline: t('ui.couldNotCheckGithub'),
               missing: t('ui.jitenmpvIsNotInstalled'), upToDate: t('ui.upToDate', {value: ' v0.2.5'})};
"""


def _plugins(installed: bool, version: str = "") -> dict[str, object]:
    return {"mpv_study_plugins": {"jiten_mpv": {"installed": installed, "version": version}}}


def test_settings_check_first_and_update_only_on_a_second_press(page) -> None:
    assert ru("ui.checkJitenmpvUpdate") == "Проверить обновление JitenMPV"
    assert ru("ui.updateJitenmpv") == "Обновить JitenMPV"
    assert ru("ui.installJitenmpv") == "Установить JitenMPV"
    for lang in ("en", "ru"):
        result = page.run(
            JITEN_BUTTON
            + r"""
            const start = view();
            const checked = await press();
            const calls = __apiCalls.map(call => call.name).filter(name => name.startsWith('jiten_mpv'));
            const updated = await press();
            return {start, checked, calls, updated, toast: __text('#toast'), texts};
            """,
            lang=lang,
            state={"settings": _plugins(True, "0.2.4")},
            api={
                "jiten_mpv_check_update": {
                    "reachable": True,
                    "update_available": True,
                    "current_version_known": True,
                    "current_version": "0.2.4",
                    "latest_version": "0.2.5",
                },
                "jiten_mpv_update": {"ok": True, "plugins": _plugins(True, "0.2.5")["mpv_study_plugins"]},
            },
        )
        out = result.value
        texts = out["texts"]
        assert out["start"] == {"button": texts["check"], "status": "v0.2.4"}
        # Checking never updates: the button offers the update and shows both versions.
        assert out["calls"] == ["jiten_mpv_check_update"]
        assert out["checked"] == {"button": texts["update"], "status": "v0.2.4 → v0.2.5"}
        assert out["updated"] == {"button": texts["check"], "status": "v0.2.5"}
        assert out["toast"] == texts["updated"]
        assert [call["name"] for call in result.calls if call["name"].startswith("jiten_mpv")] == [
            "jiten_mpv_check_update",
            "jiten_mpv_update",
        ]


def test_settings_report_up_to_date_unreachable_and_not_installed(page) -> None:
    code = (
        JITEN_BUTTON
        + "const start = view(); return {start, pressed: await press(), toast: __text('#toast'), texts};"
    )
    current = page.run(
        code,
        state={"settings": _plugins(True, "0.2.5")},
        api={
            "jiten_mpv_check_update": {
                "reachable": True,
                "update_available": False,
                "current_version": "0.2.5",
            }
        },
    )
    texts = current.value["texts"]
    assert current.value["pressed"] == {"button": texts["check"], "status": texts["upToDate"]}
    offline = page.run(
        code,
        state={"settings": _plugins(True, "0.2.5")},
        api={"jiten_mpv_check_update": {"reachable": False, "update_available": False}},
    )
    assert offline.value["pressed"] == {"button": texts["check"], "status": texts["offline"]}
    assert not offline.called("jiten_mpv_update")
    missing = page.run(
        code,
        state={"settings": _plugins(False)},
        api={"jiten_mpv_install": {"ok": True, "plugins": _plugins(True, "0.2.5")["mpv_study_plugins"]}},
    )
    assert missing.value["start"] == {"button": texts["install"], "status": texts["missing"]}
    assert missing.value["pressed"] == {"button": texts["check"], "status": "v0.2.5"}
    assert missing.value["toast"] == texts["installed"]
    assert [call["name"] for call in missing.calls if call["name"].startswith("jiten_mpv")] == [
        "jiten_mpv_install"
    ]


def test_backend_check_and_update_endpoints(monkeypatch, tmp_path: Path) -> None:
    import pudge.web_app as web_app
    from test_web_app import make_api

    api = make_api(tmp_path)
    api.light_novels.save_settings({"jiten_api_key": "jiten-key"})
    checked: list[dict[str, str]] = []
    monkeypatch.setattr(
        web_app,
        "check_jiten_mpv_update",
        lambda **kwargs: checked.append(kwargs) or {"update_available": True},
    )
    assert api.jiten_mpv_check_update() == {"update_available": True}
    assert checked == [{"mpv": api.config.tools.mpv, "ffmpeg": api.config.tools.ffmpeg}]

    updated: list[str] = []
    monkeypatch.setattr(
        web_app,
        "update_jiten_mpv_release",
        lambda key, **_kwargs: updated.append(key) or {"jiten_mpv": {"installed": True, "version": "0.2.5"}},
    )
    monkeypatch.setattr(
        web_app, "mpv_study_status", lambda **_kwargs: {"jiten_mpv": {"installed": True, "version": "0.2.5"}}
    )
    result = api.jiten_mpv_update()
    assert updated == ["jiten-key"]
    assert result == {
        "ok": True,
        "status": {"installed": True, "version": "0.2.5"},
        "plugins": {"jiten_mpv": {"installed": True, "version": "0.2.5"}},
    }

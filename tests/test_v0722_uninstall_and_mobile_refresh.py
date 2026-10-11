from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from css_support import css_rules
from pudge import web_app
from pudge.uninstall import build_uninstall_plan, launch_uninstaller, render_uninstall_script
from source_policy import source
from test_web_app import make_api
from tests.i18n_support import en, ru


def test_uninstall_plan_removes_pudge_owned_state_without_shared_tools(tmp_path: Path) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    backup = downloads / "pudge-backup-20260820-195852.zip"
    backup.touch()
    config_path = home / ".config" / "pudge" / "config.toml"
    database_path = home / ".local" / "share" / "pudge" / "library.sqlite3"
    plan = build_uninstall_plan(
        home=home,
        config_path=config_path,
        cache_dir=home / "Library" / "Caches" / "pudge",
        database_path=database_path,
        library_root=home / "Movies" / "pudge",
    )

    targets = {str(path) for path in plan.targets}
    assert str(home / "Applications" / "pudge.app") in targets
    assert str(home / ".local" / "bin" / "pudge") in targets
    assert str(home / "Library" / "LaunchAgents" / "com.pudge.agent.plist") in targets
    assert str(home / "Movies" / "pudge") in targets
    assert str(database_path.with_name("library.sqlite3-wal")) in targets
    assert str(backup) in targets
    assert str(home / ".local" / "share" / "jiten-mpv") not in targets
    assert str(home / "Library" / "Application Support" / "qBittorrent") not in targets
    assert Path("/") not in plan.targets
    assert home not in plan.targets


def test_uninstall_plan_does_not_delete_an_arbitrary_library_or_cache_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    shared_cache = home / "Library" / "Caches"
    plan = build_uninstall_plan(home=home, cache_dir=shared_cache, library_root=downloads)

    assert downloads not in plan.targets
    assert shared_cache not in plan.targets


def test_uninstall_script_waits_for_pudge_and_uses_exact_targets(tmp_path: Path) -> None:
    home = tmp_path / "Home With Spaces"
    plan = build_uninstall_plan(home=home)
    script = render_uninstall_script(plan, parent_pid=4321)

    assert "parent_pid=4321" in script
    assert "/bin/kill -0 $parent_pid" in script
    assert "launchctl bootout" in script
    assert "security delete-generic-password -s com.pudge.app" in script
    assert "tccutil reset All com.pudge.app" in script
    assert "/bin/rm -rf --" in script
    assert str(home / "Applications" / "pudge.app") in script
    assert "*.app" not in script


def test_uninstaller_is_launched_as_a_detached_private_script(tmp_path: Path, monkeypatch) -> None:
    launched: dict[str, object] = {}

    def fake_popen(command, **options):
        launched["command"] = command
        launched["options"] = options
        return object()

    monkeypatch.setattr("pudge.uninstall.subprocess.Popen", fake_popen)
    script_path = launch_uninstaller(build_uninstall_plan(home=tmp_path / "home"), parent_pid=77)
    try:
        assert launched["command"] == ["/bin/zsh", str(script_path)]
        assert launched["options"]["start_new_session"] is True
        assert script_path.stat().st_mode & 0o777 == 0o700
        assert "parent_pid=77" in script_path.read_text(encoding="utf-8")
    finally:
        script_path.unlink(missing_ok=True)


UNINSTALL = r"""
setPage('settings', true); await __tick(100);
const confirmation = () => ({title: __text('#pudgeConfirmTitle'), message: $('pudgeConfirmMessage').textContent,
  ok: __text('[data-pudge-confirm="ok"]'), danger: document.querySelector('[data-pudge-confirm="ok"]').classList.contains('danger')});
const uninstalls = () => __apiCalls.filter(call => call.name === 'uninstall_pudge').length;
const out = {button: $('uninstallPudge').innerText, expected: {
  first: {title: t('ui.deletePudge'), message: t('ui.thisPermanentlyRemovesThePudge'), ok: t('ui.continue'), danger: true},
  second: {title: t('ui.deleteEverything'), message: t('ui.finalConfirmationThisCannotBe'), ok: t('ui.deletePudge2'), danger: true},
  button: t('ui.deletePudgeFromThisMac')}};
await __click('#uninstallPudge'); await __tick(20);
out.first = confirmation();
await __click('[data-pudge-confirm="cancel"]'); await __tick(20);
out.afterFirstCancel = uninstalls();
await __click('#uninstallPudge'); await __tick(20);
await __click('[data-pudge-confirm="ok"]'); await __tick(20);
out.second = confirmation();
await __click('[data-pudge-confirm="cancel"]'); await __tick(20);
out.afterSecondCancel = uninstalls();
await __click('#uninstallPudge'); await __tick(20);
await __click('[data-pudge-confirm="ok"]'); await __tick(20);
await __click('[data-pudge-confirm="ok"]'); await __tick(20);
out.afterBoth = uninstalls(); out.disabled = $('uninstallPudge').disabled;
return out;
"""


def test_settings_offer_a_red_double_confirmed_uninstall(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(UNINSTALL, lang=lang)
        out = result.value
        assert out["button"] == out["expected"]["button"]
        # Two red confirmations; cancelling either one deletes nothing.
        assert out["first"] == out["expected"]["first"] and out["second"] == out["expected"]["second"]
        assert (out["afterFirstCancel"], out["afterSecondCancel"], out["afterBoth"]) == (0, 0, 1)
        assert out["disabled"] is True
    assert en("ui.deletePudgeFromThisMac") == "Delete Pudge from this Mac"
    assert ru("ui.deletePudgeFromThisMac") == "Удалить Pudge с этого Mac"
    assert css_rules("index.html").get("button.uninstall-pudge", "background") == "#a51f33"


def test_uninstall_api_starts_the_detached_cleanup_then_closes_the_window(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    launched, timers, destroyed = [], [], []

    class Timer:
        def __init__(self, delay, function):
            timers.append((delay, function))

        def start(self):
            pass

    monkeypatch.setattr(web_app, "launch_uninstaller", launched.append)
    monkeypatch.setattr(web_app.threading, "Timer", Timer)
    monkeypatch.setattr(web_app.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match=re.escape(en("error.web.uninstallMacOnly"))):
        api.uninstall_pudge()
    assert launched == [] and timers == []
    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    result = api.uninstall_pudge()
    [plan] = launched
    assert result == {"ok": True, "target_count": len(plan.targets)}
    # This installation's own config and database are part of the plan.
    assert {Path(api.config_path), Path(api.config.library.database_path)} <= set(plan.targets)
    # The window closes shortly after the cleanup process was started.
    [(delay, close)] = timers
    assert delay == 0.2
    api.window = SimpleNamespace(destroy=lambda: destroyed.append(True))
    close()
    assert destroyed == [True]


def test_readme_tells_users_how_to_remove_pudge() -> None:
    # policy: users follow README's removal instructions; the section must stay.
    assert "## remove pudge" in source("README.md").casefold()

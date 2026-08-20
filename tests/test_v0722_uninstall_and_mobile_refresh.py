from __future__ import annotations

from pathlib import Path

from pudge.uninstall import build_uninstall_plan, launch_uninstaller, render_uninstall_script


ROOT = Path(__file__).parents[1]


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


def test_uninstaller_is_launched_as_a_detached_private_script(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_settings_expose_a_red_double_confirmed_uninstall_action() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    backend = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    confirm = (ROOT / "pudge" / "web" / "pudge_confirm.js").read_text(encoding="utf-8")

    assert 'id="uninstallPudge"' in html
    assert "Delete Pudge from this Mac" in html
    assert "Удалить Pudge с этого Mac" in html
    assert 'class="uninstall-pudge"' in html
    assert "pywebview.api.uninstall_pudge()" in html
    assert html.count("pudgeConfirm(finalWarning") == 1
    assert "def uninstall_pudge" in backend
    assert "options?.danger" in confirm
    assert "confirm.danger" in confirm


def test_companion_refreshes_visible_library_and_uses_new_shell_cache() -> None:
    companion = ROOT / "pudge" / "web" / "companion"
    app = (companion / "app.js").read_text(encoding="utf-8")
    html = (companion / "index.html").read_text(encoding="utf-8")
    service_worker = (companion / "sw.js").read_text(encoding="utf-8")

    assert "PUDGE_COMPANION_LIVE_SYNC_V15" in app
    assert "refreshVisibleLibrary" in app
    assert "loadLibrary().catch(() => {})" in app
    assert "window.addEventListener('focus', refreshVisibleLibrary)" in app
    assert "window.setInterval(refreshVisibleLibrary, 15000)" in app
    assert "app.js?v=15" in html
    assert "styles.css?v=15" in html
    assert "pudge-companion-shell-v15" in service_worker


def test_source_documentation_uses_human_initial_setup_language() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "DEVELOPMENT.md",
        ROOT / "MOBILE_SYNC_PROTOCOL.md",
        ROOT / "RELEASING.md",
        ROOT / "SECURITY.md",
        ROOT / "docs" / "ALGORITHMS.md",
        ROOT / "docs" / "USER_GUIDE.md",
        ROOT / "docs" / "VN_READER_DESIGN.md",
        ROOT / "pudge" / "THIRD_PARTY_NOTICES.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents).casefold()

    assert "first experience" not in combined
    assert "first-experience" not in combined
    assert "first run" not in combined
    assert "first-run" not in combined
    assert "## remove pudge" in (ROOT / "README.md").read_text(encoding="utf-8").casefold()

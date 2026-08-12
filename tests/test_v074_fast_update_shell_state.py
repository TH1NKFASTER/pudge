from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fast_update_shared_shell_state_is_defined_before_rebuild_gate() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    gate = installer.index("if (( REBUILD_APP )); then")
    lsregister = installer.index(
        'LSREGISTER="/System/Library/Frameworks/CoreServices.framework/'
        'Frameworks/LaunchServices.framework/Support/lsregister"'
    )
    assert lsregister < gate


def test_fast_update_does_not_require_pyinstaller_branch_for_launchservices() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if (( FAST_UPDATE )) && [[ -f "$LAUNCHER_RUNTIME_MARKER" ]]; then' in installer
    assert '"$LSREGISTER"' in installer

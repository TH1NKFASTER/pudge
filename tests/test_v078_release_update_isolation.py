from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_updater_invokes_extracted_installer_via_zsh() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert '"if ! /bin/zsh ./install.sh --update; then",' in updater
    assert '"if ! ./install.sh --update; then",' not in updater


def test_installer_verifies_managed_package_in_isolated_python() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert (
        'EXPECTED_VERSION="$EXPECTED_VERSION" "$VENV_DIR/bin/python" -I - '
        "<<'PYVERIFY'"
    ) in installer

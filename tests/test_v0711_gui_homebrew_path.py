from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_finds_homebrew_without_login_shell_path() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "/opt/homebrew/bin/brew" in installer
    assert "/usr/local/bin/brew" in installer
    assert 'export PATH="${BREW_BIN:h}:$PATH"' in installer


def test_homebrew_path_is_fixed_before_formula_checks() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    export = installer.index('export PATH="${BREW_BIN:h}:$PATH"')
    formulas = installer.index("for formula in mpv ffmpeg alass")
    assert export < formulas

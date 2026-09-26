from __future__ import annotations

from pathlib import Path

import pudge.first_experience as first


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
    monkeypatch.setattr(first, "_run_jiten_mpv_installer", lambda: (_ for _ in ()).throw(AssertionError("installer ran during check")))

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


def test_settings_use_two_step_check_then_update_button() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "Проверить обновление JitenMPV" in html
    assert "Обновить JitenMPV" in html
    assert "Установить JitenMPV" in html
    assert "mode==='check'" in html
    assert "pywebview.api.jiten_mpv_check_update()" in html
    assert "pywebview.api.jiten_mpv_update()" in html
    assert "def jiten_mpv_check_update(" in web_app
    assert "def jiten_mpv_update(" in web_app

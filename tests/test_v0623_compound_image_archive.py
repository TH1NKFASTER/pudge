from __future__ import annotations

import os
from pathlib import Path

from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import find_7zip, materialize_jimaku_files


class FakeJimakuClient:
    def __init__(self, archive: Path):
        self.archive = archive

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        return self.archive


def _fake_7zz(tmp_path: Path, payload: Path) -> Path:
    tool = tmp_path / "7zz-outside-path"
    tool.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in -o*) out=${arg#-o} ;; esac\n"
        "done\n"
        "mkdir -p \"$out\"\n"
        f"cp {str(payload)!r} \"$out/Infinity Castle Japanese.sup\"\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


def test_find_7zip_uses_app_injected_absolute_path(tmp_path: Path, monkeypatch) -> None:
    tool = tmp_path / "7zz"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PUDGE_7ZIP", str(tool))
    monkeypatch.setenv("PATH", "")

    assert find_7zip() == str(tool)


def test_sup_7z_is_extracted_when_finder_app_path_lacks_homebrew(
    tmp_path: Path, monkeypatch
) -> None:
    archive = tmp_path / "Demon.Slayer.Infinity.Castle.sup.7z"
    archive.write_bytes(b"fake archive")
    payload = tmp_path / "payload.sup"
    payload.write_bytes(b"PG subtitle payload")
    tool = _fake_7zz(tmp_path, payload)
    monkeypatch.setenv("PUDGE_7ZIP", str(tool))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    item = JimakuFile(
        url="https://example.test/movie.sup.7z",
        name=(
            "Demon.Slayer.Kimetsu.no.Yaiba.Infinity.Castle.2025."
            "1080p.BDRip.AAC5.1.10bits.x265-Rapta.sup.7z"
        ),
        size=1,
        last_modified="",
        score=70.0,
    )
    candidates = materialize_jimaku_files(
        FakeJimakuClient(archive),  # type: ignore[arg-type]
        item,
        VideoIdentity(
            title="Demon Slayer: Kimetsu no Yaiba - Infinity Castle",
            year=2025,
        ),
        tmp_path / "Demon Slayer Infinity Castle.mkv",
        tmp_path / "cache",
    )

    assert len(candidates) == 1
    assert candidates[0].path.suffix.casefold() == ".sup"
    assert candidates[0].verified_japanese is True


def test_installer_injects_sevenzip_into_app_and_agent() -> None:
    installer = Path("install.sh").read_text(encoding="utf-8")
    assert 'SEVENZIP_BIN="$(brew --prefix sevenzip)/bin/7zz"' in installer
    assert "PUDGE_7ZIP" in installer
    assert "sevenzip_path = os.environ[\"SEVENZIP_BIN\"]" in installer

"""Pudge took gigabytes it did not need: installer caches, stale packages, 7-Zip, caches without limits.

Measured on a user's Mac, and what happens now:

1. pip (~/Library/Caches/pip, 802 MB) and uv (~/.cache/uv, 3.58 GB) kept every download of
   install.sh, the Mokuro installer and the Irodori installer. Every `pip install` now runs
   with --no-cache-dir; uv gets a cache folder under Pudge's cache folder for that one install,
   deleted when it ends (also after a failure, and a crashed install's folder on the next run).
2. The managed venv kept transformers, unidic_lite, tokenizers ... of an older Pudge: install.sh
   only installed and upgraded. After a locked install (release-requirements.txt or the
   requirements exported from uv.lock) it removes every package those requirements do not list
   (PEP 503 names; pip, setuptools, wheel and Pudge stay), with one summary line.
3. Homebrew's python-tk@3.12 was installed, though nothing uses tkinter any more.
4. Homebrew's sevenzip was installed only for Jimaku (and Light Novel) .7z/.rar archives. macOS's
   own /usr/bin/bsdtar reads them; 7zz is only a fallback when bsdtar fails and 7zz is there.
   The size limit and timeout apply to both, links in an archive are never followed, and an
   encrypted or unreadable archive gets a clear message. --doctor no longer reports a missing 7zz.
5. Cache folders without a limit (jimaku 834 MB, segment-subs, timing-reference,
   timeline-alignment, the removed OCR engine's manga-text-regions) are now bounded like the
   other derived caches; files a library episode still uses stay. Expensive results
   (reading-audio alignments, Mokuro OCR) are never evicted, audiobook Whisper transcripts only
   when no book used them for half a year (test_fix_audiobook_transcripts_kept_and_aged.py); manga OCR
   files are now written gzip-compressed (.json.gz) and old .json ones are still read.

install.sh runs under zsh with the stand-ins of tests/t71g01_support.py; archive tools are small
shell scripts; no network.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.cache_management import enforce_derived_caches
from pudge.database import Database
from pudge.i18n import err, t
from pudge.manager_models import LibraryEpisode
from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import JimakuClient, jimaku_download_dir, materialize_jimaku_files
from t71g01_support import MacSim, stand_in
from test_fix_locked_dev_installs import UV_STANDIN, _checkout_with_lock
from test_fix_mokuro_irodori_tested_versions import _update as _update_mokuro
from test_fix_mokuro_irodori_tested_versions import irodori as irodori_fixture
from test_fix_mokuro_irodori_tested_versions import mokuro as mokuro_fixture
from test_v089_irodori_managed_install import _api

mokuro = mokuro_fixture
irodori = irodori_fixture

MB = 1024 * 1024
DAY = 24 * 3600.0
JAPANESE_SRT = "".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n今日はいい天気ですね。\n\n" for i in range(1, 30)
)


# ---------------------------------------------------------------------------------------
# 1, 2. install.sh: no pip/uv cache left behind; packages no longer needed are removed.
def _pip_installs(result) -> list[list[str]]:
    return [call for call in result.calls("pip") if call[:1] == ["install"]]


def _dist(site: Path, name: str, version: str = "1.0") -> Path:
    """An installed distribution: its dist-info and one module file."""
    module = site / name.replace("-", "_").lower() / "__init__.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("", encoding="utf-8")
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    (info / "RECORD").write_text(
        f"{module.relative_to(site).as_posix()},,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n",
        encoding="utf-8",
    )
    return info


def _uv_cache(sim: MacSim) -> Path:
    return sim.home / "Library/Caches/pudge/uv-install-cache"


def test_release_install_leaves_no_pip_cache_and_removes_packages_no_longer_needed(tmp_path) -> None:
    sim = MacSim(tmp_path / "sim")
    first = sim.install(sim.project("9.8.7", version="9.8.7"))
    assert first.returncode == 0, first.output
    assert _pip_installs(first) and all("--no-cache-dir" in call for call in _pip_installs(first))
    assert "no longer needed" not in first.output  # a new venv has nothing extra

    # What an older Pudge left in the venv, and what the new requirements still list.
    for name in ("transformers", "unidic-lite", "tokenizers", "httpx", "typing-extensions"):
        _dist(sim.site, name)
    for name in ("pip", "setuptools", "wheel"):
        _dist(sim.site, name)
    release = sim.project("9.8.8", version="9.8.8")
    (release / "release-requirements.txt").write_text(
        "httpx==0.28.1 \\\n    --hash=sha256:00\n"
        "typing_extensions==4.12.2 ; python_version < '3.13' \\\n    --hash=sha256:00\n"
        "    # via httpx\n",
        encoding="utf-8",
    )

    update = sim.install(release, "--update")

    assert update.returncode == 0, update.output
    assert all("--no-cache-dir" in call for call in _pip_installs(update))
    assert [call for call in update.calls("pip") if call[:1] == ["uninstall"]] == [
        ["uninstall", "--quiet", "--yes", "tokenizers", "transformers", "unidic-lite"]
    ]
    assert update.output.count("Removed 3 packages no longer needed.") == 1
    left = sorted(info.name.split("-")[0] for info in sim.site.glob("*.dist-info"))
    assert left == ["httpx", "pip", "pudge", "setuptools", "typing_extensions", "wheel"]
    assert not (sim.site / "transformers").exists()
    # Uninstall comes after the locked install and before Pudge's own wheel.
    assert (
        update.index("pip", "install", "--quiet", "--no-cache-dir", "--upgrade", "--require-hashes")
        < update.index("pip", "uninstall")
        < update.index("pip", "install", "--no-cache-dir", "--no-deps")
    )


def test_unlocked_fallback_install_removes_nothing(tmp_path) -> None:
    sim = MacSim(tmp_path / "sim")
    assert sim.install(sim.project("9.8.7", version="9.8.7")).returncode == 0
    _dist(sim.site, "transformers")
    release = sim.project("9.8.8", version="9.8.8")
    (release / "release-requirements.txt").unlink()  # no lock: resolved from the wheel

    update = sim.install(release, "--update")

    assert update.returncode == 0, update.output
    assert [call for call in _pip_installs(update) if any(arg.endswith(".whl[sync]") for arg in call)]
    assert all("--no-cache-dir" in call for call in _pip_installs(update))
    assert not [call for call in update.calls("pip") if call[:1] == ["uninstall"]]
    assert (sim.site / "transformers").is_dir()


UV_RECORDING = UV_STANDIN + (
    'record uv-cache "${UV_CACHE_DIR:-}"\n'
    '[ -e "$UV_CACHE_DIR/leftover" ] && record uv-saw-leftover\n'
    '[ -n "${UV_CACHE_DIR:-}" ] && mkdir -p "$UV_CACHE_DIR/archive-v0" && echo cached > "$UV_CACHE_DIR/archive-v0/wheel"\n'
)


def test_checkout_install_gives_uv_a_temporary_cache_and_removes_unlisted_packages(tmp_path) -> None:
    sim = MacSim(tmp_path / "sim")
    checkout = _checkout_with_lock(sim)
    stand_in(sim.base / "uv-standin", UV_RECORDING)
    first = sim.install(checkout)
    assert first.returncode == 0, first.output
    assert first.calls("uv-cache") == [[str(_uv_cache(sim))]]
    assert not _uv_cache(sim).exists()
    assert all("--no-cache-dir" in call for call in _pip_installs(first))

    _dist(sim.site, "transformers")
    (_uv_cache(sim) / "leftover").mkdir(parents=True)  # an install that crashed
    second = sim.install(checkout, "--update")

    assert second.returncode == 0, second.output
    assert second.calls("uv-saw-leftover") == []
    assert not _uv_cache(sim).exists()
    assert [call for call in second.calls("pip") if call[:1] == ["uninstall"]] == [
        ["uninstall", "--quiet", "--yes", "transformers"]
    ]
    assert "Removed 1 package no longer needed." in second.output


def test_a_failed_uv_step_removes_its_cache_too(tmp_path) -> None:
    sim = MacSim(tmp_path / "sim")
    checkout = _checkout_with_lock(sim)
    stand_in(
        sim.base / "uv-standin",
        '[ -n "${UV_CACHE_DIR:-}" ] && mkdir -p "$UV_CACHE_DIR/archive-v0"\n'
        'echo "error: The lockfile needs to be updated" >&2\nexit 2\n',
    )
    (_uv_cache(sim) / "leftover").mkdir(parents=True)  # an install that crashed
    failed = sim.install(checkout)
    assert failed.returncode != 0
    assert "The lockfile needs to be updated" in failed.output
    assert not _uv_cache(sim).exists()


# ---------------------------------------------------------------------------------------
# 1. Mokuro and Irodori installers.
def test_mokuro_install_and_update_keep_no_pip_cache(mokuro) -> None:
    api, stand_in_ = mokuro
    api._run_mokuro_ocr_install()
    _update_mokuro(api)
    installs = [command for command in stand_in_.calls if command[1:4] == ["-m", "pip", "install"]]
    assert len(installs) == 2  # the install and the update's new environment
    assert all(command[4] == "--no-cache-dir" for command in installs)


def test_irodori_install_gives_uv_a_temporary_cache_under_pudges_cache(irodori, monkeypatch) -> None:
    api, stand_in_ = irodori
    cache = api._irodori_uv_cache_dir()
    assert cache.parent == Path(api.config.paths.cache_dir).expanduser()
    (cache / "leftover").mkdir(parents=True)  # an install that crashed
    seen: list[tuple[str, bool]] = []
    run = stand_in_.run

    def recording(command, **kwargs):
        if str(command[0]) == str(stand_in_.uv):
            folder = Path(kwargs["env"]["UV_CACHE_DIR"])
            seen.append((str(folder), (folder / "leftover").exists()))
            (folder / "archive-v0").mkdir(parents=True)  # what uv downloads
        return run(command, **kwargs)

    monkeypatch.setattr(web_app_module.subprocess, "run", recording)
    api.install_irodori_tts()
    api._irodori_install_thread.join(30)
    assert api.irodori_install_status()["state"] == "ready"
    assert seen == [(str(cache), False)]
    assert not cache.exists()

    def failing(command, **kwargs):
        if str(command[0]) == str(stand_in_.uv):
            Path(kwargs["env"]["UV_CACHE_DIR"], "archive-v0").mkdir(parents=True)
            return SimpleNamespace(returncode=2)
        return run(command, **kwargs)

    monkeypatch.setattr(web_app_module.subprocess, "run", failing)
    api.install_irodori_tts()
    api._irodori_install_thread.join(30)
    assert api.irodori_install_status()["state"] == "failed"
    assert not cache.exists()


def test_the_uv_irodori_bootstraps_is_installed_without_pip_cache(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(web_app_module.shutil, "which", lambda _name: None)
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        command = [str(part) for part in command]
        commands.append(command)
        root = api._irodori_install_root() / "uv-bootstrap" / "bin"
        root.mkdir(parents=True, exist_ok=True)
        (root / ("uv" if "pip" in command else "python")).write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_app_module.subprocess, "run", run)
    with open(tmp_path / "log", "w", encoding="utf-8") as log:
        api._irodori_uv(log)
    [pip] = [command for command in commands if "pip" in command]
    assert pip[1:] == ["-m", "pip", "install", "--no-cache-dir", "--upgrade", "uv"]


# ---------------------------------------------------------------------------------------
# 3. No python-tk, no sevenzip from Homebrew: tests/test_install_script.py
#    (test_install_needs_neither_7zip_nor_tk) and tests/test_v0711_gui_homebrew_path.py.


# ---------------------------------------------------------------------------------------
# 4. .7z/.rar archives: bsdtar first, 7zz only as a fallback.
class _Client:
    def __init__(self, archive: Path) -> None:
        self.archive = archive

    def download(self, _item, _cache_dir):
        return self.archive


def _tool(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$(basename "$0") $* LC_ALL=$LC_ALL" >> "$FAKE_TOOL_LOG"\n'
        'out=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-C" ] && out="$arg"; case "$arg" in -o*) out=${arg#-o};; esac; prev="$arg"; done\n'
        + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """No archive tool by default; `bsdtar(body)` / `sevenzip(body)` install one; `log()` lists their runs."""
    log = tmp_path / "tools.log"
    log.touch()
    payload = tmp_path / "payload.srt"
    payload.write_text(JAPANESE_SRT, encoding="utf-8")
    monkeypatch.setenv("FAKE_TOOL_LOG", str(log))
    monkeypatch.setenv("FAKE_PAYLOAD", str(payload))
    monkeypatch.setattr("pudge.providers.jimaku.find_bsdtar", lambda: None)
    monkeypatch.setattr("pudge.providers.jimaku.find_7zip", lambda: None)

    def bsdtar(body: str) -> Path:
        path = _tool(tmp_path / "bin" / "bsdtar", body)
        monkeypatch.setattr("pudge.providers.jimaku.find_bsdtar", lambda: str(path))
        return path

    def sevenzip(body: str) -> Path:
        path = _tool(tmp_path / "bin" / "7zz", body)
        monkeypatch.setattr("pudge.providers.jimaku.find_7zip", lambda: str(path))
        return path

    return SimpleNamespace(
        bsdtar=bsdtar,
        sevenzip=sevenzip,
        log=lambda: log.read_text(encoding="utf-8").splitlines(),
        payload=payload,
    )


def _jimaku(tmp_path: Path, name: str = "Show - 01.7z") -> list:
    folder = tmp_path / "cache" / "jimaku" / "0123456789abcdef"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / name
    archive.write_bytes(b"7z archive")
    item = JimakuFile(url="https://jimaku.invalid/" + name, name=name, size=1, last_modified="", score=70.0)
    return materialize_jimaku_files(
        _Client(archive),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Show"),
        tmp_path / "Show - 01.mkv",
        tmp_path / "cache",
    )


COPY_PAYLOAD = 'cp "$FAKE_PAYLOAD" "$out/Show - 01.srt"\n'


def test_jimaku_archive_is_unpacked_by_bsdtar_without_7zip(tmp_path, tools) -> None:
    tools.bsdtar(COPY_PAYLOAD)

    [candidate] = _jimaku(tmp_path)

    assert candidate.verified_japanese and candidate.path.read_text(encoding="utf-8") == JAPANESE_SRT
    [run] = tools.log()
    archive = tmp_path / "cache/jimaku/0123456789abcdef/Show - 01.7z"
    assert run.startswith(f"bsdtar -xf {archive} -C ")
    # Japanese member names need a UTF-8 locale (a Finder-launched app has none).
    assert run.endswith("LC_ALL=en_US.UTF-8")


def test_7zz_is_the_fallback_when_bsdtar_cannot_read_the_archive(tmp_path, tools) -> None:
    tools.bsdtar('touch "$out/partial.srt"\necho "bsdtar: Unrecognized archive format" >&2\nexit 1\n')
    tools.sevenzip(COPY_PAYLOAD)

    [candidate] = _jimaku(tmp_path)

    assert candidate.name == "Show - 01.srt"
    assert [line.split()[0] for line in tools.log()] == ["bsdtar", "7zz"]
    assert not any("partial" in path.name for path in candidate.path.parent.iterdir())


def test_encrypted_or_unreadable_archives_get_a_clear_message(tmp_path, tools, capsys) -> None:
    tools.bsdtar('echo "bsdtar: Encrypted file is unsupported" >&2\nexit 1\n')
    assert _jimaku(tmp_path / "a") == []
    assert (
        t("error.jimaku.extractFailedDetail", file="Show - 01.7z", error=err("archive.encrypted"))
        in capsys.readouterr().out
    )

    tools.bsdtar('echo "bsdtar: Unrecognized archive format" >&2\nexit 1\n')
    tools.sevenzip('echo "ERROR: Cannot open the file as archive"\nexit 2\n')
    assert _jimaku(tmp_path / "b", "Show - 01.rar") == []
    assert (
        t("error.jimaku.extractFailedDetail", file="Show - 01.rar", error=err("archive.unreadable"))
        in capsys.readouterr().out
    )


def test_without_any_archive_tool_the_message_names_both(tmp_path, tools, capsys) -> None:
    assert _jimaku(tmp_path) == []
    assert t("error.jimaku.sevenZipMissing") in capsys.readouterr().out
    assert "bsdtar" in t("error.jimaku.sevenZipMissing", lang="en")


def test_a_link_in_the_archive_is_never_followed(tmp_path, tools) -> None:
    secret = tmp_path / "private" / "notes.srt"
    secret.parent.mkdir()
    secret.write_text(JAPANESE_SRT, encoding="utf-8")
    tools.bsdtar(f'ln -s "{secret}" "$out/Show - 01.srt"\n')

    assert _jimaku(tmp_path) == []
    assert [
        path.name for path in (tmp_path / "cache/jimaku/0123456789abcdef").iterdir() if path.suffix == ".srt"
    ] == []


def test_bsdtar_is_stopped_at_the_size_limit_and_7zz_is_not_tried(
    tmp_path, tools, monkeypatch, capsys
) -> None:
    tools.payload.write_text(JAPANESE_SRT + ";" * 200_000, encoding="utf-8")
    tools.bsdtar(COPY_PAYLOAD + "sleep 5\n")
    tools.sevenzip(COPY_PAYLOAD)
    monkeypatch.setattr("pudge.providers.jimaku.SUBTITLE_ARCHIVE_LIMIT", 64 * 1024, raising=False)

    started = time.monotonic()
    assert _jimaku(tmp_path) == []
    assert time.monotonic() - started < 4.0
    assert [line.split()[0] for line in tools.log()] == ["bsdtar"]
    assert "unpacks to more than" in capsys.readouterr().out


def test_light_novel_rar_batch_is_unpacked_by_bsdtar(tmp_path, tools) -> None:
    from test_v072_planning_reader_nyaa_cards import _config

    from pudge.light_novels import LightNovelError, LightNovelService

    service = LightNovelService(_config(tmp_path))
    archive = service.root / "Ookami vol 01-22.rar"
    archive.write_bytes(b"rar")
    # The batch is listed first and only the volume's own file is unpacked.
    tools.bsdtar(
        'if [ "$1" = "-tf" ]; then printf "Ookami 01.epub\\nOokami 02.epub\\n"; exit 0; fi\n'
        'printf first > "$out/Ookami 01.epub"\nprintf second > "$out/Ookami 02.epub"\n'
    )
    row = {"index": 0, "name": archive.name, "priority": 6, "progress": 1.0}

    assert service._materialize_selected_volume([row], 2) == [archive.resolve()]
    assert (service.root / "Ookami 02.epub").read_bytes() == b"second"
    assert not (service.root / "Ookami 01.epub").exists()
    assert tools.log()[-1].split(" -C ")[1].split(" LC_ALL=")[0].endswith(" Ookami 02.epub")

    tools.bsdtar('echo "bsdtar: Encrypted file is unsupported" >&2\nexit 1\n')
    with pytest.raises(LightNovelError) as error:
        service._materialize_selected_volume([row], 3)
    assert str(error.value) == err(
        "ln.extractFailedDetail", name=archive.name, error=err("archive.encrypted")
    )


def test_doctor_reports_bsdtar_and_no_missing_7zip(tmp_path, monkeypatch, capsys) -> None:
    from test_v072_planning_reader_nyaa_cards import _config

    import pudge.cli as cli

    bsdtar = _tool(tmp_path / "bin" / "bsdtar", "exit 0\n")
    monkeypatch.setenv("PUDGE_BSDTAR", str(bsdtar))
    monkeypatch.setattr(cli, "find_7zip", lambda: None)
    config = _config(tmp_path)
    config.tools.mpv = config.tools.ffmpeg = config.tools.ffprobe = str(tmp_path / "missing")
    config.qbittorrent.enabled = config.aria2.enabled = config.llm.enabled = False

    cli.doctor(config)

    out = capsys.readouterr().out
    assert f"Archive tool (.7z/.rar): {bsdtar} (ok)" in out
    assert "7-Zip" not in out


def test_energy_diagnostics_counts_bsdtar_as_an_archive_worker() -> None:
    from pudge.energy_diagnostics import EnergyDiagnosticsMonitor

    assert EnergyDiagnosticsMonitor._process_role("/usr/bin/bsdtar -xf /tmp/subs.7z -C /tmp/x") == (
        "archive-worker"
    )


# ---------------------------------------------------------------------------------------
# 5. Cache folders: limits for what is cheap to make again, nothing for expensive results.
def _aged(path: Path, age: float, data: bytes = b"x", *, size: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if size:
        with path.open("r+b") as handle:  # sparse: a large size without the disk space
            handle.truncate(size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _aged_folder(folder: Path, age: float, files: dict[str, int]) -> Path:
    for name, size in files.items():
        _aged(folder / name, age, size=size)
    stamp = time.time() - age
    os.utime(folder, (stamp, stamp))
    return folder


def test_cheap_cache_folders_are_bounded_and_expensive_results_stay(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    cache = tmp_path / "cache"
    video = tmp_path / "library" / "Show - 01.mkv"
    _aged(video, 0)
    subtitle = _aged(cache / "synced" / "live.srt", 0)
    db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, subtitle_path=subtitle)
    )
    jimaku = cache / "jimaku"
    in_use = _aged_folder(jimaku / "aaaa", 400 * DAY, {"Show.7z": 10, "1234_Show - 01.srt": 10})
    stale = _aged_folder(jimaku / "bbbb", 120 * DAY, {"Old.7z": 10})
    older = _aged_folder(jimaku / "cccc", 3 * DAY, {"Batch.rar": 300 * MB})
    newer = _aged_folder(jimaku / "dddd", 2 * DAY, {"Batch2.rar": 300 * MB})
    _aged(
        cache / "final-pipeline" / "live.json",
        0,
        json.dumps(
            {"video": str(video), "subtitle": str(subtitle), "raw_source": str(in_use / "1234_Show - 01.srt")}
        ).encode(),
    )
    old_reference = _aged(cache / "timing-reference" / "a.srt", 100 * DAY)
    new_reference = _aged(cache / "timing-reference" / "b.srt", DAY)
    old_timeline = _aged(cache / "timeline-alignment" / "a.srt", 200 * DAY)
    new_timeline = _aged(cache / "timeline-alignment" / "b.srt", DAY)
    old_segments = _aged_folder(cache / "segment-subs" / "aaaa", 40 * DAY, {"start.srt": 10})
    new_segments = _aged_folder(cache / "segment-subs" / "bbbb", DAY, {"start.srt": 10})
    regions = _aged(cache / "manga-text-regions" / "tmp1234.png", 2 * DAY)
    # Hours of alignment and Mokuro work: old and large, still kept.
    expensive = [
        _aged(cache / "reading-audio-alignment" / "b.json", 900 * DAY, size=400 * MB),
        _aged(cache / "manga-ocr" / "artifacts" / "c-regions-v96p27.json.gz", 900 * DAY, size=500 * MB),
    ]

    report = enforce_derived_caches(db, cache, force=True)

    assert in_use.is_dir() and (in_use / "Show.7z").is_file()  # an episode's subtitle came from it
    assert not stale.exists()  # older than 90 days
    assert not older.exists() and newer.is_dir()  # over 512 MB: least recently used goes
    assert not old_reference.exists() and new_reference.exists()
    assert not old_timeline.exists() and new_timeline.exists()
    assert not old_segments.exists() and new_segments.is_dir()
    assert not regions.exists()
    assert all(path.exists() for path in expensive)
    assert report["folders"]["jimaku"]["removed"] == 2
    assert {"reading-audio-alignment", "manga-ocr"}.isdisjoint(report["folders"])


def test_a_reused_jimaku_download_counts_as_recently_used(tmp_path) -> None:
    cache = tmp_path / "cache"
    url = "https://jimaku.invalid/Show.7z"
    item = JimakuFile(url=url, name="Show.7z", size=3, last_modified="")
    folder = jimaku_download_dir(cache, item)  # the URL, date and size key the folder
    archive = _aged(folder / "Show.7z", 80 * DAY, b"7z!")
    os.utime(folder, (time.time() - 80 * DAY,) * 2)
    archive_mtime = archive.stat().st_mtime_ns
    client = object.__new__(JimakuClient)

    assert client.download(item, cache) == archive

    assert time.time() - folder.stat().st_mtime < 60
    # The archive itself is untouched: its mtime keys the unpacked members' manifest.
    assert archive.stat().st_mtime_ns == archive_mtime


def test_manga_ocr_is_written_compressed_and_old_json_is_still_read(tmp_path) -> None:
    from test_fix_manga_pudge_ocr_removed import _mokuro_regions, _publish, _volume

    from pudge.manga import MangaService
    from pudge.manga_ocr_artifact import read_artifact

    db, cache = Database(tmp_path / "pudge.db"), tmp_path / "cache"
    service = MangaService(db, cache_dir=cache)
    book = _volume(tmp_path, service, "vol", pages=2)
    _publish(service, book, {0: _mokuro_regions("うみ")})
    artifact = service.ocr_artifact(book)
    path = service._ocr_artifact_path(book)
    assert path.name.endswith("-regions-v96p27.json.gz")
    assert json.loads(gzip.decompress(path.read_bytes())) == artifact

    # What an older Pudge left: the same OCR as plain JSON.
    legacy = path.with_name(path.name[: -len(".gz")])
    legacy.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    path.unlink()
    service = MangaService(db, cache_dir=cache)
    assert service._load_ocr_artifact(book, read_only=True) == artifact
    assert read_artifact(path) == artifact

    # The next write replaces it with the compressed file.
    _publish(service, book, {1: _mokuro_regions("そら")})
    service.ocr_artifact(book)
    assert path.is_file() and not legacy.exists()
    assert [page["page_index"] for page in read_artifact(path)["pages"]] == [0, 1]

    # Removing the volume removes its OCR, old or new.
    legacy.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    service.remove_books([book])
    assert not path.exists() and not legacy.exists()

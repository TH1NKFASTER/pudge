from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pudge.cli as cli
from pudge.config import AppConfig, MatchingConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.manager_models import LibraryEpisode
from pudge.models import JimakuFile, VideoIdentity
from pudge.pipeline_cache import load_final_pipeline_result
from pudge.providers.jimaku import materialize_jimaku_files


class FakeJimakuClient:
    def __init__(self, archive: Path):
        self.archive = archive

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        return self.archive


def _jimaku_item(name: str) -> JimakuFile:
    return JimakuFile(
        url="https://example.test/" + name,
        name=name,
        size=1,
        last_modified="",
        score=70.0,
    )


def test_v37_single_member_sup_cache_bootstraps_without_7zip(
    tmp_path: Path, monkeypatch
) -> None:
    archive = tmp_path / "Demon.Slayer.Infinity.Castle.sup.7z"
    archive.write_bytes(b"already downloaded archive")
    display_name = archive.stem
    digest = hashlib.sha1(display_name.encode("utf-8")).hexdigest()[:8]
    extracted = tmp_path / f"{digest}_{display_name}"
    extracted.write_bytes(b"persistent PGS payload")

    monkeypatch.setattr(
        "pudge.providers.jimaku.find_7zip",
        lambda: (_ for _ in ()).throw(AssertionError("7zz must not run")),
    )

    candidates = materialize_jimaku_files(
        FakeJimakuClient(archive),  # type: ignore[arg-type]
        _jimaku_item(archive.name),
        VideoIdentity(title="Demon Slayer Infinity Castle"),
        tmp_path / "Demon Slayer Infinity Castle.mkv",
        tmp_path / "cache",
    )

    assert len(candidates) == 1
    assert candidates[0].path == extracted
    manifests = list(tmp_path.glob(".*.extract-v1.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["members"][0]["output_name"] == extracted.name


def test_7z_manifest_reuses_nested_member_without_second_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    archive = tmp_path / "movie.7z"
    archive.write_bytes(b"fake archive")
    payload = tmp_path / "payload.sup"
    payload.write_bytes(b"PG subtitle payload")
    tool = tmp_path / "7zz"
    marker = tmp_path / "runs.txt"
    tool.write_text(
        "#!/bin/sh\n"
        "echo run >> \"$FAKE_7Z_MARKER\"\n"
        "for arg in \"$@\"; do case \"$arg\" in -o*) out=${arg#-o} ;; esac; done\n"
        "mkdir -p \"$out/nested\"\n"
        "cp \"$FAKE_7Z_PAYLOAD\" \"$out/nested/Infinity Castle Japanese.sup\"\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_7Z_PAYLOAD", str(payload))
    monkeypatch.setenv("FAKE_7Z_MARKER", str(marker))
    monkeypatch.setenv("PUDGE_7ZIP", str(tool))

    args = (
        FakeJimakuClient(archive),  # type: ignore[arg-type]
        _jimaku_item(archive.name),
        VideoIdentity(title="Demon Slayer Infinity Castle"),
        tmp_path / "Demon Slayer Infinity Castle.mkv",
        tmp_path / "cache",
    )
    first = materialize_jimaku_files(*args)
    assert len(first) == 1
    assert marker.read_text(encoding="utf-8").splitlines() == ["run"]

    monkeypatch.setattr(
        "pudge.providers.jimaku.find_7zip",
        lambda: (_ for _ in ()).throw(AssertionError("manifest cache missed")),
    )
    second = materialize_jimaku_files(*args)

    assert [item.path for item in second] == [item.path for item in first]
    assert marker.read_text(encoding="utf-8").splitlines() == ["run"]


def test_bitmap_ocr_is_enabled_by_default_from_v38() -> None:
    assert MatchingConfig().ocr_image_subtitles is True


def test_manager_reuses_only_existing_waiting_bitmap(tmp_path: Path) -> None:
    bitmap = tmp_path / "aligned.sup"
    bitmap.write_bytes(b"PGS")
    item = LibraryEpisode(
        media_id=1,
        title="Movie",
        episode=None,
        video_path=tmp_path / "movie.mkv",
        subtitle_path=bitmap,
        state="waiting_text_subtitles",
    )

    assert AnimeManager._prepared_bitmap_retry_path(item) == bitmap
    item.state = "ready"
    assert AnimeManager._prepared_bitmap_retry_path(item) is None
    item.state = "waiting_text_subtitles"
    bitmap.unlink()
    assert AnimeManager._prepared_bitmap_retry_path(item) is None


def test_prepared_bitmap_retry_ocr_writes_final_cache_and_next_run_hits_it(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "Demon Slayer Infinity Castle.mkv"
    video.write_bytes(b"video")
    bitmap = tmp_path / "aligned.sup"
    bitmap.write_bytes(b"PGS")
    ocr_srt = tmp_path / "ocr.srt"
    ocr_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは。これは日本語です。\n",
        encoding="utf-8",
    )
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.matching.ocr_image_subtitles = True

    calls: list[Path] = []

    def fake_ocr(_video, _cache, **kwargs):
        calls.append(Path(kwargs["subtitle_path"]))
        return ocr_srt, {
            "reason": "ocr_ready",
            "cue_count": 1,
            "quality": {"accepted": True, "status": "accepted", "warnings": []},
        }

    monkeypatch.setattr(cli, "image_subtitle_to_srt", fake_ocr)
    args = cli.build_parser().parse_args(
        [
            "--prepare-only",
            "--offline",
            "--no-anilist-progress",
            "--prepared-bitmap-sub",
            str(bitmap),
            str(video),
        ]
    )

    assert cli.process_video(video, args, cfg, None) == 0
    assert calls == [bitmap.resolve()]
    cached = load_final_pipeline_result(video, cfg)
    assert cached is not None
    assert cached["source"] == "ocr"
    assert Path(str(cached["subtitle"])).is_file()

    monkeypatch.setattr(
        cli,
        "image_subtitle_to_srt",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("final cache missed")),
    )
    assert cli.process_video(video, args, cfg, None) == 0


def test_7zip_is_reported_as_archive_worker() -> None:
    assert (
        EnergyDiagnosticsMonitor._process_role(
            "/opt/homebrew/opt/sevenzip/bin/7zz x -y /tmp/subs.sup.7z"
        )
        == "archive-worker"
    )


def test_legacy_false_ocr_default_migrates_to_enabled_but_explicit_opt_out_survives(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("[matching]\nocr_image_subtitles = false\n", encoding="utf-8")
    assert load_config(legacy).matching.ocr_image_subtitles is True

    explicit = AppConfig()
    explicit.matching.ocr_image_subtitles = False
    explicit.matching.ocr_image_subtitles_disabled_by_user = True
    explicit_path = tmp_path / "explicit.toml"
    write_config(explicit, explicit_path)
    loaded = load_config(explicit_path)
    assert loaded.matching.ocr_image_subtitles is False
    assert loaded.matching.ocr_image_subtitles_disabled_by_user is True

from pathlib import Path

from pudge.providers.aria2 import Aria2Client


def test_seed_only_downloads_do_not_consume_download_slots(tmp_path: Path) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    try:
        assert "--bt-detach-seed-only=true" in client._launch_options()
        assert client._runtime_options()["bt-detach-seed-only"] == "true"
        assert client._RUNTIME_PROFILE == "v4-detach-seed-only"
    finally:
        client.close()


def test_runtime_profile_forces_existing_sidecar_upgrade() -> None:
    source = Path("pudge/providers/aria2.py").read_text(encoding="utf-8")
    assert '_RUNTIME_PROFILE = "v4-detach-seed-only"' in source
    assert '"--bt-detach-seed-only=true"' in source

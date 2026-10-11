"""The managed aria2 sidecar: seeding never takes a download slot, and a sidecar started by an
older Pudge with other options is restarted with the current ones (behaviour; audit 71Б)."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.providers import aria2 as aria2_module
from pudge.providers.aria2 import Aria2Client


def test_seed_only_downloads_do_not_consume_download_slots(tmp_path: Path) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    try:
        assert "--bt-detach-seed-only=true" in client._launch_options()
        assert client._runtime_options()["bt-detach-seed-only"] == "true"
        assert client._RUNTIME_PROFILE == "v5-light-io"
    finally:
        client.close()


class _Sidecar:
    """aria2c as seen through Popen and JSON-RPC."""

    def __init__(self) -> None:
        self.running = False
        self.launches: list[list[str]] = []
        self.rpc: list[str] = []

    def popen(self, command, **_kwargs):
        self.launches.append(list(command))
        self.running = True
        return object()

    def call(self, method, params=None):
        if method != "aria2.getVersion":  # getVersion is the liveness probe
            self.rpc.append(method)
        if not self.running:
            raise aria2_module.Aria2Error("aria2 RPC unavailable")
        if method == "aria2.forceShutdown":
            self.running = False
        if method == "aria2.getGlobalOption":
            return {"interface": ""}
        return "OK"


def test_runtime_profile_forces_existing_sidecar_upgrade(tmp_path: Path, monkeypatch) -> None:
    sidecar = _Sidecar()
    client = Aria2Client(state_dir=tmp_path, auto_start=True)
    monkeypatch.setattr(client, "_binary_path", lambda: "/opt/homebrew/bin/aria2c")
    monkeypatch.setattr(client, "_stop_unreachable_managed_sidecars", lambda: True)
    monkeypatch.setattr(client, "_rpc_raw", sidecar.call)
    monkeypatch.setattr(
        aria2_module, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": sidecar.popen})
    )
    monkeypatch.setattr(
        aria2_module, "time", SimpleNamespace(**{**vars(time), "sleep": lambda _seconds: None})
    )
    try:
        client.ensure_running()  # first start
        assert len(sidecar.launches) == 1
        assert "--bt-detach-seed-only=true" in sidecar.launches[0]

        client.ensure_running()  # same Pudge: keep the running sidecar
        assert len(sidecar.launches) == 1 and "aria2.forceShutdown" not in sidecar.rpc

        # The sidecar keeps running across a Pudge update; the older Pudge started it
        # without the current options. It is saved, stopped and started again.
        for older in ("v4-detach-seed-only", "v3-low-heat-no-resume-recheck", ""):
            (tmp_path / "runtime-profile").write_text(older + "\n", encoding="utf-8")
            sidecar.rpc.clear()
            client.ensure_running()
            assert sidecar.rpc[:2] == ["aria2.saveSession", "aria2.forceShutdown"], older
            assert "--bt-detach-seed-only=true" in sidecar.launches[-1]
        assert len(sidecar.launches) == 4
        client.ensure_running()
        assert len(sidecar.launches) == 4
    finally:
        client.close()

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pudge.updater import AppUpdater

requires_zsh = pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the generated macOS updater runs under /bin/zsh")

ROOT = Path(__file__).resolve().parents[1]


def run_update_scenario(tmp_path, monkeypatch, *, source_exits):
    if not Path("/bin/zsh").exists():
        pytest.skip("the generated macOS updater runs under /bin/zsh")
    # Real generated shell, real owned/unrelated processes, harmless installer.
    popen = subprocess.Popen
    owned = popen([sys.executable, "-c", "import time;time.sleep(60)"])
    unrelated = popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        home = tmp_path / "home"; home.mkdir()
        (home / 'Applications/pudge.app').mkdir(parents=True)
        project = tmp_path / "source"; project.mkdir()
        marker = tmp_path / "installed"; reopened = tmp_path / "reopened"
        (project / "install.sh").write_text(f"#!/bin/zsh\ntouch '{marker}'\n")
        opener = tmp_path / "open"; opener.write_text(f"#!/bin/sh\ntouch '{reopened}'\n"); opener.chmod(0o700)
        copier = tmp_path / 'ditto'; copier.write_text('#!/bin/sh\ncp -R "$1" "$2"\n'); copier.chmod(0o700)
        order = []
        def clean():
            order.append("clean")
            if source_exits: owned.terminate(); owned.wait(timeout=3)
        updater = AppUpdater(before_install=clean,request_quit=lambda:order.append("quit"))
        updater.update_root = tmp_path / "updates"; updater.log_path = tmp_path / "update.log"
        with monkeypatch.context() as patch:
            patch.setattr("pudge.updater.Path.home",lambda:home)
            patch.setattr("pudge.updater.os.getpid",lambda:owned.pid)
            patch.setattr("pudge.updater.subprocess.Popen",lambda *a,**k:order.append("spawn"))
            updater._launch_script(project,[])
        assert order == ["clean","spawn","quit"]
        script = tmp_path / "run-pudge-update.zsh"
        body = script.read_text().replace("{1..300}","{1..2}").replace("/usr/bin/open",str(opener)).replace('/usr/bin/ditto', str(copier))
        script.write_text(body)
        result = subprocess.run(["/bin/zsh",str(script)],timeout=5)
        assert unrelated.poll() is None
        if source_exits:
            assert result.returncode == 0 and marker.exists() and reopened.exists()
        else:
            assert result.returncode != 0 and not marker.exists() and reopened.exists()
            assert owned.poll() is None  # updater must not bypass cleanup via SIGKILL
    finally:
        for process in (owned,unrelated):
            if process.poll() is None: process.terminate()
            process.wait(timeout=3)


@requires_zsh
def test_generated_installer_runs_after_owned_exit_and_preserves_unrelated(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=True)


@requires_zsh
def test_generated_installer_refuses_live_source_without_killing_it(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=False)


def run_native_quit_scenario(monkeypatch, *, stuck):
    class App:
        def __init__(self): self.requested=False
        def terminate(self): self.requested=True
        def isTerminated(self): return self.requested and not stuck
    ours,other=App(),App(); lookups=[]
    class Running:
        @staticmethod
        def runningApplicationsWithBundleIdentifier_(bundle):
            lookups.append(bundle)
            return [ours] if bundle == "owned.pudge" else []
    tick=iter([0,31,32])
    fake_time=SimpleNamespace(monotonic=lambda:next(tick),sleep=lambda _:None)
    text=(ROOT/"install.sh").read_text()
    code=text.split("<<'PUDGE_GRACEFUL_QUIT'\n",1)[1].split("\nPUDGE_GRACEFUL_QUIT",1)[0]
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules,"AppKit",SimpleNamespace(NSRunningApplication=Running))
        patch.setitem(sys.modules,"time",fake_time)
        patch.setattr(sys,"argv",["-","owned.pudge","legacy.pudge","owned.pudge"])
        if stuck:
            with pytest.raises(SystemExit,match="still shutting down"): exec(compile(code,"graceful-quit","exec"),{})
        else: exec(compile(code,"graceful-quit","exec"),{})
    assert ours.requested and not other.requested
    assert lookups == ["owned.pudge","legacy.pudge"]


def test_terminal_installer_uses_native_quit_for_its_bundle_only(monkeypatch):
    run_native_quit_scenario(monkeypatch,stuck=False)


def test_terminal_installer_aborts_if_native_quit_cannot_complete(monkeypatch):
    run_native_quit_scenario(monkeypatch,stuck=True)


@pytest.mark.parametrize('alive',[False,True])
def test_terminal_installer_refuses_a_live_cli_session_without_touching_marker(tmp_path,monkeypatch,alive):
    import json
    marker=tmp_path/'app-session.json';marker.write_text(json.dumps({'pid':12345}))
    original=marker.read_bytes();signals=[]
    def probe(pid,signal):
        signals.append((pid,signal))
        if not alive:raise ProcessLookupError
    code=(ROOT/'install.sh').read_text().split("<<'PUDGE_SESSION_GUARD'\n",1)[1].split('\nPUDGE_SESSION_GUARD',1)[0]
    monkeypatch.setattr('os.kill',probe);monkeypatch.setattr(sys,'argv',['-',str(marker)])
    if alive:
        with pytest.raises(SystemExit,match='session is still active'):exec(compile(code,'session-guard','exec'),{})
    else:exec(compile(code,'session-guard','exec'),{})
    assert signals==[(12345,0)] and marker.read_bytes()==original

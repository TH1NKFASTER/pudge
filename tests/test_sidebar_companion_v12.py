from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pudge import process_cleanup, web_app
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from test_sidebar_companion_v11 import cocoa  # noqa: F401 - shared fixture executes real Cocoa backend.


def test_quit_waits_for_cleanup_and_replies_once_in_modal_mode(cocoa, monkeypatch):
    c = cocoa
    entered, released = threading.Event(), threading.Event()
    calls = []
    def close():
        calls.append(threading.current_thread())
        entered.set()
        assert released.wait(2)
    c.api.close = close
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    proxy = c.app.delegate()
    queue = []
    monkeypatch.setattr(type(proxy), 'performSelectorOnMainThread_withObject_waitUntilDone_modes_',
                        lambda self, selector, obj, wait, modes: queue.append((selector,obj,wait,modes)))
    try:
        at = time.monotonic()
        assert proxy.applicationShouldTerminate_(c.app) == 2
        assert time.monotonic() - at < .5
        assert entered.wait(1)
        assert not c.app.replies
        assert proxy.applicationShouldTerminate_(c.app) == 2
    finally:
        released.set()
        c.api._macos_quit_thread.join(timeout=2)
    assert len(calls) == 1 and calls[0] is not threading.main_thread()
    assert len(queue) == 1
    selector, app, wait, modes = queue[0]
    assert selector == 'pudgeFinishQuit:' and app is c.app and wait is False
    assert 'modal' in modes and 'default' in modes
    proxy.pudgeFinishQuit_(app)  # Delivery in the native main-thread modal loop.
    assert c.app.replies == [True]


def minimal_api(events):
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.logger = SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
    api.audiobooks = SimpleNamespace(close=lambda timeout: events.append('audio') or [])
    api._enter_background_quiet = lambda **kwargs: events.append('quiet')
    api._stop_companion_server = lambda: events.append('server')
    api.companion_streaming = SimpleNamespace(close=lambda: events.append('stream'))
    api.energy_monitor = SimpleNamespace(stop=lambda: events.append('energy'))
    api._quiesce_manga_ocr = lambda timeout: events.append('manga') or []
    api.visual_novel_stop = lambda: events.append('vn')
    api._irodori_server_lock = threading.Lock()
    api._stop_managed_irodori_server = lambda: events.append('tts')
    api.task_supervisor = SimpleNamespace(suspend=lambda: events.append('admission'),
        shutdown=lambda timeout: events.append('tasks') or [])
    api.safe_mode = SimpleNamespace(finish_cleanly=lambda: events.append('safe'))
    return api


def test_full_close_is_idempotent_silences_first_and_continues_after_error():
    events = []
    api = minimal_api(events)
    def bad_stream():
        events.append('stream')
        raise OSError('stream close failed')
    api.companion_streaming.close = bad_stream
    api.close()
    api.close()
    assert events[:3] == ['admission', 'audio', 'quiet']
    assert events.count('audio') == 1
    assert all(name in events for name in ('energy','manga','vn','tts','tasks'))
    assert 'safe' not in events
    assert api._close_completed and api._closing


def test_finally_close_and_quit_worker_do_not_run_cleanup_twice():
    events = []
    api = minimal_api(events)
    entered, release = threading.Event(), threading.Event()
    def audio(timeout):
        entered.set()
        assert release.wait(2)
        events.append('audio')
        return []
    api.audiobooks.close = audio
    # Preinitialize as normal __init__ does, before any thread starts.
    api._close_lock = threading.RLock()
    first = threading.Thread(target=api.close)
    second = threading.Thread(target=api.close)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2);second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert events.count('audio') == events.count('quiet') == 1


def test_service_close_kills_real_player_and_rejects_restart(tmp_path):
    service = AudiobookService(Database(tmp_path/'db.sqlite3'),ffprobe='ffprobe',mpv='mpv',cache_dir=tmp_path/'cache')
    process = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    service._players[7] = process
    service._last_positions[7] = 12
    service.book = lambda book: {'id':book}
    # Persistence can fail during shutdown: the process must still be killed.
    service.set_position = lambda *args: (_ for _ in ()).throw(OSError('database unavailable'))
    try:
        assert service.close(timeout=.2) == []
        assert process.poll() is not None
        with pytest.raises(RuntimeError,match='closed'):
            service.play(7)
    finally:
        if process.poll() is None:process.kill()
        process.wait(timeout=2)


def test_owned_tree_stops_detached_grandchildren_and_keeps_unrelated_process(tmp_path):
    pid_path = tmp_path/'grandchild.pid'
    code = ('import subprocess,sys,time,signal;from pathlib import Path;'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
            'p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"],start_new_session=True);'
            f'Path({str(pid_path)!r}).write_text(str(p.pid));time.sleep(60)')
    owner = subprocess.Popen([sys.executable,'-c',code],start_new_session=True)
    foreign = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    try:
        deadline=time.monotonic()+3
        while not pid_path.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert pid_path.exists()
        child=int(pid_path.read_text())
        tree=process_cleanup.OwnedProcessTree(owner.pid)
        # Include the fake application's root explicitly; the real app helper
        # excludes its own root because AppKit exits it after cleanup.
        tree.owned[owner.pid]=process_cleanup.process_snapshot()[owner.pid]
        assert child in tree.owned and foreign.pid not in tree.owned
        assert tree.stop(grace=.05) == []
        owner.wait(timeout=2)
        assert foreign.poll() is None
    finally:
        for process in (owner,foreign):
            if process.poll() is None:process.kill()
            process.wait(timeout=2)
        if pid_path.exists():
            try:os.kill(int(pid_path.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass


def test_process_identity_change_prevents_signaling_reused_pid(monkeypatch):
    old=process_cleanup.ProcessIdentity(900,100,'earlier','S')
    newer=process_cleanup.ProcessIdentity(900,1,'later','S')
    monkeypatch.setattr(process_cleanup,'process_snapshot',lambda:{900:newer})
    tree=process_cleanup.OwnedProcessTree(100)
    tree.owned[900]=old
    signals=[]
    monkeypatch.setattr(process_cleanup.os,'kill',lambda *args:signals.append(args))
    tree._signal(signal.SIGKILL)
    assert signals == []


def test_watchdog_cleans_remaining_children_and_does_not_double_reply(cocoa, monkeypatch):
    c=cocoa
    release=threading.Event();timers=[];cleanup=[]
    class Timer:
        def __init__(self,interval,callback):self.callback=callback;timers.append(self)
        def start(self):pass
        def cancel(self):pass
    c.api._owning_pid=os.getpid()
    c.api._shutdown_process_tree=SimpleNamespace(stop=lambda:cleanup.append('children') or [])
    c.api.close=lambda:release.wait(2)
    monkeypatch.setattr(web_app.threading,'Timer',Timer)
    assert web_app._install_macos_app_delegate_proxy(c.api,c.lifecycle)
    proxy=c.app.delegate()
    try:
        assert proxy.applicationShouldTerminate_(c.app) == 2
        assert len(timers)==1
        timers[0].callback()
        assert cleanup == ['children'] and c.app.replies == [True]
    finally:
        release.set();c.api._macos_quit_thread.join(2)
    assert c.app.replies == [True]


def test_launch_uses_full_quit_cleanup_and_blocks_new_video_admission():
    source=Path(web_app.__file__).read_text()
    launch=source[source.index('def launch_web_app('):]
    assert '_MacWindowLifecycle(window, api.logger)' in launch
    assert 'lambda source: api._enter_background_quiet' not in launch
    assert 'Pudge is shutting down' in source


def test_spawn_racing_close_is_registered_before_shutdown_snapshot(tmp_path, monkeypatch):
    service=AudiobookService(Database(tmp_path/'db.sqlite3'),ffprobe='ffprobe',mpv='mpv',cache_dir=tmp_path/'cache')
    service.book=lambda book:{'id':book,'position':0,'duration':10}
    service._file_rows=lambda book:[{'file_index':0,'path':str(tmp_path/'audio.wav'),'start':0,'end':10}]
    service._tracked_thread=lambda **kwargs:None
    process=SimpleNamespace(returncode=None)
    process.poll=lambda:process.returncode
    process.terminate=lambda:setattr(process,'returncode',-15)
    process.kill=lambda:setattr(process,'returncode',-9)
    process.wait=lambda timeout:process.returncode
    closer=[]
    def spawn(*args,**kwargs):
        worker=threading.Thread(target=lambda:service.close(timeout=.1))
        closer.append(worker);worker.start()
        assert service._closed_event.wait(1)
        return process
    monkeypatch.setattr(subprocess,'Popen',spawn)
    service.play(7,start=0)
    closer[0].join(2)
    assert not closer[0].is_alive()
    assert process.poll() is not None


def test_native_quit_stops_real_mpv_before_reply(cocoa,tmp_path,short_socket_dir):
    import shutil
    import wave
    mpv=shutil.which('mpv')
    if not mpv:pytest.skip('mpv unavailable')
    audio=tmp_path/'audio.wav';socket=short_socket_dir/'mpv.sock'
    error_path=tmp_path/'mpv-stderr.log'
    assert len(os.fsencode(socket)) < 96
    with wave.open(str(audio),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*16000)
    with error_path.open('wb') as error_log:
        process=subprocess.Popen([mpv,'--no-config','--load-scripts=no','--no-video','--ao=null',
            '--loop-file=inf','--input-ipc-server='+str(socket),str(audio)],stdout=subprocess.DEVNULL,stderr=error_log)
        try:
            service=AudiobookService(Database(tmp_path/'db.sqlite3'),ffprobe='ffprobe',mpv=mpv,cache_dir=tmp_path/'cache')
            service._players[7]=process;service._ipc_paths[7]=socket;service.book=lambda book:{'id':book}
            c=cocoa;events=[];api=minimal_api(events);api.logger=c.api.logger;api.audiobooks=service
            c.api.close=api.close
            deadline=time.monotonic()+2
            while not socket.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            assert process.poll() is None and socket.exists(), (
                f'mpv IPC startup failed (exit={process.poll()}, socket={socket}): '
                + error_path.read_text(errors='replace'))
            assert web_app._install_macos_app_delegate_proxy(c.api,c.lifecycle)
            assert c.app.delegate().applicationShouldTerminate_(c.app)==2
            assert c.app.replied.wait(3)
            assert process.poll() is not None
            assert service._closed_event.is_set()
            assert c.app.replies==[True]
        finally:
            if process.poll() is None:process.kill()
            process.wait(timeout=2)

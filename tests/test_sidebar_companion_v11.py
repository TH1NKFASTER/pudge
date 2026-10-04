from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest
import webview

from pudge import web_app


class NSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def performSelectorOnMainThread_withObject_waitUntilDone_modes_(self, selector, obj, wait, modes):
        # Native dispatch is exercised separately by the v12 modal-loop test.
        self.pudgeFinishQuit_(obj)


class Application:
    def __init__(self):
        self.current_delegate = None
        self.replied = threading.Event()
        self.replies = []

    def delegate(self):
        return self.current_delegate

    def setDelegate_(self, delegate):
        self.current_delegate = delegate

    def replyToApplicationShouldTerminate_(self, allowed):
        self.replies.append(allowed)
        self.replied.set()


class Window:
    def __init__(self):
        self.hidden = False
        self.hide_calls = self.show_calls = 0
        self.native = None

    def hide(self):
        self.hide_calls += 1
        self.hidden = True

    def show(self):
        self.show_calls += 1
        self.hidden = False


@pytest.fixture
def cocoa(monkeypatch):
    # Execute the actual installed pywebview AppDelegate and the two delegate
    # assignments in BrowserView.__init__, without requiring an AppKit display.
    path = Path(webview.__file__).parent / 'platforms/cocoa.py'
    source = ast.parse(path.read_text())
    view_class = next(row for row in source.body if isinstance(row, ast.ClassDef) and row.name == 'BrowserView')
    app_class = next(row for row in view_class.body if isinstance(row, ast.ClassDef) and row.name == 'AppDelegate')
    constructor = next(row for row in view_class.body if isinstance(row, ast.FunctionDef) and row.name == '__init__')
    start = next(index for index, row in enumerate(constructor.body)
                 if isinstance(row, ast.If) and ast.unparse(row.test) == 'BrowserView._shared_app_delegate is None')
    assignments = ast.Module(body=constructor.body[start:start + 2], type_ignores=[])
    app = Application()
    browser = SimpleNamespace(app=app, instances={}, _shared_app_delegate=None)
    foundation = SimpleNamespace(NSObject=NSObject, YES=True, NO=False, NSDefaultRunLoopMode='default')
    appkit = SimpleNamespace(NSObject=NSObject, NSTerminateLater=2, NSModalPanelRunLoopMode='modal', NSApplication=SimpleNamespace(sharedApplication=lambda: app))
    context = {'BrowserView': browser, 'AppKit': appkit, 'Foundation': foundation}
    exec(compile(ast.Module(body=[app_class], type_ignores=[]), str(path), 'exec'), context)
    browser.AppDelegate = context['AppDelegate']
    create_delegate = lambda: exec(compile(assignments, str(path), 'exec'), context)
    create_delegate()
    original = app.delegate()
    monkeypatch.setitem(sys.modules, 'AppKit', appkit)
    monkeypatch.setitem(sys.modules, 'Foundation', foundation)
    monkeypatch.setitem(sys.modules, 'webview.platforms.cocoa', SimpleNamespace(BrowserView=browser))
    monkeypatch.setattr(web_app.sys, 'platform', 'darwin')
    messages = []
    logger = SimpleNamespace(info=lambda *args: messages.append(args),
                             warning=lambda *args: messages.append(args),
                             exception=lambda *args: pytest.fail(str(args)))
    api = SimpleNamespace(logger=logger, close=lambda: None)
    main = Window()
    quiet = []
    lifecycle = web_app._MacWindowLifecycle(main, logger, quiet.append)
    browser.should_close = lambda window: lifecycle.handle_closing() if window is main else True
    browser.instances['master'] = SimpleNamespace(pywebview_window=main)
    return SimpleNamespace(app=app, browser=browser, original=original, api=api,
                           main=main, lifecycle=lifecycle, quiet=quiet,
                           create_delegate=create_delegate)


def test_child_windows_preserve_real_quit_delegate_and_allow_quit(cocoa):
    c = cocoa
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    proxy = c.app.delegate()
    for uid in ('audiobook-first', 'audiobook-reopened'):
        c.create_delegate()  # Real pywebview child-window delegate assignment.
        c.browser.instances[uid] = SimpleNamespace(pywebview_window=Window())
        assert c.app.delegate() is proxy
        assert c.browser._shared_app_delegate is proxy
    assert proxy.applicationShouldTerminate_(c.app) == 2
    assert c.app.replied.wait(2)
    assert c.lifecycle.quit_requested is True
    assert c.main.hide_calls == 0
    assert c.quiet == ['macos_quit']


@pytest.mark.parametrize('has_visible_windows', [False, True])
def test_dock_reopen_restores_main_after_companion_creation(cocoa, has_visible_windows):
    c = cocoa
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    c.create_delegate()
    assert c.lifecycle.handle_closing() is False
    assert c.main.hidden
    c.app.delegate().applicationShouldHandleReopen_hasVisibleWindows_(c.app, has_visible_windows)
    assert c.main.hidden is False
    assert c.main.show_calls == 1
    assert c.lifecycle.quit_requested is False
    assert c.quiet == []


def test_minimized_main_window_is_restored_before_show(cocoa):
    calls = []
    c = cocoa
    c.main.native = SimpleNamespace(isMiniaturized=lambda: True,
                                    deminiaturize_=lambda sender: calls.append('restore'))
    c.main.show = lambda: calls.append('show')
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    c.app.delegate().applicationShouldHandleReopen_hasVisibleWindows_(c.app, True)
    assert calls == ['restore', 'show']


def test_delegate_install_is_idempotent_and_keeps_original_backend(cocoa):
    c = cocoa
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    proxy = c.app.delegate()
    assert proxy._pudge_original_delegate is c.original
    c.app.setDelegate_(c.original)  # Recover an external delegate replacement.
    for _ in range(3):
        assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
        assert c.app.delegate() is c.browser._shared_app_delegate is proxy
        assert proxy._pudge_original_delegate is c.original
    assert proxy.applicationShouldTerminate_(c.app) == 2
    assert c.app.replied.wait(2)
    assert proxy.applicationShouldTerminate_(c.app) == 2
    assert c.app.replied.wait(2)
    assert c.quiet == ['macos_quit']


def test_reopen_during_quit_does_not_show_main_again(cocoa):
    c = cocoa
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle)
    c.lifecycle.request_quit()
    c.app.delegate().applicationShouldHandleReopen_hasVisibleWindows_(c.app, False)
    assert c.main.show_calls == 0


def test_restore_from_worker_queues_native_operations_on_main_thread(cocoa, monkeypatch):
    c = cocoa
    queued, native_threads = [], []
    c.main.native = SimpleNamespace(isMiniaturized=lambda: True,
        deminiaturize_=lambda sender: native_threads.append(threading.current_thread()))
    monkeypatch.setitem(sys.modules, 'PyObjCTools', SimpleNamespace(
        AppHelper=SimpleNamespace(callAfter=lambda callback: queued.append(callback))))
    worker = threading.Thread(target=c.lifecycle.reopen)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(queued) == 1 and native_threads == []
    queued[0]()
    assert native_threads == [threading.main_thread()]
    assert c.main.show_calls == 1


def test_other_platforms_do_not_replace_delegate(cocoa, monkeypatch):
    c = cocoa
    monkeypatch.setattr(web_app.sys, 'platform', 'linux')
    assert web_app._install_macos_app_delegate_proxy(c.api, c.lifecycle) is False
    assert c.app.delegate() is c.original

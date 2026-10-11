"""Update lifecycle of v0.7.10 (behaviour; audit 71Б rewrite): the release download retries
dropped connections, the installer waits for the old app, and the window installs without
a native WebKit confirm."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from pudge import updater


class _Stream:
    """One GET of the release archive: sends `chunks`, then drops the connection if `fail`."""

    def __init__(self, chunks: list[bytes], fail: bool) -> None:
        self.chunks, self.fail = chunks, fail
        self.headers = {"Content-Length": str(sum(map(len, chunks)))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self) -> None:
        pass

    def iter_bytes(self):
        yield from self.chunks
        if self.fail:
            raise httpx.ReadError("connection reset by peer")


def _serve(monkeypatch, attempts: list[_Stream]) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(updater.httpx, "stream", lambda *_a, **_k: attempts.pop(0))
    monkeypatch.setattr(updater.time, "sleep", sleeps.append)
    return sleeps


def test_release_download_retries_dropped_connections_from_scratch(tmp_path, monkeypatch) -> None:
    app = updater.AppUpdater()
    app.log_path = tmp_path / "update.log"
    output_zip = tmp_path / "pudge.zip"
    sleeps = _serve(
        monkeypatch, [_Stream([b"abc"], True), _Stream([b"ab"], True), _Stream([b"abc", b"def"], False)]
    )

    digest = app._download("https://example.test/pudge.zip", output_zip)

    assert digest == hashlib.sha256(b"abcdef").hexdigest()
    assert output_zip.read_bytes() == b"abcdef"  # no leftovers of the broken attempts
    assert sleeps == [1.0, 2.0]
    log_text = app.log_path.read_text(encoding="utf-8")
    assert "APP update download retry 1/5 in 1s after: connection reset by peer" in log_text
    assert "APP update download retry 2/5 in 2s after: connection reset by peer" in log_text


def test_release_download_gives_up_after_five_retries_and_leaves_no_partial_file(
    tmp_path, monkeypatch
) -> None:
    app = updater.AppUpdater()
    app.log_path = tmp_path / "update.log"
    archive = tmp_path / "pudge.zip"
    sleeps = _serve(monkeypatch, [_Stream([b"partial"], True) for _ in range(6)])

    with pytest.raises(httpx.TransportError):
        app._download("https://example.test/pudge.zip", archive)

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 12.0]
    assert not archive.exists()


def test_updater_waits_for_old_managed_app_before_install(tmp_path, monkeypatch) -> None:
    from test_v16_updater_script import run_update_scenario

    run_update_scenario(tmp_path, monkeypatch, source_exits=False)


def test_install_update_button_installs_without_a_native_confirm(page) -> None:
    # A native confirm() in WKWebView blocked the window (and could not be styled or translated);
    # the update installs at once, even where such a dialog would say "no".
    result = page.run(
        r"""
        setPage('settings'); await __tick(200);
        await __click('[data-settings-category="advanced"]'); await __tick(10);
        await __click('#checkAppUpdate'); await __tick(10);
        await __click('#installAppUpdate'); await __tick(10);
        return __text('#appUpdateState') === t('update.downloading');
        """,
        before=r"""
        __api.app_update_status = async () => ({state: 'idle', available: true, latest_version: '9.9.9', channel: 'release'});
        __api.app_update_install = async () => ({state: 'preparing', running: true});
        __api.app_update_progress = async () => ({state: 'downloading', running: true});
        """,
        confirm=False,
    )
    assert result.value is True
    assert result.calls_of("app_update_install") == [[]]
    assert result.dialogs == []

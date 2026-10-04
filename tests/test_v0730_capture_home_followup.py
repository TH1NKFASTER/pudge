from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.visual_novels import VisualNovelError, VisualNovelService
from test_v0733_seihantai_refresh_drop_perf import _manager
from test_web_app import make_api

ROOT = Path(__file__).resolve().parents[1]


def test_conflicting_completed_downloads_keep_owner_and_subtitle_state(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Synthetic Series - 11.mkv"
    video.write_bytes(b"synthetic video")
    for media_id in (1, 2, 3):
        manager.db.upsert_anime(LibraryAnime(media_id, f"Synthetic {media_id}", status="CURRENT"))
        manager.db.upsert_download(DownloadItem(
            torrent_hash=f"hash-{media_id}", name=video.name, state="complete", progress=1,
            save_path=str(video.parent), content_path=str(video), media_id=media_id,
            episode=11, media_episode=11, release_episode=11,
        ))
    manager.db.upsert_episode(LibraryEpisode(
        media_id=2, title="Synthetic 2", episode=11, media_episode=11, release_episode=11,
        video_path=video, state="ready", torrent_hash="hash-2",
    ))
    before = manager.db.episode_by_path(video)
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("No re-probe")))
    for _ in range(5):
        assert manager.reconcile_completed_download_rows() == 0
        assert manager.reconcile_completed_download_rows(1, 11) == 0
        assert manager._register_completed_download(manager.db.downloads()[0]) == 0
    assert manager.db.episode_by_path(video) == before
    assert manager.db.subtitle_jobs() == []
    assert len(manager.db.downloads()) == 3 and video.read_bytes() == b"synthetic video"


@pytest.fixture
def picker_bridge(monkeypatch):
    import pudge.screen_capture_picker as module

    queued = []

    class Object:
        def __init_subclass__(cls, **kwargs):
            pass

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class Configuration(Object):
        def setAllowedPickerModes_(self, value): self.modes = value
        def setAllowsChangingSelectedContent_(self, value): self.changing = value
        def setExcludedBundleIDs_(self, value): self.excluded = value
        def setWidth_(self, value): self.width = value
        def setHeight_(self, value): self.height = value
        def setShowsCursor_(self, value): self.cursor = value
        def setCapturesAudio_(self, value): self.audio = value

    class Picker:
        def setDefaultConfiguration_(self, value): self.config = value
        def addObserver_(self, value): self.observer = value
        def removeObserver_(self, value):
            assert value is self.observer
            self.observer = None
        def setActive_(self, value): self.active = value
        def presentPickerUsingContentStyle_(self, value): self.style = value

    picker = Picker()
    monkeypatch.setattr(module, "_observer_class", None)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(protocolNamed=lambda name: name))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSObject=Object))
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", SimpleNamespace(
        SCContentSharingPicker=SimpleNamespace(sharedPicker=lambda: picker),
        SCContentSharingPickerConfiguration=Configuration,
        SCStreamConfiguration=Configuration, SCContentSharingPickerModeSingleWindow=1,
        SCShareableContentStyleWindow=7,
    ))
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=lambda fn: queued.append(fn))))
    monkeypatch.setattr("pudge.visual_novels.platform.system", lambda: "Darwin")
    return module, picker, queued


def _select(picker_bridge, action):
    module, picker, queued = picker_bridge
    session = module.WindowPickerSession()
    result = {}

    def run():
        try: result["filter"] = session.select(threading.Event(), timeout=1)
        except VisualNovelError as exc: result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    # Main-run-loop work is deliberately queued, proving the RPC worker does
    # not present Cocoa UI itself. No real TCC request or desktop pixels.
    for _ in range(100):
        if queued: break
        thread.join(.001)
    queued.pop(0)()
    assert picker.config.modes == 1 and picker.config.changing is False
    assert picker.active and picker.style == 7
    action(picker.observer)
    thread.join(1)
    assert not thread.is_alive()
    return session, result


def test_picker_accepts_selected_filter_and_releases_observer(picker_bridge):
    chosen = object()
    session, result = _select(picker_bridge, lambda observer: observer.contentSharingPicker_didUpdateWithFilter_forStream_(None, chosen, None))
    assert result == {"filter": chosen}
    session.close(); session.close()
    _, picker, queued = picker_bridge
    queued.pop(0)()
    assert picker.active is False and picker.observer is None


def test_picker_cancel_ignores_late_selection(picker_bridge):
    def cancel(observer):
        observer.contentSharingPicker_didCancelForStream_(None, None)
        observer.contentSharingPicker_didUpdateWithFilter_forStream_(None, object(), None)

    _, result = _select(picker_bridge, cancel)
    assert result["error"].code == "cancelled"
    picker_bridge[2].pop(0)()
    assert picker_bridge[1].active is False


def test_picker_error_is_classified_and_cleaned(picker_bridge):
    error = SimpleNamespace(localizedDescription=lambda: "Permission denied", code=lambda: -3801)
    _, result = _select(picker_bridge, lambda observer: observer.contentSharingPickerStartDidFailWithError_(error))
    assert result["error"].code == "permission_required"
    picker_bridge[2].pop(0)()
    assert picker_bridge[1].active is False


def test_picker_filter_captures_without_shareable_enumeration(picker_bridge, monkeypatch):
    module, _, _ = picker_bridge
    chosen = SimpleNamespace(
        contentRect=lambda: SimpleNamespace(size=SimpleNamespace(width=3000, height=2000)),
        pointPixelScale=lambda: 2,
        includedWindows=lambda: [SimpleNamespace(windowID=lambda: 77, title=lambda: "Synthetic game")],
    )
    session = SimpleNamespace(select=lambda event: chosen, close=lambda: None)
    monkeypatch.setattr(module, "WindowPickerSession", lambda: session)
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_recording_access", lambda: False)
    monkeypatch.setattr(service, "_shareable_content", lambda *_: (_ for _ in ()).throw(AssertionError("No broad capture grant")))

    def capture(path, content_filter, configuration, stop_event):
        assert content_filter is chosen
        assert (configuration.width, configuration.height) == (3840, 2560)
        assert configuration.audio is False and configuration.cursor is False
        Image.new("RGB", (64, 48), "white").save(path)
        stop_event.set()

    class Frames:
        def __init__(self, **kwargs): pass
        def start(self, selected, configuration, stop, **kwargs):
            assert selected is chosen
            assert configuration.width == 3840 and configuration.height == 2560
            self.configuration = configuration
        def capture(self, path, stop, **kwargs): capture(path, chosen, self.configuration, stop)
        def close(self): pass

    monkeypatch.setattr("pudge.screen_capture_stream.StreamFrameSource", Frames)
    monkeypatch.setattr(service, "_capture_window", lambda *_: pytest.fail("Picker must not request screenshots"))
    monkeypatch.setattr(service, "_recognize_frame", lambda _: "")
    monkeypatch.setattr(service, "_recognize_speaker", lambda _: "")
    state = service.start_with_picker()
    service._thread.join(1)
    assert state["window_id"] == 77 and state["window_title"] == "Synthetic game"
    assert state["capture_selection"] == "system_picker"
    assert service.state()["capture_count"] == 1
    service.stop()


def test_cancelled_picker_does_not_start_consumption(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    monkeypatch.setattr(api.visual_novels, "start_with_picker", lambda *_: (_ for _ in ()).throw(VisualNovelError("Cancelled", code="cancelled")))
    monkeypatch.setattr(api.consumption, "begin_vn_capture", lambda **_: (_ for _ in ()).throw(AssertionError("No consumption on cancellation")))
    assert api.visual_novel_start(0, "", "", True)["selection_cancelled"] is True


def test_home_updates_preserve_cards_covers_focus_and_sequence_selection():
    subprocess.run(["node", str(ROOT / "tests/js/home_stability.cjs"), str(ROOT / "pudge/web/index.html")], check=True, capture_output=True, text=True, timeout=15)

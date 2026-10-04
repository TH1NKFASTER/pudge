from __future__ import annotations

import contextlib
import sys
import threading
import weakref
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.screen_capture_stream import StreamFrameSource
from pudge.visual_novels import VisualNovelError, VisualNovelService
from test_v0730_capture_home_followup import picker_bridge  # noqa: F401
from test_web_app import make_api


@pytest.fixture
def stream_bridge(picker_bridge, monkeypatch):
    import pudge.screen_capture_stream as module

    _, _, queued = picker_bridge
    sck = sys.modules['ScreenCaptureKit']
    sck.SCStreamOutputTypeScreen = 0
    sck.SCStreamFrameInfoStatus = 'status'
    sck.SCFrameStatusComplete = 0
    sck.SCFrameStatusIdle = 1
    sck.SCFrameStatusBlank = 2
    sck.SCFrameStatusSuspended = 3
    sck.SCFrameStatusStopped = 4
    streams = []

    class Stream:
        @classmethod
        def alloc(cls): return cls()
        def initWithFilter_configuration_delegate_(self, chosen, config, delegate):
            self.chosen, self.config, self.delegate = chosen, config, delegate
            self.stops = 0
            self.removes = 0
            streams.append(self)
            return self
        def addStreamOutput_type_sampleHandlerQueue_error_(self, output, kind, queue, error):
            assert kind == 0 and queue is None and error is None
            self.output = output
            return True, None
        def startCaptureWithCompletionHandler_(self, callback): self.started = callback
        def stopCaptureWithCompletionHandler_(self, callback):
            self.stops += 1
            callback(None)
        def removeStreamOutput_type_error_(self, output, kind, error):
            assert output is self.output
            self.removes += 1
            self.output = None
            return True, None

    sck.SCStream = Stream
    class Frame:
        def __init__(self, pixel): self.pixel = pixel
        def extent(self): return 'synthetic bounds'
    class Context:
        renders = []
        @classmethod
        def contextWithOptions_(cls, _): return cls()
        def createCGImage_fromRect_(self, image, rect):
            self.renders.append(image.pixel)
            return image.pixel
    class Bitmap:
        @classmethod
        def alloc(cls): return cls()
        def initWithCGImage_(self, image): self.image = image; return self
        def representationUsingType_properties_(self, _, __):
            image = self.image
            def write(path, atomic):
                Image.new('RGB', (16, 12), image).save(path)
                return True
            return SimpleNamespace(writeToFile_atomically_=write)
    monkeypatch.setattr(module, '_delegate_class', None)
    sys.modules['objc'].autorelease_pool = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, 'CoreMedia', SimpleNamespace(
        CMTimeMake=lambda n, d: (n, d),
        CMSampleBufferIsValid=lambda sample: sample.valid,
        CMSampleBufferGetSampleAttachmentsArray=lambda sample, _: sample.attachments,
        CMSampleBufferGetImageBuffer=lambda sample: sample.pixel,
    ))
    monkeypatch.setitem(sys.modules, 'Quartz', SimpleNamespace(
        CIImage=SimpleNamespace(imageWithCVPixelBuffer_=Frame), CIContext=Context,
    ))
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(
        NSBitmapImageRep=Bitmap, NSBitmapImageFileTypePNG=4,
    ))
    monkeypatch.setitem(sys.modules, 'PyObjCTools', SimpleNamespace(AppHelper=SimpleNamespace(
        callAfter=lambda fn, *args: queued.append(lambda: fn(*args)),
    )))
    class Configuration:
        def setMinimumFrameInterval_(self, value): self.interval = value
        def setQueueDepth_(self, value): self.depth = value
    return SimpleNamespace(module=module, sck=sck, queued=queued, streams=streams,
                           config=Configuration(), renders=Context.renders)


def start_source(bridge, *, error=None):
    source = StreamFrameSource()
    result = {}
    stop = threading.Event()
    chosen = object()
    def start():
        try: source.start(chosen, bridge.config, stop, timeout=1)
        except VisualNovelError as exc: result['error'] = exc
    worker = threading.Thread(target=start)
    worker.start()
    for _ in range(100):
        if bridge.queued: break
        worker.join(.001)
    bridge.queued.pop(0)()
    stream = bridge.streams[-1]
    assert stream.chosen is chosen
    assert bridge.config.interval == (1, 2) and bridge.config.depth == 3
    stream.started(error)
    worker.join(1)
    assert not worker.is_alive()
    return source, stream, result


def sample(pixel='red', status=0, *, valid=True, attachments=True):
    return SimpleNamespace(pixel=pixel, valid=valid,
                           attachments=[{'status': status}] if attachments else [])


def test_stream_reuses_static_frame_keeps_only_latest_and_encodes_on_demand(stream_bridge, tmp_path):
    source, stream, result = start_source(stream_bridge)
    assert not result
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    old = weakref.ref(source._image)
    for _ in range(30):
        stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample('blue'), 0)
    assert old() is None and stream_bridge.renders == []
    for _ in range(2):
        stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(status=1), 0)
        source.capture(tmp_path/'frame.png', threading.Event(), timeout=.05)
        with Image.open(tmp_path/'frame.png') as image:
            assert image.getpixel((0, 0)) == (0, 0, 255)
    assert stream_bridge.renders == ['blue', 'blue']
    source.close(); source.close()
    stream_bridge.queued.pop(0)()
    assert stream.stops == stream.removes == 1 and stream.delegate.frames is None
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    assert source._image is None


@pytest.mark.parametrize('case', ['invalid', 'audio', 'missing_attachment', 'missing_image', 'blank', 'suspended', 'stopped'])
def test_stream_does_not_decode_incomplete_or_unselected_content(stream_bridge, tmp_path, case):
    source, stream, _ = start_source(stream_bridge)
    status = {'blank': 2, 'suspended': 3, 'stopped': 4}.get(case, 0)
    if case in ('blank', 'suspended', 'stopped'):
        stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(
        pixel=None if case == 'missing_image' else 'red', status=status,
        valid=case != 'invalid', attachments=case != 'missing_attachment',
    ), 1 if case == 'audio' else 0)
    with pytest.raises(VisualNovelError) as caught:
        source.capture(tmp_path/'frame.png', threading.Event(), timeout=.001)
    assert caught.value.code == ('window_unavailable' if case == 'stopped' else 'capture_timeout')
    assert not (tmp_path/'frame.png').exists() and stream_bridge.renders == []
    source.close(); stream_bridge.queued.pop(0)()


def test_stream_start_decline_releases_native_stream(stream_bridge):
    error = SimpleNamespace(localizedDescription=lambda: 'Permission denied', code=lambda: -3801)
    source, stream, result = start_source(stream_bridge, error=error)
    assert result['error'].code == 'permission_required'
    stream_bridge.queued.pop(0)()
    assert stream.stops == stream.removes == 1 and source._closed


def test_cancel_before_cocoa_runs_cannot_create_late_stream(stream_bridge):
    source = StreamFrameSource()
    stop = threading.Event(); stop.set()
    with pytest.raises(VisualNovelError, match='cancelled'):
        source.start(object(), stream_bridge.config, stop, timeout=.01)
    stream_bridge.queued.pop(0)()
    assert not stream_bridge.streams and source._closed


def test_stream_stop_error_discards_frame_and_wakes_reader(stream_bridge, tmp_path):
    source, stream, _ = start_source(stream_bridge)
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    error = SimpleNamespace(localizedDescription=lambda: 'Window closed', code=lambda: -1)
    stream.delegate.stream_didStopWithError_(stream, error)
    with pytest.raises(VisualNovelError, match='Window closed'):
        source.capture(tmp_path/'frame.png', threading.Event())
    assert not (tmp_path/'frame.png').exists() and source._image is None
    source.close(); stream_bridge.queued.pop(0)()


@pytest.mark.parametrize('failure', ['start', 'capture', 'timeout'])
def test_reader_error_closes_stream_and_picker_without_explicit_stop(monkeypatch, stream_bridge, failure):
    closed = []
    class Frames:
        def __init__(self, **kwargs): pass
        def start(self, *args, **kwargs):
            if failure == 'start': raise VisualNovelError('declined', code='permission_required')
        def capture(self, *args, **kwargs):
            raise VisualNovelError('no frame', code='capture_timeout' if failure == 'timeout' else 'capture_decode_error')
        def close(self): closed.append('stream')
    monkeypatch.setattr('pudge.screen_capture_stream.StreamFrameSource', Frames)
    monkeypatch.setattr(VisualNovelService, '_screen_recording_access', staticmethod(lambda: False))
    service = VisualNovelService()
    picker = SimpleNamespace(close=lambda: closed.append('picker'))
    service.start(0, 'synthetic game', capture_target=(object(), object()), picker_session=picker)
    service._thread.join(1)
    state = service.state()
    assert state['status'] == 'error' and not state['running'] and state['capture_method'] == 'stream'
    assert state['capture_count'] == 0 and closed == ['stream', 'picker']
    assert service._picker_session is None
    service.stop()
    assert closed == ['stream', 'picker']


def test_failed_reader_does_not_leave_consumption_session_open(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    api._vn_consumption_session_id = 'synthetic session'
    ended = []
    monkeypatch.setattr(api.consumption, 'end_vn_capture', lambda session: ended.append(session))
    monkeypatch.setattr(api.visual_novels, 'state', lambda: {'status': 'error', 'running': False})
    api.visual_novel_state(); api.visual_novel_state()
    assert ended == ['synthetic session'] and api._vn_consumption_session_id == ''
    monkeypatch.setattr(api.visual_novels, 'start_with_picker', lambda *_: {'status': 'error', 'window_id': 0})
    monkeypatch.setattr(api.consumption, 'begin_vn_capture', lambda **_: pytest.fail('Failed reader must not start consumption'))
    assert api.visual_novel_start(0, use_system_picker=True)['status'] == 'error'


def test_cancel_waiting_for_first_frame_wakes_and_stops_source(stream_bridge, tmp_path):
    source, stream, _ = start_source(stream_bridge)
    cancelled = threading.Event()
    caught = []
    def wait():
        try: source.capture(tmp_path/'frame.png', cancelled, timeout=1)
        except VisualNovelError as error: caught.append(error.code)
    worker = threading.Thread(target=wait)
    worker.start(); cancelled.set(); worker.join(.2)
    assert not worker.is_alive() and caught == ['cancelled']
    source.close(); stream_bridge.queued.pop(0)()
    assert stream.stops == stream.removes == 1


def test_late_start_callback_after_timeout_cannot_restore_stream(stream_bridge):
    source = StreamFrameSource()
    result = []
    def start():
        try: source.start(object(), stream_bridge.config, threading.Event(), timeout=.08)
        except VisualNovelError as error: result.append(error.code)
    worker = threading.Thread(target=start)
    worker.start()
    for _ in range(100):
        if stream_bridge.queued: break
        worker.join(.001)
    stream_bridge.queued.pop(0)()
    stream = stream_bridge.streams[-1]
    worker.join(.2)
    assert not worker.is_alive() and result == ['capture_timeout']
    stream_bridge.queued.pop(0)()
    stream.started(None)
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    assert stream.stops == 1 and source._closed and source._image is None


def test_stream_decode_error_retains_no_partial_file(stream_bridge, tmp_path, monkeypatch):
    source, stream, _ = start_source(stream_bridge)
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, sample(), 0)
    context = sys.modules['Quartz'].CIContext
    monkeypatch.setattr(context, 'createCGImage_fromRect_', lambda *_: None)
    with pytest.raises(VisualNovelError) as error:
        source.capture(tmp_path/'frame.png', threading.Event())
    assert error.value.code == 'capture_decode_error' and not (tmp_path/'frame.png').exists()
    source.close(); stream_bridge.queued.pop(0)()


def test_picker_excludes_own_app(picker_bridge):
    from test_v0730_capture_home_followup import _select
    session, _ = _select(picker_bridge, lambda observer: observer.contentSharingPicker_didUpdateWithFilter_forStream_(None, object(), None))
    assert picker_bridge[1].config.excluded == ['com.pudge.app', 'com.anime-mpv.app']
    session.close(); picker_bridge[2].pop(0)()

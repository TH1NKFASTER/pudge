import threading
import uuid

from test_iphone_audio_repository import archived, service  # noqa: F401


class LiveAudio:
    def __init__(self):
        self._playback_lock = threading.RLock()
        self._playback_sessions = {1: "live-one"}
        self._ipc_paths = {1: "socket"}
        self.running = True
        self.paused = False
        self.pauses = 0
        self.confirm = True

    def is_playing(self, _):
        return self.running

    def _ipc_command(self, _, command):
        if self.confirm:
            self.paused = True
            self.pauses += 1
            return {"error": "success"}
        return None

    def _ipc_get(self, _, name):
        return self.paused if name == "pause" else None

    def _global_position(self, *_):
        return 12.5


def setup_live(service):
    device, manifest = archived(service)
    service.audiobooks = LiveAudio()
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_assets SET book_id=1 WHERE id=?", (manifest["assetId"],))
    service.begin_mac_session(manifest["assetId"], "live-one")
    payload = {
        "operationId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "expectedRevision": 0,
        "sourceSessionId": "live-one",
        "targetReady": True,
    }
    return device, manifest, payload


def test_handoff_pause_is_confirmed_before_receipt_and_retry_cannot_pause_new_session(service):
    device, manifest, payload = setup_live(service)
    result = service.handoff(device, payload)
    assert result["status"] == "source_paused"
    assert result["positionMs"] == 12500
    assert service.audiobooks.pauses == 1
    service.audiobooks._playback_sessions[1] = "live-two"
    assert service.handoff(device, payload) == result
    assert service.audiobooks.pauses == 1


def test_handoff_missing_ack_never_claims_pause(service):
    device, _, payload = setup_live(service)
    service.audiobooks.confirm = False
    assert service.handoff(device, payload)["status"] == "source_unreachable"


def test_handoff_not_ready_does_not_pause_source(service):
    device, _, payload = setup_live(service)
    payload["targetReady"] = False
    assert service.handoff(device, payload)["status"] == "target_not_ready"
    assert service.audiobooks.pauses == 0


def test_handoff_stale_source_session_is_rejected(service):
    device, _, payload = setup_live(service)
    payload["sourceSessionId"] = "old-session"
    assert service.handoff(device, payload)["status"] == "revision_conflict"
    assert service.audiobooks.pauses == 0

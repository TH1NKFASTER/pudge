from pathlib import Path


def _html() -> str:
    return Path("pudge/web/index.html").read_text(encoding="utf-8")


def test_foreground_poll_detects_subtitle_state_changes_not_only_download_progress():
    html = _html()

    assert "function foregroundDataSignature(state)" in html
    assert "item.state||''" in html
    assert "item.subtitle?1:0" in html
    assert "const before=foregroundDataSignature(ui.state)" in html
    assert "const after=foregroundDataSignature(ui.state)" in html
    assert "if(before!==after)renderDataPages()" in html


def test_open_window_checks_tiny_state_versions_before_rebuilding_state():
    html = _html()

    assert "await pywebview.api.ui_state_versions()" in html
    assert "readyVersion!==knownReady||stateVersion!==knownState" in html
    assert "const next=await pywebview.api.get_state_fast()" in html
    assert "function pollBackgroundState()" not in html
    assert "backgroundStatePollDelay" not in html
    assert "const firstPermissionRequest=!ui.state.settings.permissions_requested" in html
    assert "await pywebview.api.request_permissions()" in html


def test_window_activity_reschedules_only_existing_lightweight_watchers():
    html = _html()

    assert "backgroundStatePolling:false" not in html
    assert "backgroundStatePollTimer:null" not in html
    assert "rescheduleBackgroundStatePoll()" not in html
    assert "void syncReadyStateVersion(true);scheduleReadyStateWatch(1000)" in html
    assert "rescheduleForegroundPoll();" in html

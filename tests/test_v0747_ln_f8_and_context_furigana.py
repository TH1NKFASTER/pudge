from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pudge.audiobooks as audiobook_module
from pudge.audiobooks import AudiobookService


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
WEB_APP = ROOT / "pudge/web_app.py"


def _service() -> AudiobookService:
    service = object.__new__(AudiobookService)
    service._lock = threading.Lock()
    process = Mock()
    process.poll.return_value = None
    service._players = {75: process}
    service._ipc_paths = {75: Path("/tmp/pudge-audiobook-test.sock")}
    service._last_positions = {75: 10.0}
    service._last_motion_at = {75: 100.0}
    return service


def test_f8_pause_falls_back_to_stalled_position_detection(monkeypatch) -> None:
    service = _service()
    service._ipc_get = Mock(side_effect=lambda _path, prop: False)
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 101.2)

    assert service.is_playback_active(75) is False


def test_advancing_position_refreshes_motion_clock(monkeypatch) -> None:
    service = _service()
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 105.0)

    service._record_playback_position(75, 10.5)

    assert service._last_positions[75] == 10.5
    assert service._last_motion_at[75] == 105.0


def test_resuming_externally_paused_mpv_uses_existing_process(monkeypatch) -> None:
    service = _service()
    service._ipc_commands_no_wait = Mock(return_value=True)
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 110.0)

    result = service.set_paused(75, False)

    assert result == {
        "ok": True,
        "book_id": 75,
        "playing": True,
        "player_running": True,
        "paused": False,
    }
    service._ipc_commands_no_wait.assert_called_once_with(
        Path("/tmp/pudge-audiobook-test.sock"),
        [["set_property", "pause", False]],
    )
    assert service._last_motion_at[75] == 110.0


def test_web_api_exposes_pause_resume_control() -> None:
    source = WEB_APP.read_text(encoding="utf-8")

    assert "def audiobook_set_paused(" in source
    assert "self.audiobooks.set_paused" in source


def test_ln_f8_pause_keeps_polling_and_cancels_animation() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "if(!desiredPlaying)cancelLnPairedInterpolation()" in source
    from test_v0106_ln_optimistic_transport_clock import _function, _run_node
    result = _run_node("""
let state={},timer=null,cancelled=0,applied=0;
global.ui={lnBook:{id:7,paired_audio:true},lnPairedPollGeneration:0};
global.$=()=>({classList:{contains:()=>true}});
global.setTimeout=(fn,delay)=>{timer={fn,delay};return 1;};
global.clearTimeout=()=>{};
global.pywebview={api:{light_novel_paired_state:async()=>state}};
global.lnPairedTransportClockReconcile=()=>({position:10,snap:false});
global.syncLnPairedTray=()=>{};
global.cancelLnPairedInterpolation=()=>{cancelled++;};
global.applyLnPairedPosition=async()=>{applied++;};
""" + _function(source, "pollLnPaired") + """
(async()=>{
 const rows=[];
 for(const sample of [{playing:true,player_running:true},
    {playing:false,player_running:true},
    {playing:false,player_running:false,alignment:{status:'aligning'}},
    {playing:false,player_running:false,alignment:{status:'ready'}}]){
   state=sample;pollLnPaired();const callback=timer.fn;timer=null;
   await callback();rows.push(timer?.delay??null);
 }
 console.log(JSON.stringify({rows,cancelled,applied}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    assert result["rows"] == [250, 350, 700, None]
    assert result["cancelled"] == 3
    assert result["applied"] == 1
    # Pause/resume of a live player no longer waits for alignment (plan §6).
    assert "if(!forceReaderStart&&state.player_running&&state.audiobook_id){" in source
    assert "if(appliedDesired!==wanted){" in source
    assert "ui.lnPairedTransportPromise" in source
    assert "pywebview.api.audiobook_set_paused" in source
    assert "Продолжить" in source
    assert "Resume" in source


def test_context_furigana_marks_and_reveals_all_furigana_ruby() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert source.count('class="ln-furigana-ruby"') >= 2
    assert (
        ".ln-reader.furigana-hover .ln-word:hover "
        "ruby.ln-furigana-ruby>rt" in source
    )
    assert (
        ".ln-reader.furigana-reading .ln-word.ln-paired-word-current "
        "ruby.ln-furigana-ruby>rt" in source
    )
    assert "visibility:visible!important" in source
    assert ".ln-word:hover .ln-context-furigana rt" not in source


def test_inline_javascript_parses() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(
            r"<script([^>]*)>(.*?)</script>", source, flags=re.I | re.S
        )
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts
    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-v0747-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)

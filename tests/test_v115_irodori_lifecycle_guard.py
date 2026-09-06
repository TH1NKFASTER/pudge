from pathlib import Path
import re

WEB_APP = Path(__file__).resolve().parents[1] / "pudge" / "web_app.py"

def _source() -> str:
    return WEB_APP.read_text(encoding="utf-8")

def test_managed_irodori_has_process_group_shutdown():
    source = _source()
    assert "def _stop_managed_irodori_server" in source
    assert "os.killpg(pgid, signal.SIGTERM)" in source
    assert "os.killpg(pgid, signal.SIGKILL)" in source
    assert "atexit.register(self._stop_managed_irodori_server)" in source

def test_settings_probe_does_not_keep_server_alive():
    source = _source()
    block = re.search(r"def test_irodori_tts\(.*?\n    def test_llm_provider", source, re.S)
    assert block is not None
    text = block.group(0)
    assert "keep_managed_alive: bool = False" in text
    assert "if started_here and not keep_managed_alive:" in text
    assert "self._stop_managed_irodori_server()" in text

def test_generation_is_only_internal_keepalive_path():
    source = _source()
    assert source.count("keep_managed_alive=True") == 1
    worker = re.search(r"def _irodori_tts_worker\(.*?\n    def _start_light_novel_tts", source, re.S)
    assert worker is not None
    text = worker.group(0)
    assert "keep_managed_alive=True" in text
    assert "other_generation_running" in text
    assert "self._stop_managed_irodori_server()" in text

def test_import_autogenerate_paths_are_guarded():
    source = _source()
    assert "if irodori.irodori_tts_enabled and irodori.irodori_tts_auto_generate:" not in source
    assert source.count("and self._audiobook_link_kind(") >= 3

from pathlib import Path


def test_prepare_result_meta_final_path_is_authoritative() -> None:
    source = Path("pudge/manager.py").read_text(encoding="utf-8")
    assert 'meta_final_path = str(subtitle_meta.get("final_path") or "").strip()' in source
    assert "prepared_from_meta = Path(meta_final_path).expanduser()" in source
    assert "subtitle = prepared_from_meta" in source
    assert "source=meta_final_path" in source

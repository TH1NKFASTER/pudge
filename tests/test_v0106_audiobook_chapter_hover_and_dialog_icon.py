from pathlib import Path
import tomllib

ROOT = Path(__file__).parents[1]


def test_audiobook_chapter_hover_marks_timeline_range() -> None:
    js = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")
    assert 'data-audio-chapter-start="${chapterStart}"' in js
    assert 'data-audio-chapter-end="${chapterEnd}"' in js
    assert 'data-audio-timeline' in js
    assert "const showAudioChapterHover = chapter =>" in js
    assert "marker.style.left=`${start/duration*100}%`" in js
    assert "marker.style.width=`${(end-start)/duration*100}%`" in js
    assert ".audiobook-chapter-hover{" in css
    assert "repeating-linear-gradient(135deg" in css
    assert "rgba(87,211,140,.92)" in css


def test_native_cocoa_dialogs_use_pudge_icon() -> None:
    source = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert 'runtime_icon = Path(__file__).resolve().parent / "assets" / "app-icon.png"' in source
    assert 'icon=str(runtime_icon) if runtime_icon.is_file() else None' in source


def test_pywebview_version_supports_cocoa_icon() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pywebview>=6.2,<7" in project["project"]["dependencies"]

from __future__ import annotations

from pathlib import Path

from pudge.relation_graphs import compact_relations_from_graph
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_other_edges_are_excluded_from_relation_graphs() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {"media_id": 2, "title": "Sequel"},
            {"media_id": 3, "title": "Other"},
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 1, "target": 3, "relation_type": "OTHER"},
        ],
    }
    assert [item["media_id"] for item in compact_relations_from_graph(graph, 1)] == [2]

    html = HTML.read_text(encoding="utf-8")
    assert html.count("'SHARED_CHARACTERS','RELATED','OTHER'") >= 3
    provider = (ROOT / "pudge" / "providers" / "anilist.py").read_text(encoding="utf-8")
    assert '{"CHARACTER", "SHARED_CHARACTERS", "RELATED", "OTHER"}' in provider


def test_library_subtitle_statuses_are_descriptive() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "'subtitle.none':'no subtitles'" in html
    assert "'subtitle.none':'субтитров нет'" in html
    assert "'library.state.local':'no subtitles'" in html
    assert "'library.state.local':'субтитров нет'" in html
    assert "t('label.imageSubsOnly')" in html
    assert "t('label.noSubs')" in html
    assert "String(state||'local').replaceAll" not in html


def test_image_subtitle_click_reveals_external_or_embedded_source() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "const revealPath=e.subtitle_path||(e.subtitle_source==='image'?e.video_path:'')" in html
    assert "data-path=\"${escapeHtml(revealPath)}\"" in html


def test_reveal_subtitle_accepts_pgs_and_embedded_video(tmp_path: Path, monkeypatch) -> None:
    api = WebAppApi(tmp_path / "config.toml")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda command, *args, **kwargs: calls.append(command),
    )

    pgs = tmp_path / "episode.sup"
    pgs.write_bytes(b"PG")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")

    assert api.reveal_subtitle_file(str(pgs))["ok"] is True
    assert api.reveal_subtitle_file(str(video))["ok"] is True
    assert calls == [
        ["open", "-R", str(pgs.resolve())],
        ["open", "-R", str(video.resolve())],
    ]


def test_general_settings_group_and_ready_notification_label() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "${t('settings.general')}" in html
    assert "'settings.general':'General'" in html
    assert "'settings.general':'Основное'" in html
    assert "'settings.notifications':'Notify when anime is ready'" in html
    assert "'settings.notifications':'Уведомлять, когда аниме готово к просмотру'" in html
    assert 'id="testNotification"' not in html
    assert "pywebview.api.test_notification()" not in html

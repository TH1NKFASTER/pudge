"""Relation graphs without OTHER edges, descriptive subtitle states, General settings
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations


from pudge.providers.anilist import AniListClient
from pudge.relation_graphs import compact_relations_from_graph
from tests.i18n_support import en, ru

# Root 1 with a sequel and entries that are only loosely related to it.
LOOSE = {"OTHER": 3, "CHARACTER": 4, "SHARED_CHARACTERS": 5, "RELATED": 6}


def test_other_edges_are_excluded_from_compact_relations() -> None:
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


def test_anilist_relation_graph_does_not_follow_loose_relations() -> None:
    def media(media_id: int, edges: list[tuple[str, int]]) -> dict:
        return {
            "id": media_id,
            "type": "ANIME",
            "format": "TV",
            "title": {"romaji": f"Entry {media_id}"},
            "relations": {
                "edges": [
                    {"relationType": kind, "node": {"id": target, "type": "ANIME", "format": "TV"}}
                    for kind, target in edges
                ]
            },
        }

    catalog = {1: media(1, [("SEQUEL", 2), *LOOSE.items()]), 2: media(2, [("PREQUEL", 1)])}
    catalog.update({target: media(target, []) for target in LOOSE.values()})
    client = AniListClient("https://anilist.invalid")
    client._post = lambda _query, variables: {
        "Page": {"media": [catalog[i] for i in variables["ids"] if i in catalog]}
    }
    try:
        graph = client.full_relation_graph(1)
    finally:
        client.close()
    assert sorted(node["media_id"] for node in graph["nodes"]) == [1, 2]
    assert {edge["relation_type"] for edge in graph["edges"]} <= {"SEQUEL", "PREQUEL"}


def test_watch_order_graph_shows_no_loose_relations(page) -> None:
    titles = {
        1: "Root Show",
        2: "Sequel Show",
        3: "Other Show",
        4: "Character Show",
        5: "Shared Show",
        6: "Related Show",
    }
    graph = {
        "graph_id": "g1",
        "root_id": 1,
        "nodes": [
            {"media_id": media_id, "title": title, "format": "TV"} for media_id, title in titles.items()
        ],
        "edges": [{"source": 1, "target": 2, "relation_type": "SEQUEL"}]
        + [
            {"source": 2 if kind == "SHARED_CHARACTERS" else 1, "target": target, "relation_type": kind}
            for kind, target in LOOSE.items()
        ],
    }
    result = page.run(
        """
        const opening = openWatchOrder(1);
        await __tick(300);
        await opening;
        return {open: __visible('#modalBackdrop'), text: __text('#modalBody')};
        """,
        state={"current": [{"media_id": 1, "title": "Root Show", "episodes": 12, "next_episode": 1}]},
        api={"get_relation_graph": graph},
    )
    assert result.value["open"] is True
    shown = [title for title in titles.values() if title in result.value["text"]]
    assert shown == ["Root Show", "Sequel Show"]


def test_planned_anime_subtitle_states_are_descriptive(page) -> None:
    assert en("subtitle.none") == "no subtitles"
    assert ru("subtitle.none") == "субтитров нет"
    assert en("library.state.local") == "no subtitles"
    assert ru("library.state.local") == "субтитров нет"
    planned = [
        {
            "media_id": 1,
            "title": "Image Subs",
            "media_kind": "anime",
            "episodes": 12,
            "media_status": "FINISHED",
            "local": {"state": "waiting_text_subtitles", "episode": 1},
        },
        {
            "media_id": 2,
            "title": "No Subs",
            "media_kind": "anime",
            "episodes": 12,
            "media_status": "FINISHED",
            "local": {"state": "local", "episode": 1},
        },
    ]
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('planned');
            await __tick(50);
            const card = id => document.querySelector(`#planned [data-media-id="${id}"]`).innerText.split('\\n');
            return {image: card(1), none: card(2), imageText: t('label.imageSubsOnly'), noneText: t('label.noSubs')};
            """,
            lang=lang,
            state={"planned": planned},
        )
        out = result.value
        assert out["imageText"] in out["image"] and out["noneText"] not in out["image"]
        assert out["noneText"] in out["none"]


def test_general_settings_group_and_ready_notification_label(page) -> None:
    assert en("settings.general") == "General"
    assert ru("settings.general") == "Основное"
    assert en("settings.notifications") == "Notify when anime is ready"
    assert ru("settings.notifications") == "Уведомлять, когда аниме готово к просмотру"
    for lang in ("en", "ru"):
        result = page.run(
            """
            const title = [...document.querySelectorAll('#settingsContent .setting-divider-title')].find(node => node.innerText === t('settings.general'));
            const group = title?.closest('.setting-block');
            const notifications = document.getElementById('s_notifications');
            const out = {
              grouped: ['s_language', 's_escape_fullscreen', 's_notifications'].map(id => Boolean(group?.querySelector('#' + id))),
              label: notifications.closest('label').innerText.trim(), expected: t('settings.notifications'),
              buttons: [...group.querySelectorAll('button')].filter(button => !button.matches('.pudge-select-button')).length,
              testButton: Boolean(document.getElementById('testNotification')),
            };
            await __input('#s_notifications', false);
            return out;
            """,
            lang=lang,
        )
        out = result.value
        assert out["grouped"] == [True, True, True]
        assert out["label"] == out["expected"]
        assert out["buttons"] == 0 and out["testButton"] is False  # the test-notification button is gone
        assert not result.called("test_notification")

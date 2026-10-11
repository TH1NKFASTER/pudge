from pathlib import Path

from css_support import css_rules

from pudge.providers.anilist import _overlay_relation_list_entries
from test_v0552_planning_relations import DIAGRAM, planning_relations_state


def test_completed_collection_entry_marks_nested_relation_watched() -> None:
    relations = [
        {
            "media_id": 20,
            "episodes": 12,
            "list_status": "",
            "progress": 0,
            "watched": False,
            "relations": [
                {
                    "media_id": 10,
                    "episodes": 24,
                    "list_status": "",
                    "progress": 0,
                    "watched": False,
                }
            ],
        }
    ]

    _overlay_relation_list_entries(
        relations,
        {
            20: {"status": "COMPLETED", "progress": 12, "episodes": 12},
            10: {"status": "COMPLETED", "progress": 24, "episodes": 24},
        },
    )

    assert relations[0]["watched"] is True
    assert relations[0]["list_status"] == "COMPLETED"
    assert relations[0]["relations"][0]["watched"] is True


PREVIEW = """
const preview = () => ({shown: $('relationPreview').classList.contains('show'), text: $('relationPreview').innerText,
                        cover: $('relationPreview').querySelector('img.relation-preview-cover')?.getAttribute('src') ?? null});
const hover = async (selector, type) => { await __fire(selector, type, {relatedTarget: document.body}, 'MouseEvent'); return preview(); };
"""


def test_planning_left_click_is_cover_only_and_hover_uses_large_preview(tmp_path: Path, page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            DIAGRAM
            + PREVIEW
            + """
            const node = '#plannedContent .relation-node[data-media-id="90"]';
            const out = {isButton: document.querySelector(node).tagName === 'BUTTON'};
            await __click(node + ' .relation-node-title');                     // not a link and not a menu
            out.titleClick = {menu: __visible('#contextMenu'), opened: __apiCalls.filter(c => c.name === 'open_url').length};
            closeModal();
            await __click(node + ' .relation-cover-action');                   // the relation's cover opens AniList
            await __click('#plannedContent > [data-media-id="100"] .cover-action');   // so does the card's cover
            out.over = await hover(node, 'mouseover');
            out.expected = [t('label.episodesTooltip', {count: 12}), t('label.yearTooltip', {year: 2024}),
                            t('label.studio', {studio: 'Sunrise'})];
            out.out = await hover(node, 'mouseout');
            return out;
            """,
            lang=lang,
            state=planning_relations_state(tmp_path / lang),
        )
        out = result.value
        assert out["isButton"] is False
        assert out["titleClick"] == {"menu": False, "opened": 0}
        assert result.calls_of("open_url") == [
            ["https://anilist.co/anime/90"],
            ["https://anilist.co/anime/100"],
        ]
        assert out["over"]["shown"] is True and out["over"]["cover"] == "https://img.example/90.jpg"
        assert out["over"]["text"].split("\n") == ["Prequel", *out["expected"]]
        assert out["out"] == {"shown": False, "text": "", "cover": None}
    rules = css_rules("index.html")
    assert (
        rules.get(".relation-preview", "opacity") == "0"
        and rules.get(".relation-preview.show", "opacity") == "1"
    )

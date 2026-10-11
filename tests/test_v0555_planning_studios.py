from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime
from pudge.providers.anilist import _primary_studio, _relation_node_payload
from pudge.web_app import WebAppApi
from test_v0552_planning_relations import DIAGRAM, planning_relations_state
from tests.i18n_support import en, ru


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_primary_animation_studio_is_preferred() -> None:
    media = {
        "studios": {
            "nodes": [
                {"name": "Producer", "isAnimationStudio": False},
                {"name": "Animation House", "isAnimationStudio": True},
            ]
        }
    }
    assert _primary_studio(media) == "Animation House"
    assert _primary_studio({"studios": {"nodes": [{"name": "Fallback"}]}}) == "Fallback"
    assert _primary_studio({}) == ""


def test_relation_payload_contains_studio() -> None:
    payload = _relation_node_payload(
        {
            "id": 10,
            "type": "ANIME",
            "title": {"romaji": "Prequel"},
            "studios": {"nodes": [{"name": "Bones", "isAnimationStudio": True}]},
        },
        "PREQUEL",
        include_children=False,
    )
    assert payload is not None
    assert payload["studio"] == "Bones"


def test_database_persists_studio(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Planned", studio="Madhouse"))
    assert db.get_anime(1).studio == "Madhouse"  # type: ignore[union-attr]


def test_planning_payload_contains_root_and_relation_studios(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=100,
            title="Current",
            status="PLANNING",
            studio="Kyoto Animation",
            relations=[
                {
                    "relation_type": "PREQUEL",
                    "media_id": 90,
                    "title": "Prequel",
                    "studio": "Sunrise",
                    "relations": [],
                }
            ],
        )
    )

    planned = api.get_state()["planned"][0]
    assert planned["studio"] == "Kyoto Animation"
    assert planned["relations"]["current"]["studio"] == "Kyoto Animation"
    assert planned["relations"]["prequel_levels"][-1][0]["studio"] == "Sunrise"


def test_planning_ui_shows_studio_only_in_row_and_large_relation_preview(tmp_path: Path, page) -> None:
    assert en("label.studio", studio="MAPPA") == "Studio: MAPPA"
    assert ru("label.studio", studio="MAPPA") == "Студия: MAPPA"
    for lang in ("en", "ru"):
        result = page.run(
            DIAGRAM
            + """
            const studio = id => __text(`#plannedContent > [data-media-id="${id}"] .planned-studio`);
            const out = {rows: [studio(100), studio(200)],
                         expected: [t('label.studio', {studio: 'Kyoto Animation'}), t('label.studio', {studio: 'Bones'})],
                         compact: relationNode(90).querySelector('.relation-node-meta').innerText};
            await __fire('#plannedContent .relation-node[data-media-id="90"]', 'mouseover', {relatedTarget: document.body}, 'MouseEvent');
            out.preview = $('relationPreview').innerText;
            out.previewStudio = t('label.studio', {studio: 'Sunrise'});
            // A Watching card has no studio row.
            out.watching = animeCard({media_id: 5, title: 'Watching', studio: 'MAPPA', progress: 1, episodes: 12}, false);
            return out;
            """,
            lang=lang,
            state=planning_relations_state(tmp_path / lang),
        )
        out = result.value
        assert out["rows"] == out["expected"]
        assert out["compact"] == "TV • 2024"  # the compact node: format and year only
        assert out["previewStudio"] in out["preview"].split("\n")
        assert "MAPPA" not in out["watching"]

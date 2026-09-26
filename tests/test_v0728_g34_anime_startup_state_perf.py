from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "pudge" / "web_app.py").read_text()


def _method_block(name: str, next_name: str) -> str:
    start = SOURCE.index(f"    def {name}(")
    end = SOURCE.index(f"    def {next_name}(", start)
    return SOURCE[start:end]


def test_ready_queue_builder_does_not_reload_entire_anime_library_per_card() -> None:
    block = _method_block("_ready_queue_items", "create_next_episodes_queue")
    assert "anime_list()" not in block
    assert "db.get_anime(media_id)" in block
    assert "anime_by_id: dict[int, LibraryAnime] | None = None" in block


def test_anime_payload_reuses_current_anime_for_queue_badges() -> None:
    block = _method_block("_anime_payload", "_continue_payloads")
    assert "known_anime = {int(anime.media_id): anime}" in block
    assert block.count("anime_by_id=known_anime") == 2


def test_full_state_timing_is_logged_for_runtime_regressions() -> None:
    block = _method_block("get_state", "get_state_fast")
    assert 'timed_step(self.logger, "web.get_state")' in block

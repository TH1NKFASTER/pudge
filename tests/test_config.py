from pathlib import Path

from pudge.config import (
    AniListConfig,
    AppConfig,
    LLMConfig,
    MatchingConfig,
    PathsConfig,
    SyncConfig,
    load_config,
    write_config,
)


def test_config_round_trip(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = AppConfig(
        paths=PathsConfig(download_dirs=[tmp_path / "Downloads"], cache_dir=tmp_path / "cache"),
        anilist=AniListConfig(
            enabled=True,
            endpoint="https://graphql.anilist.co",
            client_id="12345",
            access_token="token",
            auto_update_progress=True,
            watched_threshold=5 / 6,
            watched_max_remaining_minutes=7.5,
        ),
        llm=LLMConfig(
            enabled=True,
            base_url="http://localhost:11434",
            api_key="secret",
            model="qwen3.5:9b-q8_0",
            think=False,
            keep_alive="10m",
            num_ctx=8192,
            validate_embedded_reference=True,
            embedded_reference_sample_count=7,
            embedded_reference_phrases_per_sample=3,
            embedded_reference_min_similarity=0.7,
        ),
        matching=MatchingConfig(prefer_srt=True, evaluate_all_jimaku=True, max_jimaku_candidates=12),
        sync=SyncConfig(engine="auto", compare_engines=True, vad="auditok", fix_framerate=False, gss=True, alass_split_penalty=9.0),
        config_path=path,
    )

    config.ui.onboarding_completed = True
    write_config(config, path)
    loaded = load_config(path)

    assert loaded.ui.onboarding_completed is True
    assert loaded.anilist.client_id == "12345"
    assert loaded.anilist.access_token == "token"
    assert loaded.anilist.auto_update_progress is True
    assert loaded.anilist.watched_threshold == 5 / 6
    assert loaded.anilist.watched_max_remaining_minutes == 7.5
    assert loaded.llm.enabled is True
    assert loaded.llm.api_key == "secret"
    assert loaded.llm.model == "qwen3.5:9b-q8_0"
    assert loaded.llm.think is False
    assert loaded.llm.num_ctx == 8192
    assert loaded.llm.validate_embedded_reference is True
    assert loaded.llm.embedded_reference_sample_count == 7
    assert loaded.llm.embedded_reference_phrases_per_sample == 3
    assert loaded.llm.embedded_reference_min_similarity == 0.7
    assert loaded.matching.prefer_srt is True
    assert loaded.matching.evaluate_all_jimaku is True
    assert loaded.matching.max_jimaku_candidates == 12
    assert loaded.sync.engine == "auto"
    assert loaded.sync.compare_engines is True
    assert loaded.sync.alass_split_penalty == 9.0
    assert loaded.sync.vad == "auditok"
    assert loaded.sync.fix_framerate is False
    assert loaded.sync.gss is True


def test_load_config_accepts_string_path(tmp_path):
    from pudge.config import load_config

    path = tmp_path / "config.toml"
    config = load_config(str(path))

    assert config.config_path == path


def test_notifications_setting_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.ui.notifications_enabled = False
    write_config(config, path)

    loaded = load_config(path)

    assert loaded.ui.notifications_enabled is False

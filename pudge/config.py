from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .branding import CACHE_DIR, CONFIG_DIR, DEFAULT_DATABASE_PATH, DEFAULT_LIBRARY_DIR, QBITTORRENT_CATEGORY
from .jimaku_trial import apply_jimaku_trial, persisted_jimaku_api_key
from .secrets_store import SecretStore


APP_DIR = CONFIG_DIR
DEFAULT_CONFIG_PATH = APP_DIR / "config.toml"
DEFAULT_CACHE_DIR = CACHE_DIR
_SECRET_STORE = SecretStore()


@dataclass(slots=True)
class UIConfig:
    language: str = "en"
    onboarding_completed: bool = False
    escape_exits_fullscreen: bool = True
    notifications_enabled: bool = True
    permissions_requested: bool = False
    jiten_developer_tools_confirmed: bool = False


@dataclass(slots=True)
class PathsConfig:
    # Additional media folders watched by the Library scanner (for example ~/Downloads).
    # The historical key name is retained for config compatibility.
    download_dirs: list[Path] = field(default_factory=list)
    # Optional external folder(s) containing manually downloaded subtitle files.
    # An empty list means that pudge only checks the video's own directory
    # and its internal caches/Jimaku.
    subtitle_dirs: list[Path] = field(default_factory=list)
    cache_dir: Path = DEFAULT_CACHE_DIR
    max_scanned_files: int = 8000




@dataclass(slots=True)
class LibraryConfig:
    root_dir: Path = field(default_factory=lambda: DEFAULT_LIBRARY_DIR)
    database_path: Path = field(
        default_factory=lambda: DEFAULT_DATABASE_PATH
    )
    recursive: bool = True
    cover_cache_dir: Path = field(default_factory=lambda: DEFAULT_CACHE_DIR / "covers")
    disk_limit_enabled: bool = True
    disk_limit_gb: float = 500.0


@dataclass(slots=True)
class NyaaConfig:
    enabled: bool = True
    base_url: str = "https://nyaa.si"
    category: str = "1_2"
    proxy_mode: str = "direct_then_proxy"
    proxy_url: str = ""
    pre_search_command: str = ""
    auto_download_current: bool = True
    # Separate capability (configured backends) from the user's legal/intent
    # switch. Searches stay available while torrent traffic is off.
    torrents_enabled: bool = True
    subsplease_rss_enabled: bool = True
    subsplease_rss_preferred: bool = True
    auto_require_trusted: bool = True
    only_trusted_groups: bool = False
    min_release_score: float = 72.0
    min_seeders: int = 1
    preferred_resolution: str = "1080p"
    preferred_video_codecs: list[str] = field(
        default_factory=lambda: ["HEVC", "AV1", "AVC"]
    )
    preferred_sources: list[str] = field(
        default_factory=lambda: ["BluRay", "WEB-DL", "WEBRip"]
    )
    require_japanese_audio: bool = True
    avoid_upscaled: bool = True
    trusted_groups: list[str] = field(
        default_factory=lambda: ["Erai-raws", "SubsPlease", "NanakoRaws", "shincaps"]
    )
    preferred_groups: list[str] = field(
        default_factory=lambda: ["EMBER", "Judas", "Anime Time"]
    )
    blocked_groups: list[str] = field(default_factory=list)
    episode_min_size_mb: int = 250
    episode_max_size_mb: int = 3500
    max_auto_download_per_anime: int = 2
    auto_upgrade_downloaded: bool = True
    upgrade_min_score_gain: float = 30.0
    upgrade_check_hours: float = 24.0
    max_upgrade_checks_per_run: int = 2


@dataclass(slots=True)
class QBittorrentConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080"
    username: str = "admin"
    password: str = ""
    api_key: str = ""
    verify_tls: bool = True
    category: str = QBITTORRENT_CATEGORY
    pre_download_command: str = ""
    paused_on_add: bool = False
    auto_start_app: bool = True


@dataclass(slots=True)
class Aria2Config:
    enabled: bool = False
    binary: str = "aria2c"
    rpc_port: int = 6801
    auto_start: bool = True
    paused_on_add: bool = False
    seed_mode: str = "off"
    seed_ratio: float = 1.0
    seed_time_minutes: float = 120.0
    upload_limit_kib: int = 0
    vpn_interface: str = ""
    vpn_kill_switch: bool = False


@dataclass(slots=True)
class AgentConfig:
    enabled: bool = True
    # How often the background agent retries Nyaa after an episode airs.
    poll_minutes: int = 10
    # AniList metadata has a separate, slower cadence.
    anilist_refresh_minutes: int = 120
    subtitle_poll_minutes: int = 30
    delete_after_watched_hours: float = 24.0
    delete_only_managed_files: bool = True
    keep_batch_until_completed: bool = True


@dataclass(slots=True)
class PlaybackConfig:
    enabled: bool = True
    rewind_seconds: float = 15.0
    save_interval_seconds: float = 30.0


@dataclass(slots=True)
class ShortcutsConfig:
    # App navigation intentionally follows standard macOS conventions and is
    # not configurable: Cmd+1..N maps to the visible sidebar order and Cmd+F
    # focuses Planning search. Only mpv bindings are user-configurable.
    mpv_mark_watched: str = "Ctrl+a"
    mpv_open_anilist: str = "Ctrl+b"
    mpv_correct_match: str = "c"
    mpv_translate_subtitle: str = "Ctrl+t"


@dataclass(slots=True)
class DiagnosticsConfig:
    energy_monitoring_enabled: bool = False
    energy_sample_seconds: float = 30.0


@dataclass(slots=True)
class CompanionConfig:
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    port: int = 47821
    pairing_ttl_seconds: float = 300.0
    max_events_per_request: int = 500


@dataclass(slots=True)
class ToolsConfig:
    mpv: str = "mpv"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    alass: str = "alass"
    mpv_extra_args: list[str] = field(default_factory=list)
    # Only one interactive subtitle integration is loaded for Pudge playback.
    # ``auto`` keeps existing installs working by choosing the first available
    # integration without enabling an unconfigured plugin.
    mpv_study_plugin: str = "auto"


@dataclass(slots=True)
class JimakuConfig:
    api_key: str = ""
    base_url: str = "https://jimaku.cc"
    personal_api_key: str = ""
    trial_active: bool = False
    trial_expires_at: float = 0.0


@dataclass(slots=True)
class AniListConfig:
    enabled: bool = True
    endpoint: str = "https://graphql.anilist.co"
    client_id: str = ""
    access_token: str = ""
    auto_update_progress: bool = True
    watched_threshold: float = 0.85
    watched_max_remaining_minutes: float = 10.0
    add_if_missing: bool = False
    update_when_rewatching: bool = True
    completed_to_rewatching_on_episode_one: bool = False
    complete_current_final: bool = True
    complete_rewatching_final: bool = True
    mapping_cache_hours: float = 24.0
    relations_by_release_date: bool = True


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    model: str = "qwen3.5:9b-q8_0"
    ambiguity_margin: float = 8.0
    think: bool = False
    keep_alive: str = "10m"
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout_seconds: float = 90.0
    # Subtitle processing is deterministic by default. The local LLM remains
    # available for title ambiguity and an explicitly enabled semantic fallback.
    validate_embedded_reference: bool = False
    embedded_reference_sample_count: int = 6
    embedded_reference_phrases_per_sample: int = 4
    embedded_reference_min_similarity: float = 0.65


@dataclass(slots=True)
class MatchingConfig:
    local_min_score: float = 68.0
    jimaku_min_score: float = 45.0
    prefer_srt: bool = True
    convert_ass_to_srt: bool = True
    srt_alignment_tolerance_ratio: float = 0.002
    srt_alignment_tolerance_absolute: float = 50.0
    evaluate_all_jimaku: bool = True
    max_jimaku_candidates: int = 0
    ocr_image_subtitles: bool = False
    auto_upgrade_subtitles: bool = True
    subtitle_upgrade_min_score_gain: float = 25.0
    subtitle_upgrade_check_hours: float = 6.0
    max_subtitle_upgrade_checks_per_run: int = 2


@dataclass(slots=True)
class SyncConfig:
    enabled: bool = True
    engine: str = "auto"
    compare_engines: bool = True
    max_offset_seconds: float = 120.0
    quality_max_offset_seconds: float = 45.0
    skip_on_low_quality: bool = True
    vad: str = "subs_then_webrtc"
    fix_framerate: bool = True
    gss: bool = False
    alass_split_penalty: float = 7.0
    alass_timeout_seconds: float = 240.0
    segment_validation: bool = True
    segment_count: int = 5
    segment_window_seconds: float = 90.0
    segment_max_offset_seconds: float = 45.0
    piecewise_repair: bool = True
    piecewise_min_offset_seconds: float = 0.75
    piecewise_jump_threshold_seconds: float = 2.5
    piecewise_max_correction_seconds: float = 60.0
    pgs_onset_alignment: bool = True
    pgs_onset_pulse_seconds: float = 0.4
    pgs_onset_tolerance_seconds: float = 0.75
    pgs_onset_min_improvement: float = 0.08
    use_container_chapters: bool = True
    japanese_stt_fallback: bool = True
    japanese_stt_model: str = "mlx-community/whisper-tiny"
    japanese_stt_timeout_seconds: float = 600.0
    japanese_stt_min_activity: float = 0.55


@dataclass(slots=True)
class AppConfig:
    ui: UIConfig = field(default_factory=UIConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    nyaa: NyaaConfig = field(default_factory=NyaaConfig)
    qbittorrent: QBittorrentConfig = field(default_factory=QBittorrentConfig)
    aria2: Aria2Config = field(default_factory=Aria2Config)
    agent: AgentConfig = field(default_factory=AgentConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    shortcuts: ShortcutsConfig = field(default_factory=ShortcutsConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    jimaku: JimakuConfig = field(default_factory=JimakuConfig)
    anilist: AniListConfig = field(default_factory=AniListConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    config_path: Path = DEFAULT_CONFIG_PATH


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}



def _load_subtitle_dirs(paths: dict[str, Any]) -> list[Path]:
    """Load explicit subtitle folders without reviving the old implicit Downloads default."""
    explicit = paths.get("subtitle_dirs")
    if explicit is not None:
        return [_expand_path(p) for p in explicit]
    legacy = [_expand_path(p) for p in paths.get("download_dirs", [])]
    implicit_downloads = _expand_path(Path.home() / "Downloads")
    return [path for path in legacy if path != implicit_downloads]


def _load_watched_media_dirs(paths: dict[str, Any]) -> list[Path]:
    """Load explicit media-watch folders without reviving legacy subtitle dirs.

    v0.6.37 briefly reused the historical ``download_dirs`` key for media
    watching. Older installations, however, often contain the same key solely
    because it used to mean an external subtitle/Downloads folder. Prefer the
    new unambiguous key and only keep the v0.6.37 value when it is clearly
    distinct from the configured subtitle folders.
    """
    explicit = paths.get("watched_media_dirs")
    if explicit is not None:
        return [_expand_path(p) for p in explicit]

    legacy = [_expand_path(p) for p in paths.get("download_dirs", [])]
    if not legacy:
        return []
    subtitle_raw = paths.get("subtitle_dirs")
    if subtitle_raw is None:
        return []
    subtitle = [_expand_path(p) for p in subtitle_raw]
    legacy_keys = {str(path.expanduser().resolve()) for path in legacy}
    subtitle_keys = {str(path.expanduser().resolve()) for path in subtitle}
    if legacy_keys == subtitle_keys:
        return []
    # This is most likely a folder explicitly added through v0.6.37/38's
    # watched-folder UI, where subtitle_dirs had already diverged. Migrate it
    # once and write it back under watched_media_dirs on the next save.
    return legacy

def load_config(path: Path | None = None) -> AppConfig:
    config_path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    use_keychain = config_path.resolve() == DEFAULT_CONFIG_PATH.expanduser().resolve()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)

    ui = _section(raw, "ui")
    paths = _section(raw, "paths")
    library = _section(raw, "library")
    nyaa = _section(raw, "nyaa")
    qbittorrent = _section(raw, "qbittorrent")
    aria2 = _section(raw, "aria2")
    agent = _section(raw, "agent")
    playback = _section(raw, "playback")
    shortcuts = _section(raw, "shortcuts")
    diagnostics = _section(raw, "diagnostics")
    companion = _section(raw, "companion")
    tools = _section(raw, "tools")
    jimaku = _section(raw, "jimaku")
    anilist = _section(raw, "anilist")
    llm = _section(raw, "llm")
    matching = _section(raw, "matching")
    sync = _section(raw, "sync")

    cfg = AppConfig(
        ui=UIConfig(
            language=str(ui.get("language", "en")).lower() if str(ui.get("language", "en")).lower() in {"en", "ru"} else "en",
            onboarding_completed=bool(ui.get("onboarding_completed", False)),
            escape_exits_fullscreen=bool(ui.get("escape_exits_fullscreen", True)),
            notifications_enabled=bool(ui.get("notifications_enabled", True)),
            permissions_requested=bool(ui.get("permissions_requested", False)),
            jiten_developer_tools_confirmed=bool(ui.get("jiten_developer_tools_confirmed", False)),
        ),
        paths=PathsConfig(
            download_dirs=_load_watched_media_dirs(paths),
            subtitle_dirs=_load_subtitle_dirs(paths),
            cache_dir=_expand_path(paths.get("cache_dir", DEFAULT_CACHE_DIR)),
            max_scanned_files=int(paths.get("max_scanned_files", 8000)),
        ),
        library=LibraryConfig(
            root_dir=_expand_path(library.get("root_dir", DEFAULT_LIBRARY_DIR)),
            database_path=_expand_path(
                library.get("database_path", DEFAULT_DATABASE_PATH)
            ),
            recursive=bool(library.get("recursive", True)),
            cover_cache_dir=_expand_path(
                library.get("cover_cache_dir", DEFAULT_CACHE_DIR / "covers")
            ),
            disk_limit_enabled=bool(library.get("disk_limit_enabled", True)),
            disk_limit_gb=max(0.0, float(library.get("disk_limit_gb", 500.0))),
        ),
        nyaa=NyaaConfig(
            enabled=bool(nyaa.get("enabled", True)),
            base_url=str(nyaa.get("base_url", "https://nyaa.si")).rstrip("/"),
            category=str(nyaa.get("category", "1_2")),
            proxy_mode=str(nyaa.get("proxy_mode", "direct_then_proxy")),
            proxy_url=str(nyaa.get("proxy_url", "")).strip(),
            pre_search_command=str(nyaa.get("pre_search_command", "")).strip(),
            auto_download_current=bool(nyaa.get("auto_download_current", True)),
            torrents_enabled=bool(nyaa.get("torrents_enabled", False)),
            subsplease_rss_enabled=True,
            subsplease_rss_preferred=True,
            auto_require_trusted=bool(nyaa.get("auto_require_trusted", True)),
            only_trusted_groups=bool(nyaa.get("only_trusted_groups", False)),
            min_release_score=float(nyaa.get("min_release_score", 72.0)),
            min_seeders=int(nyaa.get("min_seeders", 1)),
            preferred_resolution=str(nyaa.get("preferred_resolution", "1080p")),
            preferred_video_codecs=[str(x) for x in nyaa.get(
                "preferred_video_codecs", ["HEVC", "AV1", "AVC"]
            )],
            preferred_sources=[str(x) for x in nyaa.get(
                "preferred_sources", ["BluRay", "WEB-DL", "WEBRip"]
            )],
            require_japanese_audio=True,
            avoid_upscaled=True,
            trusted_groups=[str(x) for x in nyaa.get(
                "trusted_groups", ["Erai-raws", "SubsPlease", "NanakoRaws", "shincaps"]
            )],
            preferred_groups=[str(x) for x in nyaa.get(
                "preferred_groups", ["EMBER", "Judas", "Anime Time"]
            )],
            blocked_groups=[str(x) for x in nyaa.get("blocked_groups", [])],
            episode_min_size_mb=int(nyaa.get("episode_min_size_mb", 250)),
            episode_max_size_mb=int(nyaa.get("episode_max_size_mb", 3500)),
            max_auto_download_per_anime=int(nyaa.get("max_auto_download_per_anime", 2)),
            auto_upgrade_downloaded=bool(nyaa.get("auto_upgrade_downloaded", True)),
            upgrade_min_score_gain=30.0,
            upgrade_check_hours=float(nyaa.get("upgrade_check_hours", 24.0)),
            max_upgrade_checks_per_run=int(nyaa.get("max_upgrade_checks_per_run", 2)),
        ),
        qbittorrent=QBittorrentConfig(
            enabled=bool(qbittorrent.get("enabled", False)),
            base_url=str(qbittorrent.get("base_url", "http://127.0.0.1:8080")).rstrip("/"),
            username=str(qbittorrent.get("username", "admin")),
            password=_SECRET_STORE.resolve(
                "qbittorrent-password",
                qbittorrent.get("password", ""),
                env="PUDGE_QBITTORRENT_PASSWORD",
                use_keychain=use_keychain,
            ),
            api_key=_SECRET_STORE.resolve(
                "qbittorrent-api-key",
                qbittorrent.get("api_key", ""),
                env="PUDGE_QBITTORRENT_API_KEY",
                use_keychain=use_keychain,
            ),
            verify_tls=bool(qbittorrent.get("verify_tls", True)),
            category=str(qbittorrent.get("category", QBITTORRENT_CATEGORY)),
            pre_download_command=str(qbittorrent.get("pre_download_command", "")).strip(),
            paused_on_add=bool(qbittorrent.get("paused_on_add", False)),
            auto_start_app=bool(qbittorrent.get("auto_start_app", True)),
        ),
        aria2=Aria2Config(
            enabled=bool(aria2.get("enabled", False)),
            binary="aria2c",
            rpc_port=max(1024, min(65535, int(aria2.get("rpc_port", 6801)))),
            auto_start=bool(aria2.get("auto_start", True)),
            paused_on_add=bool(aria2.get("paused_on_add", False)),
            seed_mode=str(aria2.get("seed_mode", "off")).strip().casefold(),
            seed_ratio=max(0.0, float(aria2.get("seed_ratio", 1.0))),
            seed_time_minutes=max(0.0, float(aria2.get("seed_time_minutes", 120.0))),
            upload_limit_kib=max(0, int(aria2.get("upload_limit_kib", 0))),
            vpn_interface=str(aria2.get("vpn_interface", "")).strip(),
            vpn_kill_switch=bool(aria2.get("vpn_kill_switch", False)),
        ),
        agent=AgentConfig(
            enabled=bool(agent.get("enabled", True)),
            # v0.5.50 deliberately uses a new key so installations that still
            # carry the old 30-minute generic poll adopt the new 10-minute
            # torrent retry cadence automatically.
            poll_minutes=max(5, int(agent.get("torrent_poll_minutes", 10))),
            anilist_refresh_minutes=max(5, int(agent.get("anilist_refresh_minutes", 120))),
            subtitle_poll_minutes=int(agent.get("subtitle_poll_minutes", 30)),
            delete_after_watched_hours=float(agent.get("delete_after_watched_hours", 24.0)),
            delete_only_managed_files=bool(agent.get("delete_only_managed_files", True)),
            keep_batch_until_completed=bool(agent.get("keep_batch_until_completed", True)),
        ),
        playback=PlaybackConfig(
            enabled=True,
            rewind_seconds=10.0,
            save_interval_seconds=max(10.0, float(playback.get("save_interval_seconds", 30.0))),
        ),
        shortcuts=ShortcutsConfig(
            mpv_mark_watched=str(shortcuts.get("mpv_mark_watched", "Ctrl+a")).strip(),
            mpv_open_anilist=str(shortcuts.get("mpv_open_anilist", "Ctrl+b")).strip(),
            mpv_correct_match=str(shortcuts.get("mpv_correct_match", "c")).strip(),
            mpv_translate_subtitle=str(shortcuts.get("mpv_translate_subtitle", "Ctrl+t")).strip(),
        ),
        diagnostics=DiagnosticsConfig(
            energy_monitoring_enabled=bool(diagnostics.get("energy_monitoring_enabled", False)),
            energy_sample_seconds=max(10.0, float(diagnostics.get("energy_sample_seconds", 30.0))),
        ),
        companion=CompanionConfig(
            enabled=bool(companion.get("enabled", False)),
            bind_host=str(companion.get("bind_host", "127.0.0.1")).strip() or "127.0.0.1",
            port=max(1024, min(65535, int(companion.get("port", 47821)))),
            pairing_ttl_seconds=max(30.0, float(companion.get("pairing_ttl_seconds", 300.0))),
            max_events_per_request=max(1, min(2000, int(companion.get("max_events_per_request", 500)))),
        ),
        tools=ToolsConfig(
            mpv=str(tools.get("mpv", "mpv")),
            ffmpeg=str(tools.get("ffmpeg", "ffmpeg")),
            ffprobe=str(tools.get("ffprobe", "ffprobe")),
            alass=str(tools.get("alass", "alass")),
            mpv_extra_args=[str(x) for x in tools.get("mpv_extra_args", [])],
            mpv_study_plugin=str(tools.get("mpv_study_plugin", "auto")).strip().casefold(),
        ),
        jimaku=JimakuConfig(
            api_key=_SECRET_STORE.resolve(
                "jimaku-api-key",
                jimaku.get("api_key", ""),
                env="JIMAKU_API_KEY",
                use_keychain=use_keychain,
            ),
            personal_api_key=_SECRET_STORE.resolve(
                "jimaku-api-key",
                jimaku.get("api_key", ""),
                env="JIMAKU_API_KEY",
                use_keychain=use_keychain,
            ),
            base_url=str(jimaku.get("base_url", "https://jimaku.cc")).rstrip("/"),
        ),
        anilist=AniListConfig(
            enabled=bool(anilist.get("enabled", True)),
            endpoint=str(anilist.get("endpoint", "https://graphql.anilist.co")),
            client_id=str(anilist.get("client_id", "")).strip(),
            access_token=_SECRET_STORE.resolve(
                "anilist-access-token",
                anilist.get("access_token", ""),
                env="ANILIST_ACCESS_TOKEN",
                use_keychain=use_keychain,
            ),
            auto_update_progress=bool(anilist.get("auto_update_progress", True)),
            watched_threshold=0.85,
            watched_max_remaining_minutes=10.0,
            add_if_missing=bool(anilist.get("add_if_missing", False)),
            update_when_rewatching=bool(anilist.get("update_when_rewatching", True)),
            completed_to_rewatching_on_episode_one=bool(
                anilist.get("completed_to_rewatching_on_episode_one", False)
            ),
            complete_current_final=bool(anilist.get("complete_current_final", True)),
            complete_rewatching_final=bool(anilist.get("complete_rewatching_final", True)),
            mapping_cache_hours=float(anilist.get("mapping_cache_hours", 24.0)),
            relations_by_release_date=bool(anilist.get("relations_by_release_date", True)),
        ),
        llm=LLMConfig(
            enabled=bool(llm.get("enabled", False)),
            base_url=str(llm.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            api_key=_SECRET_STORE.resolve(
                "llm-api-key",
                llm.get("api_key", ""),
                env="PUDGE_LLM_API_KEY",
                use_keychain=use_keychain,
            ),
            model=str(llm.get("model", "qwen3.5:9b-q8_0")),
            ambiguity_margin=float(llm.get("ambiguity_margin", 8.0)),
            think=bool(llm.get("think", False)),
            keep_alive=str(llm.get("keep_alive", "10m")),
            temperature=float(llm.get("temperature", 0.0)),
            num_ctx=int(llm.get("num_ctx", 8192)),
            timeout_seconds=float(llm.get("timeout_seconds", 90.0)),
            validate_embedded_reference=bool(llm.get("subtitle_semantic_checks", False)),
            embedded_reference_sample_count=int(llm.get("embedded_reference_sample_count", 6)),
            embedded_reference_phrases_per_sample=int(llm.get("embedded_reference_phrases_per_sample", 4)),
            embedded_reference_min_similarity=float(llm.get("embedded_reference_min_similarity", 0.65)),
        ),
        matching=MatchingConfig(
            local_min_score=float(matching.get("local_min_score", 68.0)),
            jimaku_min_score=float(matching.get("jimaku_min_score", 45.0)),
            prefer_srt=bool(matching.get("prefer_srt", True)),
            convert_ass_to_srt=bool(matching.get("convert_ass_to_srt", True)),
            srt_alignment_tolerance_ratio=float(matching.get("srt_alignment_tolerance_ratio", 0.002)),
            srt_alignment_tolerance_absolute=float(matching.get("srt_alignment_tolerance_absolute", 50.0)),
            evaluate_all_jimaku=bool(matching.get("evaluate_all_jimaku", True)),
            max_jimaku_candidates=int(matching.get("max_jimaku_candidates", 0)),
            ocr_image_subtitles=bool(matching.get("ocr_image_subtitles", False)),
            auto_upgrade_subtitles=True,
            subtitle_upgrade_min_score_gain=25.0,
            subtitle_upgrade_check_hours=6.0,
            max_subtitle_upgrade_checks_per_run=2,
        ),
        sync=SyncConfig(
            enabled=bool(sync.get("enabled", True)),
            engine=str(sync.get("engine", "auto")),
            compare_engines=bool(sync.get("compare_engines", True)),
            max_offset_seconds=float(sync.get("max_offset_seconds", 120.0)),
            quality_max_offset_seconds=float(sync.get("quality_max_offset_seconds", 45.0)),
            skip_on_low_quality=bool(sync.get("skip_on_low_quality", True)),
            vad=str(sync.get("vad", "subs_then_webrtc")),
            fix_framerate=bool(sync.get("fix_framerate", True)),
            gss=bool(sync.get("gss", False)),
            alass_split_penalty=float(sync.get("alass_split_penalty", 7.0)),
            alass_timeout_seconds=float(sync.get("alass_timeout_seconds", 240.0)),
            segment_validation=bool(sync.get("segment_validation", True)),
            segment_count=int(sync.get("segment_count", 5)),
            segment_window_seconds=float(sync.get("segment_window_seconds", 90.0)),
            segment_max_offset_seconds=float(sync.get("segment_max_offset_seconds", 45.0)),
            piecewise_repair=bool(sync.get("piecewise_repair", True)),
            piecewise_min_offset_seconds=float(sync.get("piecewise_min_offset_seconds", 0.75)),
            piecewise_jump_threshold_seconds=float(sync.get("piecewise_jump_threshold_seconds", 2.5)),
            piecewise_max_correction_seconds=float(sync.get("piecewise_max_correction_seconds", 60.0)),
            pgs_onset_alignment=bool(sync.get("pgs_onset_alignment", True)),
            pgs_onset_pulse_seconds=float(sync.get("pgs_onset_pulse_seconds", 0.4)),
            pgs_onset_tolerance_seconds=float(sync.get("pgs_onset_tolerance_seconds", 0.75)),
            pgs_onset_min_improvement=float(sync.get("pgs_onset_min_improvement", 0.08)),
            use_container_chapters=True,
            japanese_stt_fallback=True,
            japanese_stt_model=str(
                sync.get("japanese_stt_model", "mlx-community/whisper-tiny")
            ).strip() or "mlx-community/whisper-tiny",
            japanese_stt_timeout_seconds=max(
                60.0, float(sync.get("japanese_stt_timeout_seconds", 600.0))
            ),
            japanese_stt_min_activity=max(
                0.0, min(1.0, float(sync.get("japanese_stt_min_activity", 0.55)))
            ),
        ),
        config_path=config_path,
    )
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.library.cover_cache_dir.mkdir(parents=True, exist_ok=True)
    apply_jimaku_trial(cfg)
    return cfg


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_string_list(values: list[str | Path]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def write_config(config: AppConfig, destination: Path | None = None) -> Path:
    destination = (destination or config.config_path or DEFAULT_CONFIG_PATH).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    use_keychain = destination.resolve() == DEFAULT_CONFIG_PATH.expanduser().resolve()
    qbt_password = _SECRET_STORE.persisted_config_value(
        "qbittorrent-password", config.qbittorrent.password, use_keychain=use_keychain
    )
    qbt_api_key = _SECRET_STORE.persisted_config_value(
        "qbittorrent-api-key", config.qbittorrent.api_key, use_keychain=use_keychain
    )
    jimaku_api_key = _SECRET_STORE.persisted_config_value(
        "jimaku-api-key",
        persisted_jimaku_api_key(config.jimaku),
        use_keychain=use_keychain,
    )
    anilist_token = _SECRET_STORE.persisted_config_value(
        "anilist-access-token", config.anilist.access_token, use_keychain=use_keychain
    )
    llm_api_key = _SECRET_STORE.persisted_config_value(
        "llm-api-key", config.llm.api_key, use_keychain=use_keychain
    )
    text = f'''[ui]
language = {_toml_string(config.ui.language)}
onboarding_completed = {_toml_bool(config.ui.onboarding_completed)}
escape_exits_fullscreen = {_toml_bool(config.ui.escape_exits_fullscreen)}
notifications_enabled = {_toml_bool(config.ui.notifications_enabled)}
permissions_requested = {_toml_bool(config.ui.permissions_requested)}
jiten_developer_tools_confirmed = {_toml_bool(config.ui.jiten_developer_tools_confirmed)}

[paths]
watched_media_dirs = {_toml_string_list(config.paths.download_dirs)}
subtitle_dirs = {_toml_string_list(config.paths.subtitle_dirs)}
cache_dir = {_toml_string(config.paths.cache_dir)}
max_scanned_files = {config.paths.max_scanned_files}

[library]
root_dir = {_toml_string(config.library.root_dir)}
database_path = {_toml_string(config.library.database_path)}
recursive = {_toml_bool(config.library.recursive)}
cover_cache_dir = {_toml_string(config.library.cover_cache_dir)}
disk_limit_enabled = {_toml_bool(config.library.disk_limit_enabled)}
disk_limit_gb = {config.library.disk_limit_gb}

[nyaa]
enabled = {_toml_bool(config.nyaa.enabled)}
base_url = {_toml_string(config.nyaa.base_url)}
category = {_toml_string(config.nyaa.category)}
proxy_mode = {_toml_string(config.nyaa.proxy_mode)}
proxy_url = {_toml_string(config.nyaa.proxy_url)}
pre_search_command = {_toml_string(config.nyaa.pre_search_command)}
auto_download_current = {_toml_bool(config.nyaa.auto_download_current)}
torrents_enabled = {_toml_bool(config.nyaa.torrents_enabled)}
subsplease_rss_enabled = true
subsplease_rss_preferred = true
auto_require_trusted = {_toml_bool(config.nyaa.auto_require_trusted)}
only_trusted_groups = {_toml_bool(config.nyaa.only_trusted_groups)}
min_release_score = {config.nyaa.min_release_score}
min_seeders = {config.nyaa.min_seeders}
preferred_resolution = {_toml_string(config.nyaa.preferred_resolution)}
preferred_video_codecs = {_toml_string_list(config.nyaa.preferred_video_codecs)}
preferred_sources = {_toml_string_list(config.nyaa.preferred_sources)}
require_japanese_audio = {_toml_bool(config.nyaa.require_japanese_audio)}
avoid_upscaled = {_toml_bool(config.nyaa.avoid_upscaled)}
trusted_groups = {_toml_string_list(config.nyaa.trusted_groups)}
preferred_groups = {_toml_string_list(config.nyaa.preferred_groups)}
blocked_groups = {_toml_string_list(config.nyaa.blocked_groups)}
episode_min_size_mb = {config.nyaa.episode_min_size_mb}
episode_max_size_mb = {config.nyaa.episode_max_size_mb}
max_auto_download_per_anime = {config.nyaa.max_auto_download_per_anime}
auto_upgrade_downloaded = {_toml_bool(config.nyaa.auto_upgrade_downloaded)}
upgrade_min_score_gain = {config.nyaa.upgrade_min_score_gain}
upgrade_check_hours = {config.nyaa.upgrade_check_hours}
max_upgrade_checks_per_run = {config.nyaa.max_upgrade_checks_per_run}

[qbittorrent]
enabled = {_toml_bool(config.qbittorrent.enabled)}
base_url = {_toml_string(config.qbittorrent.base_url)}
username = {_toml_string(config.qbittorrent.username)}
password = {_toml_string(qbt_password)}
api_key = {_toml_string(qbt_api_key)}
verify_tls = {_toml_bool(config.qbittorrent.verify_tls)}
category = {_toml_string(config.qbittorrent.category)}
pre_download_command = {_toml_string(config.qbittorrent.pre_download_command)}
paused_on_add = {_toml_bool(config.qbittorrent.paused_on_add)}
auto_start_app = {_toml_bool(config.qbittorrent.auto_start_app)}

[aria2]
enabled = {_toml_bool(config.aria2.enabled)}
binary = {_toml_string(config.aria2.binary)}
rpc_port = {config.aria2.rpc_port}
auto_start = {_toml_bool(config.aria2.auto_start)}
paused_on_add = {_toml_bool(config.aria2.paused_on_add)}
seed_mode = {_toml_string(config.aria2.seed_mode)}
seed_ratio = {config.aria2.seed_ratio}
seed_time_minutes = {config.aria2.seed_time_minutes}
upload_limit_kib = {config.aria2.upload_limit_kib}
vpn_interface = {_toml_string(config.aria2.vpn_interface)}
vpn_kill_switch = {_toml_bool(config.aria2.vpn_kill_switch)}

[agent]
enabled = {_toml_bool(config.agent.enabled)}
torrent_poll_minutes = {config.agent.poll_minutes}
anilist_refresh_minutes = {config.agent.anilist_refresh_minutes}
subtitle_poll_minutes = {config.agent.subtitle_poll_minutes}
delete_after_watched_hours = {config.agent.delete_after_watched_hours}
delete_only_managed_files = {_toml_bool(config.agent.delete_only_managed_files)}
keep_batch_until_completed = {_toml_bool(config.agent.keep_batch_until_completed)}

[playback]
enabled = {_toml_bool(config.playback.enabled)}
rewind_seconds = {config.playback.rewind_seconds}
save_interval_seconds = {config.playback.save_interval_seconds}

[shortcuts]
mpv_mark_watched = {_toml_string(config.shortcuts.mpv_mark_watched)}
mpv_open_anilist = {_toml_string(config.shortcuts.mpv_open_anilist)}
mpv_correct_match = {_toml_string(config.shortcuts.mpv_correct_match)}
mpv_translate_subtitle = {_toml_string(config.shortcuts.mpv_translate_subtitle)}

[diagnostics]
energy_monitoring_enabled = {_toml_bool(config.diagnostics.energy_monitoring_enabled)}
energy_sample_seconds = {config.diagnostics.energy_sample_seconds}

[companion]
enabled = {_toml_bool(config.companion.enabled)}
bind_host = {_toml_string(config.companion.bind_host)}
port = {config.companion.port}
pairing_ttl_seconds = {config.companion.pairing_ttl_seconds}
max_events_per_request = {config.companion.max_events_per_request}

[tools]
mpv = {_toml_string(config.tools.mpv)}
ffmpeg = {_toml_string(config.tools.ffmpeg)}
ffprobe = {_toml_string(config.tools.ffprobe)}
alass = {_toml_string(config.tools.alass)}
mpv_extra_args = {_toml_string_list(config.tools.mpv_extra_args)}
mpv_study_plugin = {_toml_string(config.tools.mpv_study_plugin)}

[jimaku]
api_key = {_toml_string(jimaku_api_key)}
base_url = {_toml_string(config.jimaku.base_url)}

[anilist]
enabled = {_toml_bool(config.anilist.enabled)}
endpoint = {_toml_string(config.anilist.endpoint)}
client_id = {_toml_string(config.anilist.client_id)}
access_token = {_toml_string(anilist_token)}
auto_update_progress = {_toml_bool(config.anilist.auto_update_progress)}
watched_threshold = {config.anilist.watched_threshold}
watched_max_remaining_minutes = {config.anilist.watched_max_remaining_minutes}
add_if_missing = {_toml_bool(config.anilist.add_if_missing)}
update_when_rewatching = {_toml_bool(config.anilist.update_when_rewatching)}
completed_to_rewatching_on_episode_one = {_toml_bool(config.anilist.completed_to_rewatching_on_episode_one)}
complete_current_final = {_toml_bool(config.anilist.complete_current_final)}
complete_rewatching_final = {_toml_bool(config.anilist.complete_rewatching_final)}
mapping_cache_hours = {config.anilist.mapping_cache_hours}
relations_by_release_date = {_toml_bool(config.anilist.relations_by_release_date)}

[llm]
enabled = {_toml_bool(config.llm.enabled)}
base_url = {_toml_string(config.llm.base_url)}
api_key = {_toml_string(llm_api_key)}
model = {_toml_string(config.llm.model)}
ambiguity_margin = {config.llm.ambiguity_margin}
think = {_toml_bool(config.llm.think)}
keep_alive = {_toml_string(config.llm.keep_alive)}
temperature = {config.llm.temperature}
num_ctx = {config.llm.num_ctx}
timeout_seconds = {config.llm.timeout_seconds}
subtitle_semantic_checks = {_toml_bool(config.llm.validate_embedded_reference)}
embedded_reference_sample_count = {config.llm.embedded_reference_sample_count}
embedded_reference_phrases_per_sample = {config.llm.embedded_reference_phrases_per_sample}
embedded_reference_min_similarity = {config.llm.embedded_reference_min_similarity}

[matching]
local_min_score = {config.matching.local_min_score}
jimaku_min_score = {config.matching.jimaku_min_score}
prefer_srt = {_toml_bool(config.matching.prefer_srt)}
convert_ass_to_srt = {_toml_bool(config.matching.convert_ass_to_srt)}
srt_alignment_tolerance_ratio = {config.matching.srt_alignment_tolerance_ratio}
srt_alignment_tolerance_absolute = {config.matching.srt_alignment_tolerance_absolute}
evaluate_all_jimaku = {_toml_bool(config.matching.evaluate_all_jimaku)}
max_jimaku_candidates = {config.matching.max_jimaku_candidates}
ocr_image_subtitles = {_toml_bool(config.matching.ocr_image_subtitles)}
auto_upgrade_subtitles = {_toml_bool(config.matching.auto_upgrade_subtitles)}
subtitle_upgrade_min_score_gain = {config.matching.subtitle_upgrade_min_score_gain}
subtitle_upgrade_check_hours = {config.matching.subtitle_upgrade_check_hours}
max_subtitle_upgrade_checks_per_run = {config.matching.max_subtitle_upgrade_checks_per_run}

[sync]
enabled = {_toml_bool(config.sync.enabled)}
engine = {_toml_string(config.sync.engine)}
compare_engines = {_toml_bool(config.sync.compare_engines)}
max_offset_seconds = {config.sync.max_offset_seconds}
quality_max_offset_seconds = {config.sync.quality_max_offset_seconds}
skip_on_low_quality = {_toml_bool(config.sync.skip_on_low_quality)}
vad = {_toml_string(config.sync.vad)}
fix_framerate = {_toml_bool(config.sync.fix_framerate)}
gss = {_toml_bool(config.sync.gss)}
alass_split_penalty = {config.sync.alass_split_penalty}
alass_timeout_seconds = {config.sync.alass_timeout_seconds}
segment_validation = {_toml_bool(config.sync.segment_validation)}
segment_count = {config.sync.segment_count}
segment_window_seconds = {config.sync.segment_window_seconds}
segment_max_offset_seconds = {config.sync.segment_max_offset_seconds}
piecewise_repair = {_toml_bool(config.sync.piecewise_repair)}
piecewise_min_offset_seconds = {config.sync.piecewise_min_offset_seconds}
piecewise_jump_threshold_seconds = {config.sync.piecewise_jump_threshold_seconds}
piecewise_max_correction_seconds = {config.sync.piecewise_max_correction_seconds}
pgs_onset_alignment = {_toml_bool(config.sync.pgs_onset_alignment)}
pgs_onset_pulse_seconds = {config.sync.pgs_onset_pulse_seconds}
pgs_onset_tolerance_seconds = {config.sync.pgs_onset_tolerance_seconds}
pgs_onset_min_improvement = {config.sync.pgs_onset_min_improvement}
use_container_chapters = {_toml_bool(config.sync.use_container_chapters)}
japanese_stt_fallback = {_toml_bool(config.sync.japanese_stt_fallback)}
japanese_stt_model = {_toml_string(config.sync.japanese_stt_model)}
japanese_stt_timeout_seconds = {config.sync.japanese_stt_timeout_seconds}
japanese_stt_min_activity = {config.sync.japanese_stt_min_activity}
'''
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o600)
    return destination


def write_default_config(destination: Path | None = None) -> Path:
    destination = (destination or DEFAULT_CONFIG_PATH).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination

    bundled = Path(__file__).resolve().parent / "config.example.toml"
    if bundled.exists():
        shutil.copy2(bundled, destination)
    else:
        destination.write_text('[jimaku]\napi_key = ""\n', encoding="utf-8")
    destination.chmod(0o600)
    return destination

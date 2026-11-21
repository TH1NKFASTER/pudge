from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import weakref
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterable, Iterator

from .database_storage import vacuum_into
from .language import IMAGE_SUBTITLE_EXTENSIONS
from .manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from .filename import release_name_display_title
from .release_parser import double_episode_span
from .episode_state import transition_episode_state, stronger_episode_state
from .personal_schedule import (
    PersonalReleaseItem,
    PersonalReleaseRule,
    PersonalReleaseSchedule,
    PersonalScheduleRevisionConflict,
    materialize_schedule_items,
    normalize_schedule_rule,
    reanchored_start_local_datetime,
)
from .i18n import err
from .private_files import secure_database_files


_LOGGER = logging.getLogger(__name__)
# Bumped whenever this process saves or forgets a release numbering rule: caches of episode
# offsets derived from the rules (the window's _episode_numbering_offset) compare it.
_NUMBERING_RULES_VERSION = [0]


def numbering_rules_version() -> int:
    return _NUMBERING_RULES_VERSION[0]


def _numbering_rules_changed() -> None:
    _NUMBERING_RULES_VERSION[0] += 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    media_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    titles_json TEXT NOT NULL DEFAULT '[]',
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    cover_url TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    episodes INTEGER,
    format TEXT,
    season_year INTEGER,
    start_date TEXT,
    studio TEXT NOT NULL DEFAULT '',
    media_status TEXT,
    end_date TEXT,
    mean_score INTEGER,
    user_score REAL,
    duration INTEGER,
    next_airing_episode INTEGER,
    next_airing_at INTEGER,
    relations_json TEXT NOT NULL DEFAULT '[]',
    list_added_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER,
    title TEXT NOT NULL,
    episode INTEGER,
    media_episode INTEGER,
    release_episode INTEGER,
    video_path TEXT NOT NULL UNIQUE,
    subtitle_path TEXT,
    embedded_subtitle_id INTEGER,
    subtitle_origin TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'local',
    torrent_hash TEXT NOT NULL DEFAULT '',
    downloaded_at REAL,
    watched_at REAL,
    delete_after REAL,
    playback_position REAL,
    playback_duration REAL,
    playback_updated_at REAL,
    playback_active_seconds REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_media_episode ON episodes(media_id, episode);
CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes(state);
CREATE INDEX IF NOT EXISTS idx_episodes_delete_after ON episodes(delete_after);

CREATE TABLE IF NOT EXISTS downloads (
    torrent_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    save_path TEXT NOT NULL DEFAULT '',
    content_path TEXT NOT NULL DEFAULT '',
    media_id INTEGER,
    episode INTEGER,
    media_episode INTEGER,
    release_episode INTEGER,
    is_batch INTEGER NOT NULL DEFAULT 0,
    added_on INTEGER NOT NULL DEFAULT 0,
    completed_on INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS subtitle_jobs (
    video_path TEXT PRIMARY KEY,
    media_id INTEGER,
    episode INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    next_check REAL NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'queued',
    lease_until REAL NOT NULL DEFAULT 0,
    heartbeat_at REAL NOT NULL DEFAULT 0,
    progress_json TEXT NOT NULL DEFAULT '{}',
    action_code TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS episode_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episode_state_history_video
ON episode_state_history(video_path,created_at DESC);

CREATE TABLE IF NOT EXISTS app_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    current REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    attempt_of TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_app_jobs_state_updated
ON app_jobs(state,updated_at DESC);

-- Split-stage numbering learned from a manual release choice:
-- "this uploader numbers this title continuously, with this offset".
CREATE TABLE IF NOT EXISTS release_numbering_rules (
    media_id INTEGER NOT NULL,
    release_group TEXT NOT NULL,
    season_key TEXT NOT NULL,
    episode_offset INTEGER NOT NULL,
    example_title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY(media_id, release_group, season_key)
);

CREATE TABLE IF NOT EXISTS release_history (
    info_hash TEXT PRIMARY KEY,
    media_id INTEGER,
    episode INTEGER,
    media_episode INTEGER,
    release_episode INTEGER,
    title TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    selected_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

-- prune_history() looks up the latest release of each row's episode: without this index that
-- correlated subquery read the whole table once per row (10k rows: ~13 s under the write lock).
CREATE INDEX IF NOT EXISTS idx_release_history_media ON release_history(media_id);


CREATE TABLE IF NOT EXISTS upgrade_jobs (
    new_info_hash TEXT PRIMARY KEY,
    old_torrent_hash TEXT NOT NULL,
    media_id INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    old_score REAL NOT NULL,
    new_score REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'downloading',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_upgrade_jobs_state ON upgrade_jobs(state, updated_at);


CREATE TABLE IF NOT EXISTS subtitle_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path TEXT NOT NULL,
    media_id INTEGER,
    episode INTEGER,
    source TEXT NOT NULL DEFAULT '',
    candidate_name TEXT NOT NULL DEFAULT '',
    candidate_path TEXT NOT NULL DEFAULT '',
    score REAL,
    status TEXT NOT NULL DEFAULT 'selected',
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    video_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_subtitle_history_video_created
ON subtitle_history(video_path, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_subtitle_history_media_episode
ON subtitle_history(media_id, episode, created_at DESC);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'manual',
    media_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    media_id INTEGER,
    episode INTEGER,
    video_path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL,
    UNIQUE(playlist_id, video_path)
);

CREATE INDEX IF NOT EXISTS idx_playlist_items_queue
ON playlist_items(playlist_id, state, position);

CREATE TABLE IF NOT EXISTS personal_release_schedules (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL DEFAULT 'default',
    anime_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    start_local_datetime TEXT NOT NULL,
    timezone_iana TEXT NOT NULL,
    cadence TEXT NOT NULL DEFAULT 'weekly',
    rule_version INTEGER NOT NULL DEFAULT 1,
    interval_unit TEXT NOT NULL DEFAULT 'weeks',
    interval_every INTEGER NOT NULL DEFAULT 1,
    interval_clock TEXT NOT NULL DEFAULT 'wall',
    cycle_id INTEGER NOT NULL DEFAULT 1,
    first_episode_id INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    rewatch INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(anime_id) REFERENCES anime(media_id) ON DELETE CASCADE,
    UNIQUE(profile_id, anime_id)
);

CREATE TABLE IF NOT EXISTS personal_release_items (
    schedule_id INTEGER NOT NULL,
    logical_episode_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    unlock_at_utc REAL NOT NULL,
    nominal_local_datetime TEXT NOT NULL,
    schedule_revision INTEGER NOT NULL,
    dst_adjusted INTEGER NOT NULL DEFAULT 0,
    unlocked_once INTEGER NOT NULL DEFAULT 0,
    watched_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(schedule_id, logical_episode_id),
    FOREIGN KEY(schedule_id) REFERENCES personal_release_schedules(schedule_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_personal_release_items_unlock
ON personal_release_items(schedule_id, unlocked_once, unlock_at_utc, ordinal);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);


-- Cheap cross-process UI invalidation marker. These triggers bump one integer
-- only when data that can change rendered UI state changes. Playback heartbeat
-- fields are intentionally excluded so watching a video does not rebuild the UI
-- every second. The UPDATE triggers of anime, episodes, downloads and
-- subtitle_jobs are installed by Database._install_ui_state_update_triggers.
CREATE TRIGGER IF NOT EXISTS anime_ui_state_insert AFTER INSERT ON anime BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS anime_ui_state_delete AFTER DELETE ON anime BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS episodes_ui_state_insert AFTER INSERT ON episodes BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS episodes_ui_state_delete AFTER DELETE ON episodes BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS downloads_ui_state_insert AFTER INSERT ON downloads BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS downloads_ui_state_delete AFTER DELETE ON downloads BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS subtitle_jobs_ui_state_insert AFTER INSERT ON subtitle_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS subtitle_jobs_ui_state_delete AFTER DELETE ON subtitle_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS upgrade_jobs_ui_state_insert AFTER INSERT ON upgrade_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS upgrade_jobs_ui_state_update AFTER UPDATE ON upgrade_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS upgrade_jobs_ui_state_delete AFTER DELETE ON upgrade_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS subtitle_history_ui_state_insert AFTER INSERT ON subtitle_history BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS subtitle_history_ui_state_delete AFTER DELETE ON subtitle_history BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS playlists_ui_state_insert AFTER INSERT ON playlists BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS playlists_ui_state_update AFTER UPDATE ON playlists BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS playlists_ui_state_delete AFTER DELETE ON playlists BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS playlist_items_ui_state_insert AFTER INSERT ON playlist_items BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS playlist_items_ui_state_update AFTER UPDATE ON playlist_items BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS playlist_items_ui_state_delete AFTER DELETE ON playlist_items BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TABLE IF NOT EXISTS relation_graphs (
    graph_id TEXT PRIMARY KEY,
    graph_json TEXT NOT NULL,
    refreshed_at REAL NOT NULL,
    next_refresh_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relation_graphs_next_refresh
ON relation_graphs(next_refresh_at);

CREATE TABLE IF NOT EXISTS relation_graph_members (
    media_id INTEGER PRIMARY KEY,
    graph_id TEXT NOT NULL,
    FOREIGN KEY(graph_id) REFERENCES relation_graphs(graph_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relation_graph_members_graph
ON relation_graph_members(graph_id);

CREATE TABLE IF NOT EXISTS manga_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    read_pages INTEGER NOT NULL DEFAULT 0,
    reading_direction TEXT NOT NULL DEFAULT 'rtl',
    anilist_id INTEGER,
    cover_url TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    user_score REAL,
    mean_score REAL,
    source_fingerprint TEXT NOT NULL DEFAULT '',
    position_updated_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS manga_ocr_cache (
    book_id INTEGER NOT NULL,
    page_index INTEGER NOT NULL,
    region_key TEXT NOT NULL DEFAULT 'full',
    text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(book_id,page_index,region_key),
    FOREIGN KEY(book_id) REFERENCES manga_books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audiobooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    position REAL NOT NULL DEFAULT 0,
    finished INTEGER NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 1,
    last_played_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audiobook_chapters (
    book_id INTEGER NOT NULL,
    chapter_index INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    start REAL NOT NULL DEFAULT 0,
    end REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(book_id,chapter_index),
    FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audiobook_files (
    book_id INTEGER NOT NULL,
    file_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    start REAL NOT NULL DEFAULT 0,
    end REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(book_id,file_index),
    FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audiobook_bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    position REAL NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audiobook_bookmarks_book
ON audiobook_bookmarks(book_id,sort_order,position);

CREATE TABLE IF NOT EXISTS reading_audio_links (
    ln_book_id INTEGER PRIMARY KEY,
    audiobook_id INTEGER NOT NULL,
    alignment_mode TEXT NOT NULL DEFAULT 'chapter',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(audiobook_id) REFERENCES audiobooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS character_name_overrides (
    media_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    preferred TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(media_id,source)
);

CREATE TABLE IF NOT EXISTS media_identities (
    kind TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    anilist_id INTEGER NOT NULL,
    anilist_type TEXT NOT NULL DEFAULT 'MANGA',
    title TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY(kind,local_id)
);


CREATE TABLE IF NOT EXISTS consumption_media (
    media_uuid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title_snapshot TEXT NOT NULL DEFAULT '',
    current_library_kind TEXT NOT NULL DEFAULT '',
    current_library_id TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    deleted_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumption_media_library
ON consumption_media(current_library_kind,current_library_id);

CREATE TABLE IF NOT EXISTS consumption_media_aliases (
    kind TEXT NOT NULL,
    alias_type TEXT NOT NULL,
    alias_value TEXT NOT NULL,
    media_uuid TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(kind,alias_type,alias_value),
    FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_consumption_alias_media
ON consumption_media_aliases(media_uuid);

CREATE TABLE IF NOT EXISTS vn_titles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    executable_hint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(normalized_title,executable_hint)
);

CREATE TABLE IF NOT EXISTS consumption_devices (
    device_id TEXT PRIMARY KEY,
    device_name TEXT NOT NULL DEFAULT '',
    next_seq INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS consumption_sessions (
    session_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL DEFAULT 'default',
    device_id TEXT NOT NULL,
    activity_group_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    primary_media_uuid TEXT,
    started_at_utc REAL NOT NULL,
    ended_at_utc REAL,
    origin TEXT NOT NULL DEFAULT 'automatic',
    status TEXT NOT NULL DEFAULT 'active',
    measurement_method TEXT NOT NULL DEFAULT 'observation',
    policy_version TEXT NOT NULL DEFAULT 'r7-v1',
    schema_version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(primary_media_uuid) REFERENCES consumption_media(media_uuid)
);
CREATE INDEX IF NOT EXISTS idx_consumption_sessions_time
ON consumption_sessions(started_at_utc,ended_at_utc);
CREATE INDEX IF NOT EXISTS idx_consumption_sessions_media
ON consumption_sessions(primary_media_uuid,status,started_at_utc);
CREATE INDEX IF NOT EXISTS idx_consumption_sessions_group
ON consumption_sessions(activity_group_id,kind,started_at_utc);

CREATE TABLE IF NOT EXISTS consumption_events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    interval_start_utc REAL NOT NULL,
    interval_end_utc REAL NOT NULL,
    elapsed_monotonic_ms REAL NOT NULL,
    received_at_utc REAL NOT NULL,
    timezone_iana TEXT NOT NULL DEFAULT '',
    utc_offset INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(device_id,device_seq),
    FOREIGN KEY(session_id) REFERENCES consumption_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_consumption_events_session
ON consumption_events(session_id,interval_start_utc);
CREATE INDEX IF NOT EXISTS idx_consumption_events_time
ON consumption_events(interval_start_utc,interval_end_utc);

CREATE TABLE IF NOT EXISTS consumption_event_media (
    event_id TEXT NOT NULL,
    media_uuid TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'primary',
    locator_start_json TEXT NOT NULL DEFAULT '{}',
    locator_end_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(event_id,media_uuid,role),
    FOREIGN KEY(event_id) REFERENCES consumption_events(event_id) ON DELETE CASCADE,
    FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid)
);
CREATE INDEX IF NOT EXISTS idx_consumption_event_media_media
ON consumption_event_media(media_uuid,event_id);

CREATE TABLE IF NOT EXISTS vn_capture_sessions (
    session_id TEXT PRIMARY KEY,
    vn_title_id TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    FOREIGN KEY(session_id) REFERENCES consumption_sessions(session_id),
    FOREIGN KEY(vn_title_id) REFERENCES vn_titles(id)
);
CREATE INDEX IF NOT EXISTS idx_vn_capture_title
ON vn_capture_sessions(vn_title_id,started_at);

CREATE TABLE IF NOT EXISTS consumption_corrections (
    correction_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    replacement_start_utc REAL,
    replacement_end_utc REAL,
    replacement_duration_seconds REAL,
    reason TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    created_at_utc REAL NOT NULL,
    UNIQUE(target_type,target_id,revision)
);
CREATE INDEX IF NOT EXISTS idx_consumption_corrections_target
ON consumption_corrections(target_type,target_id,revision DESC);

CREATE TABLE IF NOT EXISTS consumption_manual_entries (
    manual_id TEXT PRIMARY KEY,
    media_uuid TEXT,
    kind TEXT NOT NULL,
    title_snapshot TEXT NOT NULL DEFAULT '',
    started_at_utc REAL,
    duration_seconds REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at_utc REAL NOT NULL,
    updated_at_utc REAL NOT NULL,
    FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid)
);
CREATE INDEX IF NOT EXISTS idx_consumption_manual_time
ON consumption_manual_entries(started_at_utc,kind);

CREATE TABLE IF NOT EXISTS consumption_daily_rollups (
    local_day TEXT PRIMARY KEY,
    total_seconds REAL NOT NULL DEFAULT 0,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    event_count INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL DEFAULT 'r8-v1',
    generated_at_utc REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS consumption_history_state (
    profile_id TEXT PRIMARY KEY,
    reset_epoch INTEGER NOT NULL DEFAULT 0,
    reset_at_utc REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consumption_sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_utc REAL NOT NULL,
    UNIQUE(record_type,record_key)
);
CREATE INDEX IF NOT EXISTS idx_consumption_sync_outbox_created
ON consumption_sync_outbox(created_at_utc,id);

CREATE TABLE IF NOT EXISTS consumption_sync_receipts (
    device_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    received_at_utc REAL NOT NULL,
    PRIMARY KEY(device_id,record_id)
);

-- Statistics words (consumption_words.py): parsed words of each piece of text consumed
-- (episode subtitles, a manga page, a VN line, an LN chapter), filled in the background.
CREATE TABLE IF NOT EXISTS consumption_word_units (
    unit_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    words_json TEXT NOT NULL DEFAULT '',
    source_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumption_word_units_status
ON consumption_word_units(status,updated_at);

-- Lines a visual-novel capture session read (the capture keeps only the last ones in memory).
CREATE TABLE IF NOT EXISTS vn_capture_lines (
    session_id TEXT NOT NULL,
    line_id INTEGER NOT NULL,
    captured_at_utc REAL NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY(session_id,line_id)
);
CREATE INDEX IF NOT EXISTS idx_vn_capture_lines_time
ON vn_capture_lines(captured_at_utc);
"""

LATEST_SCHEMA_VERSION = 13

# Indexes added to tables of every schema version (cache_registry only exists after the
# migrations): created after _migrate on every initialization, so existing libraries get
# them too. The FK columns pointing at anime are searched on every anime delete/cascade.
_ADDITIVE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cache_registry_path ON cache_registry(path);
CREATE INDEX IF NOT EXISTS idx_downloads_media ON downloads(media_id);
CREATE INDEX IF NOT EXISTS idx_upgrade_jobs_media ON upgrade_jobs(media_id);
CREATE INDEX IF NOT EXISTS idx_playlists_media ON playlists(media_id);
CREATE INDEX IF NOT EXISTS idx_personal_release_schedules_anime ON personal_release_schedules(anime_id);
"""


# Rows of the ``state`` table whose key starts with a prefix: ``key>=? AND key<?`` searches the
# primary-key index. ``key LIKE 'p%'`` and ``substr(key,1,n)='p'`` read the whole table (tens of
# thousands of rows with manga OCR and per-episode markers) and LIKE also treats "_" as a wildcard.
STATE_PREFIX_SQL = "key>=? AND key<?"


# prune_history entries that delete ``state`` rows rather than a table of their own.
_STATE_PRUNE_ROWS = frozenset({"playback_markers", "double_episode_seen", "aria2_stall_markers"})


def state_prefix_bounds(prefix: str) -> tuple[str, str]:
    """``(low, high)`` for STATE_PREFIX_SQL: every key that starts with ``prefix``, nothing else."""
    prefix = str(prefix)
    if not prefix:
        raise ValueError("empty state key prefix")
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQLite script without ``executescript``'s implicit COMMIT.

    ``sqlite3.Connection.executescript`` commits any pending transaction before
    executing its input.  That made the old multi-step migrations only partly
    recoverable.  ``sqlite3.complete_statement`` understands trigger bodies, so
    feeding complete statements one-by-one keeps schema creation and migrations
    inside the transaction opened by :class:`Database`.
    """

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if sql:
            conn.execute(sql)
    if statement.strip():
        raise sqlite3.OperationalError("incomplete SQL migration statement")


# Needs-action jobs that wait for the person's answer (Keep or Delete an extra file of a
# download, "preparation keeps failing"): Refresh and launch never turn them back into
# pending. Settings-related codes are retried, settings may have changed. A new file in
# the subtitle inbox retries a failing job too, only the extra files stay.
EXTRA_FILE_ACTION_CODES = ("review_extra_file", "extra_file_kept")


def preferred_copy(rows: Iterable[Any], folder_counts: dict[str, int]) -> Any:
    """The copy to play among several library files of the same episode.

    A file that is on disk wins, then one next to more of the title's episodes (the batch the
    neighbouring episodes come from over an older single download), then the oldest row (the
    order the library query returned before). The Mac (``ready_episode``) and the phone list
    (``iphone_media``) use the same rule.
    """

    def key(row: Any) -> tuple[int, int, int]:
        path = Path(str(row["video_path"]))
        try:
            present = path.is_file()
        except OSError:
            present = False
        return (int(present), folder_counts.get(str(path.parent), 0), -int(row["id"] or 0))

    return max(rows, key=key)


def season_zero_set_aside_key(video: Path | str) -> str:
    """State key of a season-0 file the library scan set aside (see extra_files)."""
    return "season_zero_set_aside:" + hashlib.sha1(str(Path(video).resolve()).encode("utf-8")).hexdigest()


REFRESH_KEEPS_ACTION_CODES = (*EXTRA_FILE_ACTION_CODES, "subtitle_retry_exhausted")

# Continue Watching lists an episode from this position on, until this close to its end.
RESUME_MIN_POSITION = 30.0
RESUME_MIN_REMAINING = 60.0


def _resumable(state: object, position: float | None, duration: float | None) -> bool:
    """True when the episode belongs in Continue Watching (as resumable_episodes selects it)."""
    if str(state or "") == "watched" or position is None or float(position) < RESUME_MIN_POSITION:
        return False
    return duration is None or float(duration) - float(position) >= RESUME_MIN_REMAINING


_UI_STATE_BUMP_SQL = (
    "UPDATE state SET value=CAST(value AS INTEGER)+1,"
    "updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';"
)
_EPISODE_UI_COLUMNS = (
    "media_id",
    "title",
    "episode",
    "video_path",
    "subtitle_path",
    "embedded_subtitle_id",
    "subtitle_origin",
    "state",
    "torrent_hash",
    "downloaded_at",
    "watched_at",
    "delete_after",
)
# (trigger, table, columns the rendered UI never reads, extra condition).
# A no-op upsert, a timestamp touch or a worker heartbeat no longer forces a
# full Home rebuild; every other column (including ones added by later
# migrations) still invalidates it.
_UI_STATE_UPDATE_TRIGGERS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("anime_ui_state_update", "anime", ("updated_at", "list_added_at"), ""),
    # Progress and the client stats in raw_json change on every client poll; live progress
    # reaches the page through the traffic poll (WEBFLOW-7). Finishing still counts, and
    # upsert_download bumps for any other raw_json change.
    (
        "downloads_ui_state_update",
        "downloads",
        ("updated_at", "progress", "raw_json"),
        "((OLD.progress >= 1) IS NOT (NEW.progress >= 1))",
    ),
    (
        "subtitle_jobs_ui_state_update",
        "subtitle_jobs",
        ("updated_at", "heartbeat_at", "lease_until", "progress_json", "stage", "next_check"),
        # A processing job's next_check is its lease (the UI shows "preparing");
        # a pending job's next_check is the "Next check" time the UI shows.
        "(OLD.next_check IS NOT NEW.next_check AND "
        "(OLD.state IS NOT 'processing' OR NEW.state IS NOT 'processing'))",
    ),
)


# Client statistics stored in downloads.raw_json that change on every poll (qBittorrent's
# torrent info, aria2's normalized status): a change of only these is no UI change.
_VOLATILE_DOWNLOAD_RAW_KEYS = frozenset(
    {
        # qBittorrent
        "amount_left",
        "availability",
        "completed",
        "dlspeed",
        "downloaded",
        "downloaded_session",
        "eta",
        "last_activity",
        "num_complete",
        "num_incomplete",
        "num_leechs",
        "num_seeds",
        "popularity",
        "progress",
        "ratio",
        "reannounce",
        "seeding_time",
        "seen_complete",
        "time_active",
        "uploaded",
        "uploaded_session",
        "upspeed",
        # aria2 (providers/aria2.py)
        "download_speed",
        "num_connections",
        "num_seeders",
        "upload_speed",
        "verification_progress",
        "verified",
    }
)


def _download_ui_raw(raw: object) -> dict[str, object]:
    """raw_json without the per-poll client statistics."""
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key not in _VOLATILE_DOWNLOAD_RAW_KEYS}


def _ui_state_update_trigger_sql(conn: sqlite3.Connection) -> dict[str, str]:
    specs: dict[str, str] = {}
    for name, table, skipped, extra in _UI_STATE_UPDATE_TRIGGERS:
        columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        changed = [f"OLD.{column} IS NOT NEW.{column}" for column in columns if column not in skipped]
        if extra:
            changed.append(extra)
        specs[name] = (
            f"CREATE TRIGGER {name} AFTER UPDATE ON {table}\nWHEN "
            + " OR ".join(changed)
            + f" BEGIN\n    {_UI_STATE_BUMP_SQL}\nEND"
        )
    episode_changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in _EPISODE_UI_COLUMNS)
    specs["episodes_ui_state_update"] = (
        "CREATE TRIGGER episodes_ui_state_update\nAFTER UPDATE OF "
        + ",".join(_EPISODE_UI_COLUMNS)
        + f" ON episodes\nWHEN {episode_changed} BEGIN\n    {_UI_STATE_BUMP_SQL}\nEND"
    )
    return specs


# Which schema code last initialized the file (DB-6): helpers open the library with
# initialize=False and skip the schema script only while this matches. The schema
# version alone did not say it: tables and _ensure_column checks are added without
# a version bump, so the app version is part of the digest.
SCHEMA_DIGEST_KEY = "schema_digest"
_SCHEMA_DIGEST: str | None = None


def _schema_digest() -> str:
    global _SCHEMA_DIGEST
    if _SCHEMA_DIGEST is None:
        from . import __version__

        source = repr(
            (
                SCHEMA,
                _ADDITIVE_INDEXES,
                _UI_STATE_BUMP_SQL,
                _EPISODE_UI_COLUMNS,
                _UI_STATE_UPDATE_TRIGGERS,
                sorted(Database._CURRENT_INDEXES.items()),
            )
        )
        _SCHEMA_DIGEST = (
            f"{LATEST_SCHEMA_VERSION}:{__version__}:{hashlib.sha256(source.encode()).hexdigest()[:16]}"
        )
    return _SCHEMA_DIGEST


class _TrackedConnection(sqlite3.Connection):
    """A connection that knows which of its cursors are still alive.

    A cursor kept past its call can hold an unfinished SELECT, and with it the old
    read snapshot: a parked connection would then read stale rows on its next call.
    Such a connection is closed as before instead of parked.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    def _track(self, cursor: sqlite3.Cursor) -> sqlite3.Cursor:
        self.cursors.add(cursor)
        return cursor

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._track(super().cursor(*args, **kwargs))

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._track(super().execute(*args, **kwargs))

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._track(super().executemany(*args, **kwargs))

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._track(super().executescript(*args, **kwargs))


class _IdleConnection:
    """A thread's parked SQLite connection (Database(thread_connections=True))."""

    __slots__ = ("conn", "identity", "pid", "owner")

    def __init__(self, conn: sqlite3.Connection, identity: tuple[int, int] | None, owner: object) -> None:
        self.conn: sqlite3.Connection | None = conn
        self.identity = identity
        self.pid = os.getpid()
        self.owner = owner


# Parked connections of the whole process, oldest first: at most _PARKED_LIMIT stay open (two
# file handles each; a macOS app starts with 256). A thread that finished or a Database no
# longer used leaves its connection here until it is the oldest. Taken out under the lock,
# so a connection is closed by eviction or retire() only while nobody uses it.
_PARKED_LIMIT = 12
_PARKED_LOCK = threading.Lock()
_PARKED: dict[int, _IdleConnection] = {}


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing an idle handle
            pass


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def reused_connection(db: object) -> AbstractContextManager[object]:
    """``db.reuse_connection()`` for a real Database, a no-op for test doubles."""
    return db.reuse_connection() if isinstance(db, Database) else nullcontext()


def _json_object(value: object) -> dict[str, object]:
    """Stored JSON object; one damaged row must not break a whole listing."""
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Episode state written when a subtitle selection is dropped for a new preparation:
# Watched and Dropped episodes stay what they are (a rebuild must not show them again
# as new on Home).
_WAITING_UNLESS_WATCHED_SQL = (
    "CASE WHEN state IN ('watched','dropped') THEN state ELSE 'waiting_subtitles' END"
)
# ... and when a job is deferred, postponed or waits for the user: a Ready episode that
# keeps its selection (an upgrade check or a rebuild keeping its subtitles) stays Ready.
_WAITING_UNLESS_SETTLED_SQL = (
    "CASE WHEN state IN ('watched','dropped') THEN state "
    "WHEN state='ready' AND (subtitle_path IS NOT NULL OR embedded_subtitle_id IS NOT NULL) THEN state "
    "ELSE 'waiting_subtitles' END"
)
_SELECTED_HISTORY_STATUSES_SQL = "('selected','upgraded','manual','legacy')"


class Database:
    def __init__(
        self,
        path: Path,
        *,
        initialize: bool = True,
        integrity_check: bool = True,
        thread_connections: bool = False,
    ) -> None:
        """Open the library.

        ``initialize=False`` (helpers, the agent, CLI commands): when this app's
        schema code already initialized the file, only open it — no write lock,
        no schema script, no full-file ``quick_check``. An older or unknown schema
        is still initialized/migrated. ``integrity_check`` runs ``quick_check``
        after a full initialization (the window decides when it is due, DB-6);
        a migration always checks.

        ``thread_connections`` (the window's own library): a finished call parks
        its connection for the next call on the same thread instead of closing
        it, saving the open and schema parse (milliseconds per call). retire()
        closes the parked connections; a replaced file is never reused. Off
        for helper processes, which must not keep the file open between calls.
        """
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection_scope_local = threading.local()
        self._connection_condition = threading.Condition()
        self._active_connections = 0
        self._retired = False
        self._thread_connections = bool(thread_connections)
        self._park_owner = object()  # this Database's entries in _PARKED
        self.integrity_checked = False
        existing_database = self.path.is_file() and self.path.stat().st_size > 0
        if existing_database:
            # Older versions created it 0644 in a 0755 folder: other accounts could read it.
            secure_database_files(self.path)
        if not initialize and existing_database:
            with self.connect() as conn:
                row = conn.execute("PRAGMA user_version").fetchone()
                if row and int(row[0]) == LATEST_SCHEMA_VERSION:
                    try:
                        digest = conn.execute(
                            "SELECT value FROM state WHERE key=?", (SCHEMA_DIGEST_KEY,)
                        ).fetchone()
                    except sqlite3.OperationalError:
                        digest = None  # no state table yet: initialize below
                    if digest and str(digest[0]) == _schema_digest():
                        return  # The owning app initializes/checks the current schema.
            integrity_check = False  # helpers never pay for the full-file check
        with self.connect() as conn:
            # WAL mode is persistent for the database file. Reissuing this PRAGMA
            # on every short-lived connection forces SQLite to touch/parse schema
            # state repeatedly and showed up directly in the R10 energy profile.
            conn.execute("PRAGMA journal_mode=WAL")
            version_row = conn.execute("PRAGMA user_version").fetchone()
            previous_version = int(version_row[0] if version_row else 0)
            outdated = existing_database and previous_version < LATEST_SCHEMA_VERSION
            # After an update the window, the agent and mpv helpers can open the old
            # schema together: one process at a time backs up and migrates, and the
            # others find the schema current once they get the lock.
            with self._migration_lock() if outdated else nullcontext():
                if outdated:
                    version_row = conn.execute("PRAGMA user_version").fetchone()
                    previous_version = int(version_row[0] if version_row else 0)
                    if previous_version < LATEST_SCHEMA_VERSION:
                        self._backup_before_migration(conn, previous_version)
                migrating = previous_version < LATEST_SCHEMA_VERSION
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _execute_sql_script(conn, SCHEMA)
                    self._migrate(conn)
                    self._create_additive_indexes(conn)
                    self._install_ui_state_update_triggers(conn)
                    self._recreate_drifted_indexes(conn)
                    if migrating:
                        self._quick_check(conn)
                        self.integrity_checked = True
                    conn.execute(
                        "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (SCHEMA_DIGEST_KEY, _schema_digest(), time.time()),
                    )
                    conn.commit()  # before the lock is released
                except BaseException:
                    conn.rollback()
                    raise
        if not migrating and integrity_check:
            # The full scan runs outside the write transaction, so the agent and
            # mpv heartbeat are not blocked for its duration on every launch.
            self.check_integrity()
        if not existing_database:
            secure_database_files(self.path)

    def check_integrity(self) -> None:
        """``PRAGMA quick_check`` of the whole file; raises sqlite3.DatabaseError on damage."""
        with self.connect() as conn:
            self._quick_check(conn)
        self.integrity_checked = True

    @staticmethod
    def _install_ui_state_update_triggers(conn: sqlite3.Connection) -> None:
        """(Re)create UI invalidation triggers only when their SQL changed."""
        existing = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        }
        for name, sql in _ui_state_update_trigger_sql(conn).items():
            if existing.get(name) == sql:
                continue
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(sql)

    @staticmethod
    def _quick_check(conn: sqlite3.Connection) -> None:
        integrity = conn.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise sqlite3.DatabaseError(
                f"database integrity check failed: {integrity[0] if integrity else 'no result'}"
            )

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        """An exclusive flock on ``<db>.migrate.lock`` for one schema migration."""
        import fcntl

        lock_path = self.path.with_name(f"{self.path.name}.migrate.lock")
        try:
            handle = open(lock_path, "a+b")
        except OSError:
            yield  # a read-only folder: migrate as before
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    # Indexes whose definition changed after databases had been created with the old
    # one: CREATE INDEX IF NOT EXISTS keeps the old definition for good.
    _CURRENT_INDEXES = {
        "idx_audiobook_bookmarks_book": (
            "CREATE INDEX idx_audiobook_bookmarks_book ON audiobook_bookmarks(book_id,sort_order,position)"
        ),
    }

    @staticmethod
    def _create_additive_indexes(conn: sqlite3.Connection) -> None:
        """_ADDITIVE_INDEXES on the tables this file has (an old or partly built
        library may lack cache_registry)."""
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for statement in _ADDITIVE_INDEXES.strip().splitlines():
            table = statement.split(" ON ", 1)[1].split("(", 1)[0].strip()
            if table in tables:
                conn.execute(statement)

    @classmethod
    def _recreate_drifted_indexes(cls, conn: sqlite3.Connection) -> None:
        def normalized(sql: str) -> str:
            return "".join(sql.split()).casefold().replace("ifnotexists", "")

        stored = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN ({})".format(
                    ",".join("?" * len(cls._CURRENT_INDEXES))
                ),
                tuple(cls._CURRENT_INDEXES),
            ).fetchall()
        }
        for name, sql in cls._CURRENT_INDEXES.items():
            if name in stored and normalized(stored[name]) != normalized(sql):
                conn.execute(f"DROP INDEX {name}")
                conn.execute(sql)

    def _backup_before_migration(self, conn: sqlite3.Connection, version: int) -> Path:
        """Create a consistent last-known-good copy before changing the schema."""

        backup_path = self.path.with_name(f"{self.path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
        # VACUUM INTO: the backup API copied the free pages too (~1 GB copies). The
        # temp name is this process's and thread's own: a shared one made processes
        # migrating together delete each other's copy (a 0-byte backup).
        temporary = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            vacuum_into(conn, temporary)
            os.chmod(temporary, 0o600)  # a full copy of the library: this user's only
            temporary.replace(backup_path)
        finally:
            temporary.unlink(missing_ok=True)
        return backup_path

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0] if version_row else 0)
        if version < 1:
            self._migrate_v1(conn)
            conn.execute("PRAGMA user_version=1")
        if version < 2:
            self._migrate_v2(conn)
            conn.execute("PRAGMA user_version=2")
        if version < 3:
            self._migrate_v3(conn)
            conn.execute("PRAGMA user_version=3")
        if version < 4:
            self._migrate_v4(conn)
            conn.execute("PRAGMA user_version=4")
        if version < 5:
            self._migrate_v5(conn)
            conn.execute("PRAGMA user_version=5")
        if version < 6:
            self._migrate_v6(conn)
            conn.execute("PRAGMA user_version=6")
        if version < 7:
            self._migrate_v7(conn)
            conn.execute("PRAGMA user_version=7")
        if version < 8:
            self._migrate_v8(conn)
            conn.execute("PRAGMA user_version=8")
        if version < 9:
            self._migrate_v9(conn)
            conn.execute("PRAGMA user_version=9")
        if version < 10:
            self._migrate_v10(conn)
            conn.execute("PRAGMA user_version=10")
        if version < 11:
            self._migrate_v11(conn)
            conn.execute("PRAGMA user_version=11")
        if version < 12:
            self._migrate_v12(conn)
            conn.execute("PRAGMA user_version=12")
        if version < 13:
            self._migrate_v13(conn)
            conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        # Keep additive compatibility checks idempotent for databases created by
        # local 0.7 checkpoints before the numbered v3 migration existed.
        self._ensure_column(conn, "manga_books", "anilist_id", "INTEGER")
        self._ensure_column(conn, "manga_books", "cover_url", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "manga_books", "site_url", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "manga_books", "user_score", "REAL")
        self._ensure_column(conn, "manga_books", "mean_score", "REAL")
        manga_progress_columns = {row[1] for row in conn.execute("PRAGMA table_info(manga_books)").fetchall()}
        manga_read_pages_missing = "read_pages" not in manga_progress_columns
        self._ensure_column(conn, "manga_books", "read_pages", "INTEGER NOT NULL DEFAULT 0")
        if manga_read_pages_missing:
            # Preserve old progress reasonably, but position=0 may mean merely opened.
            conn.execute(
                "UPDATE manga_books SET read_pages="
                "CASE WHEN position>0 THEN MIN(page_count,position+1) ELSE 0 END"
            )
        self._ensure_column(conn, "subtitle_jobs", "generation", "INTEGER NOT NULL DEFAULT 0")
        # Two episodes in one file (audit 17А): the last episode and where it starts.
        self._ensure_column(conn, "episodes", "media_episode_end", "INTEGER")
        self._ensure_column(conn, "episodes", "part_boundary", "REAL")
        self._ensure_column(conn, "downloads", "media_episode_end", "INTEGER")
        self._ensure_column(conn, "subtitle_jobs", "owner_token", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            conn,
            "subtitle_history",
            "video_fingerprint",
            "TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(conn, "manga_books", "source_fingerprint", "TEXT NOT NULL DEFAULT ''")
        # When the reading place last moved: updated_at also changes on AniList refreshes and
        # scores, so a newer page from the phone was dropped as older. Existing books: 0 (the
        # phone's page counts until the place moves again).
        self._ensure_column(conn, "manga_books", "position_updated_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column(conn, "audiobooks", "speed", "REAL NOT NULL DEFAULT 1")
        self._ensure_column(conn, "audiobooks", "last_played_at", "REAL NOT NULL DEFAULT 0")
        # When the title was added to the AniList list (MediaList.createdAt): Planning
        # discovery uses it for movies it saw only after their premiere. 0 = unknown.
        self._ensure_column(conn, "anime", "list_added_at", "REAL NOT NULL DEFAULT 0")
        # Hot predicates of the job/torrent queries (idempotent, instant once present).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_torrent_hash ON episodes(torrent_hash)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subtitle_jobs_state_next ON subtitle_jobs(state,next_check)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subtitle_jobs_media_episode ON subtitle_jobs(media_id,episode)"
        )
        bookmark_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(audiobook_bookmarks)").fetchall()
        }
        bookmark_sort_missing = "sort_order" not in bookmark_columns
        self._ensure_column(
            conn,
            "audiobook_bookmarks",
            "sort_order",
            "INTEGER NOT NULL DEFAULT 0",
        )
        if bookmark_sort_missing:
            rows = conn.execute(
                "SELECT id,book_id FROM audiobook_bookmarks ORDER BY book_id,position,id"
            ).fetchall()
            next_order: dict[int, int] = {}
            for row in rows:
                book_id = int(row["book_id"])
                order = next_order.get(book_id, 0)
                conn.execute(
                    "UPDATE audiobook_bookmarks SET sort_order=? WHERE id=?",
                    (order, int(row["id"])),
                )
                next_order[book_id] = order + 1
        conn.execute(
            "INSERT OR IGNORE INTO state(key,value,updated_at) VALUES('ui_state_version','0',?)",
            (time.time(),),
        )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(anime)").fetchall()}
        if "season_year" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN season_year INTEGER")
        if "start_date" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN start_date TEXT")
        if "studio" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN studio TEXT NOT NULL DEFAULT ''")
        if "media_status" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN media_status TEXT")
        if "end_date" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN end_date TEXT")
        if "mean_score" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN mean_score INTEGER")
        if "duration" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN duration INTEGER")
        if "user_score" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN user_score REAL")
        if "relations_json" not in columns:
            conn.execute("ALTER TABLE anime ADD COLUMN relations_json TEXT NOT NULL DEFAULT '[]'")
        episode_columns = {row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        if "embedded_subtitle_id" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN embedded_subtitle_id INTEGER")
        if "subtitle_origin" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN subtitle_origin TEXT NOT NULL DEFAULT ''")
        if "playback_position" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN playback_position REAL")
        if "playback_duration" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN playback_duration REAL")
        if "playback_updated_at" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN playback_updated_at REAL")
        if "playback_active_seconds" not in episode_columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN playback_active_seconds REAL NOT NULL DEFAULT 0")
        subtitle_job_columns = {row[1] for row in conn.execute("PRAGMA table_info(subtitle_jobs)").fetchall()}
        if "priority" not in subtitle_job_columns:
            conn.execute("ALTER TABLE subtitle_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        for column, declaration in (
            ("stage", "TEXT NOT NULL DEFAULT 'queued'"),
            ("lease_until", "REAL NOT NULL DEFAULT 0"),
            ("heartbeat_at", "REAL NOT NULL DEFAULT 0"),
            ("progress_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("action_code", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._ensure_column(conn, "subtitle_jobs", column, declaration)
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS manga_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                reading_direction TEXT NOT NULL DEFAULT 'rtl',
                anilist_id INTEGER,
                cover_url TEXT NOT NULL DEFAULT '',
                site_url TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS manga_ocr_cache (
                book_id INTEGER NOT NULL,
                page_index INTEGER NOT NULL,
                region_key TEXT NOT NULL DEFAULT 'full',
                text TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(book_id,page_index,region_key),
                FOREIGN KEY(book_id) REFERENCES manga_books(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audiobooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                duration REAL NOT NULL DEFAULT 0,
                position REAL NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audiobook_chapters (
                book_id INTEGER NOT NULL,
                chapter_index INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                start REAL NOT NULL DEFAULT 0,
                end REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(book_id,chapter_index),
                FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
            );
            """,
        )

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "manga_books", "user_score", "REAL")
        self._ensure_column(
            conn,
            "manga_books",
            "source_fingerprint",
            "TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(conn, "audiobooks", "speed", "REAL NOT NULL DEFAULT 1")
        self._ensure_column(
            conn,
            "audiobooks",
            "last_played_at",
            "REAL NOT NULL DEFAULT 0",
        )
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS audiobook_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                position REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_audiobook_bookmarks_book
            ON audiobook_bookmarks(book_id,sort_order,position);
            CREATE TABLE IF NOT EXISTS reading_audio_links (
                ln_book_id INTEGER PRIMARY KEY,
                audiobook_id INTEGER NOT NULL,
                alignment_mode TEXT NOT NULL DEFAULT 'chapter',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(audiobook_id) REFERENCES audiobooks(id) ON DELETE CASCADE
            );
            """,
        )

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS character_name_overrides (
                media_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                preferred TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(media_id,source)
            );
            CREATE TABLE IF NOT EXISTS media_identities (
                kind TEXT NOT NULL,
                local_id INTEGER NOT NULL,
                anilist_id INTEGER NOT NULL,
                anilist_type TEXT NOT NULL DEFAULT 'MANGA',
                title TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                site_url TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(kind,local_id)
            );
            """,
        )

    def _migrate_v8(self, conn: sqlite3.Connection) -> None:
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS personal_release_schedules (
                schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL DEFAULT 'default',
                anime_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                start_local_datetime TEXT NOT NULL,
                timezone_iana TEXT NOT NULL,
                cadence TEXT NOT NULL DEFAULT 'weekly',
                rule_version INTEGER NOT NULL DEFAULT 1,
                interval_unit TEXT NOT NULL DEFAULT 'weeks',
                interval_every INTEGER NOT NULL DEFAULT 1,
                interval_clock TEXT NOT NULL DEFAULT 'wall',
                cycle_id INTEGER NOT NULL DEFAULT 1,
                first_episode_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                rewatch INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(anime_id) REFERENCES anime(media_id) ON DELETE CASCADE,
                UNIQUE(profile_id, anime_id)
            );
            CREATE TABLE IF NOT EXISTS personal_release_items (
                schedule_id INTEGER NOT NULL,
                logical_episode_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                unlock_at_utc REAL NOT NULL,
                nominal_local_datetime TEXT NOT NULL,
                schedule_revision INTEGER NOT NULL,
                dst_adjusted INTEGER NOT NULL DEFAULT 0,
                unlocked_once INTEGER NOT NULL DEFAULT 0,
                watched_at REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(schedule_id, logical_episode_id),
                FOREIGN KEY(schedule_id) REFERENCES personal_release_schedules(schedule_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_personal_release_items_unlock
            ON personal_release_items(schedule_id, unlocked_once, unlock_at_utc, ordinal);
            """,
        )

    def _migrate_v9(self, conn: sqlite3.Connection) -> None:
        """Add stable consumption identities and the append-only R7 ledger."""
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS consumption_media (
                media_uuid TEXT PRIMARY KEY,kind TEXT NOT NULL,title_snapshot TEXT NOT NULL DEFAULT '',
                current_library_kind TEXT NOT NULL DEFAULT '',current_library_id TEXT NOT NULL DEFAULT '',
                source_revision TEXT NOT NULL DEFAULT '',metadata_json TEXT NOT NULL DEFAULT '{}',
                deleted_at REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_media_library ON consumption_media(current_library_kind,current_library_id);
            CREATE TABLE IF NOT EXISTS consumption_media_aliases (
                kind TEXT NOT NULL,alias_type TEXT NOT NULL,alias_value TEXT NOT NULL,media_uuid TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(kind,alias_type,alias_value),
                FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_alias_media ON consumption_media_aliases(media_uuid);
            CREATE TABLE IF NOT EXISTS vn_titles (
                id TEXT PRIMARY KEY,title TEXT NOT NULL,normalized_title TEXT NOT NULL,executable_hint TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(normalized_title,executable_hint)
            );
            CREATE TABLE IF NOT EXISTS consumption_devices (
                device_id TEXT PRIMARY KEY,next_seq INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumption_sessions (
                session_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL DEFAULT 'default',device_id TEXT NOT NULL,
                activity_group_id TEXT NOT NULL,kind TEXT NOT NULL,primary_media_uuid TEXT,
                started_at_utc REAL NOT NULL,ended_at_utc REAL,origin TEXT NOT NULL DEFAULT 'automatic',
                status TEXT NOT NULL DEFAULT 'active',measurement_method TEXT NOT NULL DEFAULT 'observation',
                policy_version TEXT NOT NULL DEFAULT 'r7-v1',schema_version INTEGER NOT NULL DEFAULT 1,metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(primary_media_uuid) REFERENCES consumption_media(media_uuid)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_sessions_time ON consumption_sessions(started_at_utc,ended_at_utc);
            CREATE INDEX IF NOT EXISTS idx_consumption_sessions_media ON consumption_sessions(primary_media_uuid,status,started_at_utc);
            CREATE TABLE IF NOT EXISTS consumption_events (
                event_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,device_seq INTEGER NOT NULL,session_id TEXT NOT NULL,kind TEXT NOT NULL,
                interval_start_utc REAL NOT NULL,interval_end_utc REAL NOT NULL,elapsed_monotonic_ms REAL NOT NULL,received_at_utc REAL NOT NULL,
                timezone_iana TEXT NOT NULL DEFAULT '',utc_offset INTEGER NOT NULL DEFAULT 0,payload_json TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL,schema_version INTEGER NOT NULL DEFAULT 1,UNIQUE(device_id,device_seq),
                FOREIGN KEY(session_id) REFERENCES consumption_sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_events_session ON consumption_events(session_id,interval_start_utc);
            CREATE INDEX IF NOT EXISTS idx_consumption_events_time ON consumption_events(interval_start_utc,interval_end_utc);
            CREATE TABLE IF NOT EXISTS consumption_event_media (
                event_id TEXT NOT NULL,media_uuid TEXT NOT NULL,source_revision TEXT NOT NULL DEFAULT '',role TEXT NOT NULL DEFAULT 'primary',
                locator_start_json TEXT NOT NULL DEFAULT '{}',locator_end_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(event_id,media_uuid,role),
                FOREIGN KEY(event_id) REFERENCES consumption_events(event_id) ON DELETE CASCADE,
                FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_event_media_media ON consumption_event_media(media_uuid,event_id);
            CREATE TABLE IF NOT EXISTS vn_capture_sessions (
                session_id TEXT PRIMARY KEY,vn_title_id TEXT NOT NULL,window_id INTEGER NOT NULL,generation INTEGER NOT NULL,started_at REAL NOT NULL,ended_at REAL,
                FOREIGN KEY(session_id) REFERENCES consumption_sessions(session_id),FOREIGN KEY(vn_title_id) REFERENCES vn_titles(id)
            );
            CREATE INDEX IF NOT EXISTS idx_vn_capture_title ON vn_capture_sessions(vn_title_id,started_at);
            """,
        )

    def _migrate_v10(self, conn: sqlite3.Connection) -> None:
        """Add R8 statistics corrections, manual entries and rebuildable rollups."""
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS consumption_corrections (
                correction_id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,
                revision INTEGER NOT NULL,parent_revision INTEGER NOT NULL DEFAULT 0,excluded INTEGER NOT NULL DEFAULT 0,
                replacement_start_utc REAL,replacement_end_utc REAL,replacement_duration_seconds REAL,
                reason TEXT NOT NULL DEFAULT '',device_id TEXT NOT NULL DEFAULT '',created_at_utc REAL NOT NULL,
                UNIQUE(target_type,target_id,revision)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_sessions_group
            ON consumption_sessions(activity_group_id,kind,started_at_utc);
            CREATE INDEX IF NOT EXISTS idx_consumption_corrections_target
            ON consumption_corrections(target_type,target_id,revision DESC);
            CREATE TABLE IF NOT EXISTS consumption_manual_entries (
                manual_id TEXT PRIMARY KEY,media_uuid TEXT,kind TEXT NOT NULL,title_snapshot TEXT NOT NULL DEFAULT '',
                started_at_utc REAL,duration_seconds REAL NOT NULL,note TEXT NOT NULL DEFAULT '',origin TEXT NOT NULL DEFAULT 'manual',
                created_at_utc REAL NOT NULL,updated_at_utc REAL NOT NULL,
                FOREIGN KEY(media_uuid) REFERENCES consumption_media(media_uuid)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_manual_time
            ON consumption_manual_entries(started_at_utc,kind);
            CREATE TABLE IF NOT EXISTS consumption_daily_rollups (
                local_day TEXT PRIMARY KEY,total_seconds REAL NOT NULL DEFAULT 0,breakdown_json TEXT NOT NULL DEFAULT '{}',
                event_count INTEGER NOT NULL DEFAULT 0,policy_version TEXT NOT NULL DEFAULT 'r8-v1',generated_at_utc REAL NOT NULL
            );
            """,
        )
        self._ensure_column(conn, "consumption_devices", "device_name", "TEXT NOT NULL DEFAULT ''")

    def _migrate_v11(self, conn: sqlite3.Connection) -> None:
        """Add R9 native-volume/offline consumption transport state."""
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS consumption_history_state (
                profile_id TEXT PRIMARY KEY,reset_epoch INTEGER NOT NULL DEFAULT 0,
                reset_at_utc REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS consumption_sync_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,record_type TEXT NOT NULL,
                record_key TEXT NOT NULL,payload_json TEXT NOT NULL,created_at_utc REAL NOT NULL,
                UNIQUE(record_type,record_key)
            );
            CREATE INDEX IF NOT EXISTS idx_consumption_sync_outbox_created
            ON consumption_sync_outbox(created_at_utc,id);
            CREATE TABLE IF NOT EXISTS consumption_sync_receipts (
                device_id TEXT NOT NULL,record_id TEXT NOT NULL,record_hash TEXT NOT NULL,
                received_at_utc REAL NOT NULL,PRIMARY KEY(device_id,record_id)
            );
            INSERT OR IGNORE INTO consumption_history_state(profile_id,reset_epoch,reset_at_utc)
            VALUES('default',0,0);
            """,
        )

    def _migrate_v13(self, conn: sqlite3.Connection) -> None:
        # Upgrade decisions compare scores only within one formula/context.
        self._ensure_column(conn, "release_history", "score_formula", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "release_history", "score_context", "TEXT NOT NULL DEFAULT ''")

    def _migrate_v12(self, conn: sqlite3.Connection) -> None:
        """Add versioned personal-release interval rules and cycle identity."""
        self._ensure_column(conn, "personal_release_schedules", "rule_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(
            conn, "personal_release_schedules", "interval_unit", "TEXT NOT NULL DEFAULT 'weeks'"
        )
        self._ensure_column(
            conn, "personal_release_schedules", "interval_every", "INTEGER NOT NULL DEFAULT 1"
        )
        self._ensure_column(
            conn, "personal_release_schedules", "interval_clock", "TEXT NOT NULL DEFAULT 'wall'"
        )
        self._ensure_column(conn, "personal_release_schedules", "cycle_id", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "personal_release_items", "dst_adjusted", "INTEGER NOT NULL DEFAULT 0")
        # Every pre-v12 schedule was the original one-episode-per-week rule.
        conn.execute(
            "UPDATE personal_release_schedules "
            "SET rule_version=1,interval_unit='weeks',interval_every=1,interval_clock='wall' "
            "WHERE cadence='weekly'"
        )

    def _scope_local(self) -> threading.local:
        local = getattr(self, "_connection_scope_local", None)
        if local is None:
            local = threading.local()
            self._connection_scope_local = local
        return local

    def _open_connection(self) -> sqlite3.Connection:
        if getattr(self, "_thread_connections", False):
            # A parked connection may be closed by retire() or its holder on another thread.
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, factory=_TrackedConnection)
        else:
            conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # WAL + NORMAL stays consistent after a crash or power loss but does not
        # fsync on every commit (Pudge commits on nearly every repository call).
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        parked = None
        with self._connection_condition:
            if self._retired:
                raise RuntimeError("Database is retired")
            self._active_connections += 1
            if getattr(self, "_thread_connections", False):
                local = self._scope_local()
                parked = getattr(local, "idle", None)
                local.idle = None
        conn = None
        identity = None
        reusable = False
        try:
            if parked is not None:
                with _PARKED_LOCK:
                    _PARKED.pop(id(parked), None)
                    conn, parked.conn = parked.conn, None  # None: evicted or closed by retire()
                if conn is not None and (
                    parked.pid != os.getpid() or parked.identity != _file_identity(self.path)
                ):
                    _close_quietly(conn)  # the library file was replaced (or a forked child)
                    conn = None
                identity = parked.identity
            if conn is None:
                conn = self._open_connection()
                identity = _file_identity(self.path)  # the file this connection opened
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                reusable = not conn.in_transaction
                raise
            reusable = not conn.in_transaction
        finally:
            try:
                if conn is not None and not (reusable and self._park(conn, identity)):
                    conn.close()
            finally:
                with self._connection_condition:
                    self._active_connections -= 1
                    self._connection_condition.notify_all()

    def _park(self, conn: sqlite3.Connection, identity: tuple[int, int] | None) -> bool:
        """Keep a clean connection for this thread's next call (thread_connections)."""
        if not getattr(self, "_thread_connections", False) or len(getattr(conn, "cursors", ())):
            return False
        evicted: list[sqlite3.Connection | None] = []
        with self._connection_condition:
            local = self._scope_local()
            if self._retired or identity is None or getattr(local, "idle", None) is not None:
                return False
            holder = _IdleConnection(conn, identity, self._park_owner)
            with _PARKED_LOCK:
                _PARKED[id(holder)] = holder
                while len(_PARKED) > _PARKED_LIMIT:
                    oldest = _PARKED.pop(next(iter(_PARKED)))
                    evicted.append(oldest.conn)
                    oldest.conn = None
            local.idle = holder
        for old in evicted:
            _close_quietly(old)
        return True

    def close_idle_connections(self) -> None:
        """Close this Database's parked connections (none is in use while parked)."""
        closing: list[sqlite3.Connection | None] = []
        with _PARKED_LOCK:
            for key, holder in list(_PARKED.items()):
                if holder.owner is self._park_owner:
                    del _PARKED[key]
                    closing.append(holder.conn)
                    holder.conn = None
        for conn in closing:
            _close_quietly(conn)

    def _parked_count(self) -> int:
        with _PARKED_LOCK:
            return sum(1 for holder in _PARKED.values() if holder.owner is self._park_owner)

    def retire(self, *, timeout: float = 5.0) -> None:
        """Reject new handles and drain admitted transactions before restore."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._connection_condition:
            self._retired = True
            while self._active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(err("backup.dbBusy"))
                self._connection_condition.wait(remaining)
        # Restore removes WAL/SHM and replaces the file: no handle may stay open.
        self.close_idle_connections()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # API payload assembly calls many small repository methods. When the
        # caller established connection_scope(), reuse one thread-local SQLite
        # connection instead of reparsing the large schema dozens of times.
        local = self._scope_local()
        shared = getattr(local, "connection", None)
        if shared is not None:
            if getattr(local, "per_call", False):
                with self._reused_call(local, shared) as conn:
                    yield conn
                return
            with self._scoped_call(local, shared) as conn:
                yield conn
            return
        with self._managed_connection() as conn:
            yield conn

    @contextmanager
    def _scoped_call(
        self, local: threading.local, conn: sqlite3.Connection, *, nested_scope: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """One repository call (or a nested scope) inside connection_scope().

        A write used to stay uncommitted until the whole scope ended, so a Home
        snapshot held the SQLite write lock for its full duration and blocked
        mpv progress saves, subtitle workers and the agent. Now the outermost
        call commits (or rolls back) its own writes like a fresh connection,
        unless a transaction is already open (the scope owner ran BEGIN
        IMMEDIATE): then everything stays one atomic transaction until the
        scope that opened it ends. A nested scope does not count as a call, so
        the calls inside it still commit one by one.
        """
        depth = int(getattr(local, "scope_depth", 0) or 0)
        owns = not depth and not conn.in_transaction
        if not nested_scope:
            local.scope_depth = depth + 1
        try:
            try:
                yield conn
            except BaseException:
                if owns and conn.in_transaction:
                    conn.rollback()
                raise
            if owns and conn.in_transaction:
                conn.commit()
        finally:
            local.scope_depth = depth

    @contextmanager
    def _reused_call(self, local: threading.local, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """One repository call on a reused connection, committed like a fresh one."""
        depth = int(getattr(local, "depth", 0) or 0)
        if depth:
            local.depth = depth + 1
            try:
                yield conn
            finally:
                local.depth = depth
            return
        with self._connection_condition:
            if self._retired:
                raise RuntimeError("Database is retired")
            self._active_connections += 1
        local.depth = 1
        try:
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            local.depth = 0
            with self._connection_condition:
                self._active_connections -= 1
                self._connection_condition.notify_all()

    @contextmanager
    def reuse_connection(self) -> Iterator[None]:
        """Share one SQLite connection across a long pass of small calls.

        Unlike connection_scope(), every outermost call still commits or rolls
        back on its own exactly as with a fresh connection, and no lock or
        transaction is held between calls (restore can still retire the
        database mid-pass). Only the per-call open, schema parse and close are
        saved — the dominant cost of library scans and maintenance passes.
        """
        local = self._scope_local()
        if getattr(local, "connection", None) is not None:
            yield
            return
        conn = self._open_connection()
        local.connection, local.per_call, local.depth = conn, True, 0
        try:
            yield
        finally:
            local.connection, local.per_call, local.depth = None, False, 0
            try:
                conn.rollback()
            finally:
                conn.close()

    @contextmanager
    def connection_scope(self) -> Iterator[sqlite3.Connection]:
        local = self._scope_local()
        shared = getattr(local, "connection", None)
        if shared is not None:
            if getattr(local, "per_call", False):
                # Inside reuse_connection() a scope is one atomic call.
                with self._reused_call(local, shared) as conn:
                    yield conn
                return
            with self._scoped_call(local, shared, nested_scope=True) as conn:
                yield conn
            return
        with self._managed_connection() as conn:
            local.connection, local.scope_depth = conn, 0
            try:
                yield conn
            finally:
                local.connection, local.scope_depth = None, 0

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        """Separate AniList-local progress from release filename numbering."""
        for table in ("episodes", "downloads", "release_history"):
            self._ensure_column(conn, table, "media_episode", "INTEGER")
            self._ensure_column(conn, table, "release_episode", "INTEGER")
            conn.execute(
                f"UPDATE {table} SET media_episode=episode "
                "WHERE media_episode IS NULL AND episode IS NOT NULL"
            )
            conn.execute(
                f"UPDATE {table} SET release_episode=episode "
                "WHERE release_episode IS NULL AND episode IS NOT NULL"
            )
        # Managed single-episode torrents already carried the season-local
        # request in downloads. Use that durable link to repair episode rows
        # written by versions that stored an absolute filename number there.
        conn.execute(
            """
            UPDATE episodes
            SET media_episode=(
                    SELECT COALESCE(downloads.media_episode,downloads.episode)
                    FROM downloads
                    WHERE downloads.torrent_hash=episodes.torrent_hash
                      AND downloads.media_id=episodes.media_id
                ),
                episode=(
                    SELECT COALESCE(downloads.media_episode,downloads.episode)
                    FROM downloads
                    WHERE downloads.torrent_hash=episodes.torrent_hash
                      AND downloads.media_id=episodes.media_id
                )
            WHERE torrent_hash!=''
              AND EXISTS(
                    SELECT 1 FROM downloads
                    WHERE downloads.torrent_hash=episodes.torrent_hash
                      AND downloads.media_id=episodes.media_id
                      AND COALESCE(downloads.media_episode,downloads.episode) IS NOT NULL
                )
            """
        )
        _execute_sql_script(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_episodes_media_identity
            ON episodes(media_id,media_episode);
            CREATE INDEX IF NOT EXISTS idx_episodes_release_identity
            ON episodes(media_id,release_episode);
            """,
        )

    def _migrate_v6(self, conn: sqlite3.Connection) -> None:
        """Add the device-neutral event log used by companion clients."""
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS sync_devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'unknown',
                token_hash TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL DEFAULT 0,
                revoked_at REAL NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_devices_token
            ON sync_devices(token_hash) WHERE token_hash!='';

            CREATE TABLE IF NOT EXISTS sync_pairing_codes (
                token_hash TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sync_pairing_expiry
            ON sync_pairing_codes(expires_at);

            CREATE TABLE IF NOT EXISTS sync_entities (
                entity_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                local_key TEXT NOT NULL,
                external_key TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(kind,local_key)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_entities_external
            ON sync_entities(kind,external_key);

            CREATE TABLE IF NOT EXISTS sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_uuid TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                occurred_at REAL NOT NULL,
                received_at REAL NOT NULL,
                FOREIGN KEY(device_id) REFERENCES sync_devices(device_id),
                FOREIGN KEY(entity_id) REFERENCES sync_entities(entity_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sync_events_cursor ON sync_events(id);
            CREATE INDEX IF NOT EXISTS idx_sync_events_entity
            ON sync_events(entity_id,id);

            CREATE TABLE IF NOT EXISTS sync_snapshots (
                entity_id TEXT PRIMARY KEY,
                position_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'not_started',
                source_device_id TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                occurred_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(entity_id) REFERENCES sync_entities(entity_id) ON DELETE CASCADE,
                FOREIGN KEY(source_device_id) REFERENCES sync_devices(device_id),
                FOREIGN KEY(event_id) REFERENCES sync_events(id) ON DELETE CASCADE
            );
            """,
        )

    def _migrate_v7(self, conn: sqlite3.Connection) -> None:
        """Add durable infrastructure for auditability and resumable work."""

        self._ensure_column(conn, "app_jobs", "resumable", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "app_jobs", "correlation_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sync_snapshots", "revision", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "sync_snapshots", "base_event_id", "INTEGER NOT NULL DEFAULT 0")
        _execute_sql_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS app_job_checkpoints (
                job_id TEXT PRIMARY KEY,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL,
                FOREIGN KEY(job_id) REFERENCES app_jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS media_identity_ledger (
                canonical_id TEXT PRIMARY KEY,
                media_id INTEGER,
                media_episode INTEGER,
                release_episode INTEGER,
                video_path TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                torrent_hash TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                locked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_identity_media_episode
            ON media_identity_ledger(media_id,media_episode);
            CREATE INDEX IF NOT EXISTS idx_identity_release_episode
            ON media_identity_ledger(media_id,release_episode);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_video_path
            ON media_identity_ledger(video_path) WHERE video_path!='';
            CREATE INDEX IF NOT EXISTS idx_identity_torrent
            ON media_identity_ledger(torrent_hash) WHERE torrent_hash!='';

            CREATE TABLE IF NOT EXISTS sync_dirty_entities (
                kind TEXT NOT NULL,
                local_key TEXT NOT NULL,
                dirtied_at REAL NOT NULL,
                PRIMARY KEY(kind,local_key)
            );
            CREATE TABLE IF NOT EXISTS sync_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                base_revision INTEGER NOT NULL DEFAULT 0,
                current_revision INTEGER NOT NULL DEFAULT 0,
                incoming_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                resolved_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(entity_id) REFERENCES sync_entities(entity_id) ON DELETE CASCADE,
                FOREIGN KEY(device_id) REFERENCES sync_devices(device_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_conflicts_open
            ON sync_conflicts(resolved_at,created_at DESC);

            CREATE TABLE IF NOT EXISTS torrent_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info_hash TEXT NOT NULL,
                media_id INTEGER,
                media_episode INTEGER,
                provider TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                listed_seeders INTEGER NOT NULL DEFAULT 0,
                listed_leechers INTEGER NOT NULL DEFAULT 0,
                live_seeders INTEGER,
                live_leechers INTEGER,
                metadata_seconds REAL,
                download_speed_bps REAL,
                outcome TEXT NOT NULL DEFAULT 'candidate',
                details_json TEXT NOT NULL DEFAULT '{}',
                observed_at REAL NOT NULL,
                FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_torrent_observations_release
            ON torrent_observations(info_hash,observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_torrent_observations_media
            ON torrent_observations(media_id,media_episode,observed_at DESC);

            CREATE TABLE IF NOT EXISTS cache_registry (
                cache_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_cache_registry_eviction
            ON cache_registry(category,pinned,accessed_at);

            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL,
                component TEXT NOT NULL,
                event TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                media_id INTEGER,
                media_episode INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                FOREIGN KEY(media_id) REFERENCES anime(media_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_diagnostic_events_correlation
            ON diagnostic_events(correlation_id,id);
            CREATE INDEX IF NOT EXISTS idx_diagnostic_events_media
            ON diagnostic_events(media_id,media_episode,created_at DESC);
            """,
        )

    def bump_ui_state_version(self) -> str:
        """``ui_state_version + 1`` in one statement; returns the new value.

        A read, +1 and set_state raced the triggers that increment it in SQL: bumps
        were lost and the version could move back to one a page already held, which
        then saw "unchanged" and kept stale data (DB-8).
        """
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES('ui_state_version','1',?) "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(state.value AS INTEGER)+1,"
                "updated_at=excluded.updated_at",
                (time.time(),),
            )
            row = conn.execute("SELECT value FROM state WHERE key='ui_state_version'").fetchone()
        return str(row["value"]) if row else ""

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_states(self, keys: tuple[str, ...]) -> dict[str, str]:
        """Fetch a small set of state values using one SQLite connection."""
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT key,value FROM state WHERE key IN ({placeholders})", keys).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM state WHERE key=?", (key,))

    def delete_states(self, keys: Iterable[str]) -> int:
        """Delete several state rows in one transaction; returns how many existed."""
        unique = list(dict.fromkeys(str(key) for key in keys if key))
        removed = 0
        with self.connect() as conn:
            for start in range(0, len(unique), 400):
                chunk = unique[start : start + 400]
                cursor = conn.execute(
                    f"DELETE FROM state WHERE key IN ({','.join('?' * len(chunk))})", tuple(chunk)
                )
                removed += max(0, int(cursor.rowcount or 0))
        return removed

    def set_state_if_subtitle_job_owned(
        self,
        key: str,
        value: str,
        *,
        video_path: Path,
        generation: int,
        owner_token: str,
    ) -> bool:
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, time.time()),
            )
            return True

    def delete_state_if_subtitle_job_owned(
        self,
        key: str,
        *,
        video_path: Path,
        generation: int,
        owner_token: str,
    ) -> bool:
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute("DELETE FROM state WHERE key=?", (key,))
            return True

    def prune_history(self, *, now: float | None = None, keep_seconds: float = 5 * 86400) -> dict[str, int]:
        """Drop debug/history rows older than ``keep_seconds`` (5 days).

        Kept regardless of age: the latest subtitle choice per video and the
        latest selected one, the latest release per episode and every release
        whose torrent is still on disk or downloading (upgrades and recovery
        read them), and the last 100 winning torrent observations per provider
        (they tune race deadlines).
        """
        cutoff = (time.time() if now is None else float(now)) - max(0.0, float(keep_seconds))
        removed: dict[str, int] = {}
        statements = {
            "diagnostic_events": ("DELETE FROM diagnostic_events WHERE created_at<?", (cutoff,)),
            "episode_state_history": ("DELETE FROM episode_state_history WHERE created_at<?", (cutoff,)),
            "subtitle_history": (
                """DELETE FROM subtitle_history WHERE created_at<? AND id NOT IN (
                       SELECT MAX(id) FROM subtitle_history GROUP BY video_path
                       UNION SELECT MAX(id) FROM subtitle_history
                             WHERE status IN ('selected','upgraded','manual','legacy') GROUP BY video_path)""",
                (cutoff,),
            ),
            "release_history": (
                """DELETE FROM release_history WHERE selected_at<?
                       AND lower(info_hash) NOT IN (SELECT lower(torrent_hash) FROM episodes WHERE COALESCE(torrent_hash,'')<>'')
                       AND lower(info_hash) NOT IN (SELECT lower(torrent_hash) FROM downloads WHERE COALESCE(torrent_hash,'')<>'')
                       AND info_hash NOT IN (
                           SELECT info_hash FROM release_history r
                           WHERE r.selected_at=(SELECT MAX(selected_at) FROM release_history x
                                                WHERE x.media_id IS r.media_id
                                                  AND COALESCE(x.media_episode,x.episode) IS COALESCE(r.media_episode,r.episode)))""",
                (cutoff,),
            ),
            "torrent_observations": (
                """DELETE FROM torrent_observations WHERE observed_at<? AND id NOT IN (
                       SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY provider ORDER BY observed_at DESC) AS n
                                       FROM torrent_observations WHERE outcome='winner') WHERE n<=100)""",
                (cutoff,),
            ),
            "playback_markers": (
                f"DELETE FROM state WHERE {STATE_PREFIX_SQL} AND updated_at<?",
                (*state_prefix_bounds("playback_save:v1:"), cutoff),
            ),
            # When a two-episode release was first seen: it matters for ten minutes
            # (AnimeManager.DOUBLE_EPISODE_WAIT_SECONDS), one row per release stayed forever.
            "double_episode_seen": (
                f"DELETE FROM state WHERE {STATE_PREFIX_SQL} AND updated_at<?",
                (*state_prefix_bounds("double_episode_seen:"), cutoff),
            ),
            # aria2 stall markers that older versions emptied instead of deleting.
            "aria2_stall_markers": (
                f"DELETE FROM state WHERE {STATE_PREFIX_SQL} AND value=''",
                state_prefix_bounds("aria2_stall:"),
            ),
        }
        with self.connect() as conn:
            tables = {
                str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for name, (sql, args) in statements.items():
                table = "state" if name in _STATE_PRUNE_ROWS else name
                if table not in tables:
                    continue
                try:
                    removed[name] = int(conn.execute(sql, args).rowcount or 0)
                except sqlite3.OperationalError as exc:
                    # An older SQLite without window functions keeps the rows.
                    removed[name] = 0
                    _LOGGER.warning("SKIP step=database.prune table=%s error=%r", name, exc)
        return removed

    def release_numbering_rules(self, media_id: int) -> list[dict[str, object]]:
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT media_id,release_group,season_key,episode_offset,example_title,created_at "
                    "FROM release_numbering_rules WHERE media_id=? ORDER BY created_at",
                    (int(media_id),),
                ).fetchall()
            except sqlite3.OperationalError:  # opened read-only before the app created the table
                return []
        return [dict(row) for row in rows]

    def store_release_numbering_rule(
        self, media_id: int, release_group: str, season_key: str, offset: int, *, example_title: str = ""
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO release_numbering_rules(media_id,release_group,season_key,episode_offset,example_title,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(media_id,release_group,season_key) DO UPDATE SET "
                "episode_offset=excluded.episode_offset,example_title=excluded.example_title,created_at=excluded.created_at",
                (
                    int(media_id),
                    str(release_group).casefold(),
                    str(season_key),
                    int(offset),
                    str(example_title)[:300],
                    time.time(),
                ),
            )
        _numbering_rules_changed()

    def forget_release_numbering_rules(self, media_id: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM release_numbering_rules WHERE media_id=?", (int(media_id),))
        _numbering_rules_changed()
        return int(cursor.rowcount or 0)

    def relation_graph_for_media(self, media_id: int) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT g.graph_id,g.graph_json,g.refreshed_at,g.next_refresh_at
                FROM relation_graph_members m
                JOIN relation_graphs g ON g.graph_id=m.graph_id
                WHERE m.media_id=?
                """,
                (int(media_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            graph = json.loads(str(row["graph_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            graph = {}
        if not isinstance(graph, dict):
            graph = {}
        return {
            "graph_id": str(row["graph_id"]),
            "graph": graph,
            "refreshed_at": float(row["refreshed_at"] or 0.0),
            "next_refresh_at": float(row["next_refresh_at"] or 0.0),
        }

    def relation_graph_cache(
        self,
        media_ids: Iterable[int],
    ) -> list[dict[str, object]]:
        """Return each cached franchise component once, including all members.

        The web UI preloads this compact shared cache so opening Watch Order does
        not need a Python/SQLite round trip and the first visible modal frame is
        already fully rendered.
        """
        requested_ids = sorted({int(media_id) for media_id in media_ids})
        if not requested_ids:
            return []
        placeholders = ",".join("?" for _ in requested_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT g.graph_id,g.graph_json,g.refreshed_at,g.next_refresh_at,
                       m.media_id
                FROM relation_graphs g
                JOIN relation_graph_members m ON m.graph_id=g.graph_id
                WHERE g.graph_id IN (
                    SELECT DISTINCT graph_id
                    FROM relation_graph_members
                    WHERE media_id IN ({placeholders})
                )
                ORDER BY g.graph_id,m.media_id
                """,
                tuple(requested_ids),
            ).fetchall()

        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            graph_id = str(row["graph_id"])
            item = grouped.get(graph_id)
            if item is None:
                try:
                    graph = json.loads(str(row["graph_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    graph = {}
                item = {
                    "graph_id": graph_id,
                    "graph": graph if isinstance(graph, dict) else {},
                    "refreshed_at": float(row["refreshed_at"] or 0.0),
                    "next_refresh_at": float(row["next_refresh_at"] or 0.0),
                    "members": [],
                }
                grouped[graph_id] = item
            members = item["members"]
            assert isinstance(members, list)
            members.append(int(row["media_id"]))
        return list(grouped.values())

    def due_relation_graphs(
        self,
        *,
        now: float | None = None,
        limit: int = 1,
    ) -> list[dict[str, object]]:
        current = time.time() if now is None else float(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.graph_id,g.graph_json,g.refreshed_at,g.next_refresh_at,
                       MIN(m.media_id) AS media_id
                FROM relation_graphs g
                JOIN relation_graph_members m ON m.graph_id=g.graph_id
                WHERE g.next_refresh_at<=?
                GROUP BY g.graph_id
                ORDER BY g.next_refresh_at,g.graph_id
                LIMIT ?
                """,
                (current, max(1, int(limit))),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            try:
                graph = json.loads(str(row["graph_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                graph = {}
            result.append(
                {
                    "graph_id": str(row["graph_id"]),
                    "graph": graph if isinstance(graph, dict) else {},
                    "media_id": int(row["media_id"]),
                    "refreshed_at": float(row["refreshed_at"] or 0.0),
                    "next_refresh_at": float(row["next_refresh_at"] or 0.0),
                }
            )
        return result

    def store_relation_graph(
        self,
        graph: dict[str, object],
        *,
        refreshed_at: float,
        next_refresh_at: float,
        preferred_graph_id: str = "",
    ) -> str:
        node_ids = sorted(
            {
                int(node["media_id"])
                for node in graph.get("nodes", [])
                if isinstance(node, dict) and node.get("media_id") is not None
            }
        )
        if not node_ids:
            raise ValueError("relation graph has no nodes")
        placeholders = ",".join("?" for _ in node_ids)
        with self.connect() as conn:
            existing_rows = conn.execute(
                f"SELECT DISTINCT graph_id FROM relation_graph_members WHERE media_id IN ({placeholders})",
                tuple(node_ids),
            ).fetchall()
            existing_ids = sorted({str(row["graph_id"]) for row in existing_rows})
            graph_id = str(preferred_graph_id or "").strip()
            if not graph_id:
                graph_id = existing_ids[0] if existing_ids else f"component-{node_ids[0]}"

            for stale_id in existing_ids:
                if stale_id != graph_id:
                    conn.execute(
                        "DELETE FROM relation_graph_members WHERE graph_id=?",
                        (stale_id,),
                    )
                    conn.execute("DELETE FROM relation_graphs WHERE graph_id=?", (stale_id,))

            conn.execute(
                """
                INSERT INTO relation_graphs(graph_id,graph_json,refreshed_at,next_refresh_at)
                VALUES(?,?,?,?)
                ON CONFLICT(graph_id) DO UPDATE SET
                    graph_json=excluded.graph_json,
                    refreshed_at=excluded.refreshed_at,
                    next_refresh_at=excluded.next_refresh_at
                """,
                (
                    graph_id,
                    json.dumps(graph, ensure_ascii=False),
                    float(refreshed_at),
                    float(next_refresh_at),
                ),
            )
            conn.execute("DELETE FROM relation_graph_members WHERE graph_id=?", (graph_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO relation_graph_members(media_id,graph_id) VALUES(?,?)",
                [(media_id, graph_id) for media_id in node_ids],
            )
            conn.execute(
                """
                DELETE FROM relation_graphs
                WHERE graph_id NOT IN (SELECT DISTINCT graph_id FROM relation_graph_members)
                """
            )
        return graph_id

    def defer_relation_graph(self, graph_id: str, next_refresh_at: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE relation_graphs SET next_refresh_at=? WHERE graph_id=?",
                (float(next_refresh_at), str(graph_id)),
            )

    def update_anime_relations(self, values: dict[int, list[dict[str, object]]]) -> int:
        if not values:
            return 0
        changed = 0
        with self.connect() as conn:
            for media_id, relations in values.items():
                cursor = conn.execute(
                    "UPDATE anime SET relations_json=?,updated_at=? WHERE media_id=?",
                    (json.dumps(relations, ensure_ascii=False), time.time(), int(media_id)),
                )
                changed += int(cursor.rowcount or 0)
        return changed

    def upsert_anime(self, anime: LibraryAnime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO anime(
                    media_id,title,titles_json,synonyms_json,cover_url,site_url,status,
                    progress,episodes,format,season_year,start_date,studio,media_status,end_date,mean_score,user_score,duration,next_airing_episode,next_airing_at,relations_json,list_added_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    title=excluded.title,titles_json=excluded.titles_json,
                    synonyms_json=excluded.synonyms_json,cover_url=excluded.cover_url,
                    site_url=excluded.site_url,status=excluded.status,
                    progress=excluded.progress,episodes=excluded.episodes,format=excluded.format,
                    season_year=excluded.season_year,start_date=excluded.start_date,studio=excluded.studio,media_status=excluded.media_status,end_date=excluded.end_date,mean_score=excluded.mean_score,
                    user_score=excluded.user_score,duration=excluded.duration,next_airing_episode=excluded.next_airing_episode,
                    next_airing_at=excluded.next_airing_at,relations_json=excluded.relations_json,
                    list_added_at=CASE WHEN excluded.list_added_at>0 THEN excluded.list_added_at
                        ELSE anime.list_added_at END,
                    updated_at=excluded.updated_at
                """,
                (
                    anime.media_id,
                    anime.title,
                    json.dumps(anime.titles, ensure_ascii=False),
                    json.dumps(anime.synonyms, ensure_ascii=False),
                    anime.cover_url,
                    anime.site_url,
                    anime.status,
                    anime.progress,
                    anime.episodes,
                    anime.format,
                    anime.season_year,
                    anime.start_date,
                    anime.studio,
                    anime.media_status,
                    anime.end_date,
                    anime.mean_score,
                    anime.user_score,
                    anime.duration,
                    anime.next_airing_episode,
                    anime.next_airing_at,
                    json.dumps(anime.relations, ensure_ascii=False),
                    # Rows built from relation graphs, imports or local files lack
                    # the AniList add time; the CASE above keeps a stored one.
                    max(0.0, float(anime.list_added_at or 0)),
                    time.time(),
                ),
            )
            if anime.status:
                self.note_anilist_progress(anime.media_id, int(anime.progress or 0), conn=conn)

    def anime_list(self, statuses: tuple[str, ...] | None = None) -> list[LibraryAnime]:
        sql = "SELECT * FROM anime"
        params: tuple[object, ...] = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params = tuple(statuses)
        sql += " ORDER BY title COLLATE NOCASE"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._anime_from_row(row) for row in rows]

    def get_anime(self, media_id: int) -> LibraryAnime | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM anime WHERE media_id=?", (media_id,)).fetchone()
        return self._anime_from_row(row) if row else None

    @staticmethod
    def _anime_from_row(row: sqlite3.Row) -> LibraryAnime:
        return LibraryAnime(
            media_id=int(row["media_id"]),
            title=str(row["title"]),
            titles=list(json.loads(row["titles_json"] or "[]")),
            synonyms=list(json.loads(row["synonyms_json"] or "[]")),
            cover_url=str(row["cover_url"] or ""),
            site_url=str(row["site_url"] or ""),
            status=str(row["status"] or ""),
            progress=int(row["progress"] or 0),
            episodes=int(row["episodes"]) if row["episodes"] is not None else None,
            format=str(row["format"]) if row["format"] else None,
            season_year=int(row["season_year"])
            if "season_year" in row.keys() and row["season_year"] is not None
            else None,
            start_date=str(row["start_date"]) if "start_date" in row.keys() and row["start_date"] else None,
            studio=str(row["studio"] or "") if "studio" in row.keys() else "",
            media_status=str(row["media_status"]) if row["media_status"] else None,
            end_date=str(row["end_date"]) if row["end_date"] else None,
            mean_score=int(row["mean_score"]) if row["mean_score"] is not None else None,
            user_score=float(row["user_score"])
            if "user_score" in row.keys() and row["user_score"] is not None
            else None,
            duration=int(row["duration"]) if row["duration"] is not None else None,
            next_airing_episode=(
                int(row["next_airing_episode"]) if row["next_airing_episode"] is not None else None
            ),
            next_airing_at=int(row["next_airing_at"]) if row["next_airing_at"] else None,
            relations=list(json.loads(row["relations_json"] or "[]"))
            if "relations_json" in row.keys()
            else [],
            # The scheduled agent opens the database without migrating it.
            list_added_at=float(row["list_added_at"] or 0) if "list_added_at" in row.keys() else 0.0,
        )

    @staticmethod
    def _ensure_anime_parent(
        conn: sqlite3.Connection,
        media_id: int | None,
        title: str = "",
    ) -> None:
        """Keep legacy import paths valid while enforcing SQLite foreign keys.

        Older versions silently allowed an episode/download to arrive before
        its AniList row because foreign keys were disabled on normal
        connections.  Preserve that ordering by creating a minimal parent row;
        the next AniList refresh fills it using the regular upsert.
        """

        if media_id is None:
            return
        now = time.time()
        # Callers pass what they have: a torrent, release or file name. The row is
        # only a placeholder until AniList fills it, and a file name is no title.
        conn.execute(
            "INSERT OR IGNORE INTO anime(media_id,title,updated_at) VALUES(?,?,?)",
            (int(media_id), release_name_display_title(str(title or ""), int(media_id)), now),
        )

    @staticmethod
    def _span_end(name: str, first: int | None, end: int | None, *, batch: bool = False) -> int | None:
        """The second episode of a two-episode file or download ("01-02", audit 17А).

        Derived once when a row is written; reads trust the stored column.
        """
        if end is not None or first is None or batch:
            return end
        return int(first) + 1 if double_episode_span(Path(str(name or "")).name) is not None else None

    def upsert_episode(self, episode: LibraryEpisode, *, downloaded_at: float | None = None) -> None:
        now = time.time()
        episode_end = self._span_end(
            str(episode.video_path), episode.media_episode, episode.media_episode_end
        )
        with self.connect() as conn:
            self._ensure_anime_parent(conn, episode.media_id, episode.title)
            previous = conn.execute(
                "SELECT state FROM episodes WHERE video_path=?", (str(episode.video_path),)
            ).fetchone()
            requested_state = transition_episode_state(
                str(previous["state"]) if previous is not None else "local",
                episode.state,
                trigger="scan",
            )
            conn.execute(
                """
                INSERT INTO episodes(
                    media_id,title,episode,media_episode,release_episode,video_path,subtitle_path,embedded_subtitle_id,subtitle_origin,state,torrent_hash,
                    downloaded_at,watched_at,delete_after,updated_at,media_episode_end
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,episodes.media_id),
                    title=excluded.title,
                    episode=COALESCE(excluded.media_episode,episodes.episode),
                    media_episode=COALESCE(excluded.media_episode,episodes.media_episode,episodes.episode),
                    media_episode_end=CASE WHEN excluded.media_episode IS NOT NULL
                        THEN excluded.media_episode_end ELSE episodes.media_episode_end END,
                    release_episode=COALESCE(excluded.release_episode,episodes.release_episode),
                    subtitle_path=CASE
                        WHEN excluded.subtitle_path IS NOT NULL THEN excluded.subtitle_path
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles','couldnt_sync')
                        THEN episodes.subtitle_path
                        ELSE NULL
                    END,
                    embedded_subtitle_id=CASE
                        WHEN excluded.embedded_subtitle_id IS NOT NULL THEN excluded.embedded_subtitle_id
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles','couldnt_sync')
                        THEN episodes.embedded_subtitle_id
                        ELSE NULL
                    END,
                    subtitle_origin=CASE
                        WHEN excluded.subtitle_origin!='' THEN excluded.subtitle_origin
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles','couldnt_sync')
                        THEN episodes.subtitle_origin
                        ELSE ''
                    END,
                    state=excluded.state,
                    torrent_hash=CASE WHEN excluded.torrent_hash!='' THEN excluded.torrent_hash ELSE episodes.torrent_hash END,
                    downloaded_at=COALESCE(excluded.downloaded_at,episodes.downloaded_at),
                    watched_at=COALESCE(excluded.watched_at,episodes.watched_at),
                    delete_after=COALESCE(excluded.delete_after,episodes.delete_after),
                    updated_at=excluded.updated_at
                """,
                (
                    episode.media_id,
                    episode.title,
                    episode.media_episode,
                    episode.media_episode,
                    episode.release_episode,
                    str(episode.video_path),
                    str(episode.subtitle_path) if episode.subtitle_path else None,
                    episode.embedded_subtitle_id,
                    episode.subtitle_origin,
                    requested_state,
                    episode.torrent_hash,
                    downloaded_at,
                    episode.watched_at,
                    episode.delete_after,
                    now,
                    episode_end,
                ),
            )
            if previous is not None and str(previous["state"]) != requested_state:
                conn.execute(
                    "INSERT INTO episode_state_history(video_path,from_state,to_state,trigger,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (str(episode.video_path), str(previous["state"]), requested_state, "scan", now),
                )

    @staticmethod
    def _transition_episode(
        conn: sqlite3.Connection,
        video_path: Path,
        requested: str,
        *,
        trigger: str,
    ) -> str:
        row = conn.execute("SELECT state FROM episodes WHERE video_path=?", (str(video_path),)).fetchone()
        if row is None:
            return str(requested)
        current = str(row["state"] or "local")
        resolved = transition_episode_state(current, requested, trigger=trigger)
        if resolved != current:
            now = time.time()
            conn.execute(
                "UPDATE episodes SET state=?,updated_at=? WHERE video_path=?",
                (resolved, now, str(video_path)),
            )
            conn.execute(
                "INSERT INTO episode_state_history(video_path,from_state,to_state,trigger,created_at) "
                "VALUES(?,?,?,?,?)",
                (str(video_path), current, resolved, str(trigger), now),
            )
        return resolved

    def episodes(self, media_id: int | None = None) -> list[LibraryEpisode]:
        sql = "SELECT * FROM episodes"
        params: tuple[object, ...] = ()
        if media_id is not None:
            sql += " WHERE media_id=?"
            params = (media_id,)
        sql += " ORDER BY title COLLATE NOCASE, COALESCE(media_episode,episode), video_path"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._episode_from_row(row) for row in rows]

    def episode_rows(self, media_id: int, episode: int) -> list[LibraryEpisode]:
        """Every row of one title's episode (several files may hold it)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE media_id=? AND COALESCE(media_episode,episode)=? ORDER BY video_path",
                (int(media_id), int(episode)),
            ).fetchall()
        return [self._episode_from_row(row) for row in rows]

    def episode_by_path(self, video_path: Path) -> LibraryEpisode | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE video_path=?", (str(video_path),)).fetchone()
        return self._episode_from_row(row) if row else None

    def ready_episode(self, media_id: int, episode: int | None = None) -> LibraryEpisode | None:
        sql = "SELECT * FROM episodes WHERE media_id=? AND state IN ('ready','local','watched','waiting_subtitles','waiting_text_subtitles','couldnt_sync')"
        params: list[object] = [media_id]
        if episode is not None:
            # A two-episode file also serves its second episode (audit 17А).
            sql += (
                " AND ? BETWEEN COALESCE(media_episode,episode)"
                " AND COALESCE(media_episode_end,media_episode,episode)"
            )
            params.append(episode)
            # An exact single-episode file wins over a double that also covers it.
            sql += " ORDER BY CASE WHEN COALESCE(media_episode,episode)=? THEN 0 ELSE 1 END, COALESCE(media_episode,episode), id"
            params.append(episode)
            with self.connect() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
                if not rows:
                    return None
                number = (
                    rows[0]["media_episode"] if rows[0]["media_episode"] is not None else rows[0]["episode"]
                )
                tied = [
                    row
                    for row in rows
                    if (row["media_episode"] if row["media_episode"] is not None else row["episode"])
                    == number
                ]
                if len(tied) > 1:
                    # Two files of the same episode (an older single and a later batch):
                    # the copy the phone list offers too (preferred_copy).
                    tied = [preferred_copy(tied, self._folder_counts(conn, media_id))]
            return self._episode_from_row(tied[0])
        else:
            sql += " ORDER BY CASE WHEN COALESCE(media_episode,episode) IS NULL THEN 1 ELSE 0 END, COALESCE(media_episode,episode) LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return self._episode_from_row(row) if row else None

    @staticmethod
    def _folder_counts(conn: sqlite3.Connection, media_id: int) -> dict[str, int]:
        """Rows of one title per folder (a batch keeps its episodes together)."""
        counts: dict[str, int] = {}
        for row in conn.execute("SELECT video_path FROM episodes WHERE media_id=?", (int(media_id),)):
            folder = str(Path(str(row["video_path"])).parent)
            counts[folder] = counts.get(folder, 0) + 1
        return counts

    def folder_counts(self, media_id: int) -> dict[str, int]:
        with self.connect() as conn:
            return self._folder_counts(conn, media_id)

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> LibraryEpisode:
        # One keys() per row: it builds a fresh list on every call, and asking it once per
        # optional column made converting a full episodes() listing (dozens of passes call it)
        # noticeably slower. Columns added by later migrations may be missing from old rows.
        get = dict(zip(row.keys(), row)).get
        episode = get("episode")
        episode = int(episode) if episode is not None else None
        media_episode = get("media_episode")
        media_episode = int(media_episode) if media_episode is not None else episode
        release_episode = get("release_episode")
        release_episode = int(release_episode) if release_episode is not None else episode
        media_id = get("media_id")
        subtitle_path = get("subtitle_path")
        embedded_subtitle_id = get("embedded_subtitle_id")
        watched_at = get("watched_at")
        delete_after = get("delete_after")
        position = get("playback_position")
        duration = get("playback_duration")
        updated = get("playback_updated_at")
        episode_end = get("media_episode_end")
        boundary = get("part_boundary")
        return LibraryEpisode(
            media_id=int(media_id) if media_id is not None else None,
            title=str(row["title"]),
            episode=media_episode,
            media_episode=media_episode,
            release_episode=release_episode,
            video_path=Path(str(row["video_path"])),
            subtitle_path=Path(str(subtitle_path)) if subtitle_path else None,
            embedded_subtitle_id=int(embedded_subtitle_id) if embedded_subtitle_id is not None else None,
            subtitle_origin=str(get("subtitle_origin") or ""),
            state=str(row["state"]),
            torrent_hash=str(get("torrent_hash") or ""),
            watched_at=float(watched_at) if watched_at else None,
            delete_after=float(delete_after) if delete_after else None,
            playback_position=float(position) if position is not None else None,
            playback_duration=float(duration) if duration is not None else None,
            playback_updated_at=float(updated) if updated is not None else None,
            playback_active_seconds=float(get("playback_active_seconds") or 0.0),
            media_episode_end=int(episode_end) if episode_end is not None else None,
            part_boundary=float(boundary) if boundary is not None else None,
        )

    def record_playback(
        self,
        video_path: Path,
        position: float,
        duration: float,
        active_seconds: float = 0.0,
    ) -> None:
        position = max(0.0, float(position))
        duration = max(0.0, float(duration))
        active_seconds = max(0.0, float(active_seconds))
        now = time.time()
        finished = duration > 0 and position >= duration * 0.95
        with self.connect() as conn:
            before = conn.execute(
                "SELECT state,playback_position,playback_duration FROM episodes WHERE video_path=?",
                (str(video_path),),
            ).fetchone()
            if finished:
                conn.execute(
                    "UPDATE episodes SET playback_position=NULL,playback_duration=?,"
                    "playback_updated_at=NULL,playback_active_seconds=playback_active_seconds+?,updated_at=? WHERE video_path=?",
                    (duration, active_seconds, now, str(video_path)),
                )
            else:
                conn.execute(
                    "UPDATE episodes SET playback_position=?,playback_duration=?,"
                    "playback_updated_at=?,playback_active_seconds=playback_active_seconds+?,updated_at=? WHERE video_path=?",
                    (position, duration or None, now, active_seconds, now, str(video_path)),
                )
            # Heartbeats leave the UI version alone, except when the episode enters or
            # leaves Continue Watching: a cached Home snapshot would otherwise keep
            # showing it under New episodes ready while a fresh one shows it resumable.
            if before is not None and _resumable(
                before["state"], before["playback_position"], before["playback_duration"]
            ) != _resumable(before["state"], None if finished else position, duration or None):
                conn.execute(_UI_STATE_BUMP_SQL)

    def playback_evidence(self, video_path: Path) -> dict[str, float]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT playback_position,playback_duration,playback_active_seconds "
                "FROM episodes WHERE video_path=?",
                (str(video_path),),
            ).fetchone()
        if row is None:
            return {"position": 0.0, "duration": 0.0, "active_seconds": 0.0}
        return {
            "position": float(row["playback_position"] or 0.0),
            "duration": float(row["playback_duration"] or 0.0),
            "active_seconds": float(row["playback_active_seconds"] or 0.0),
        }

    def reset_anime_progress(self, media_id: int) -> int:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                "UPDATE anime SET progress=0,status=CASE WHEN status IN ('COMPLETED','REPEATING') THEN 'CURRENT' ELSE status END,updated_at=? WHERE media_id=?",
                (now, int(media_id)),
            )
            self.note_anilist_progress(int(media_id), 0, now=now, conn=conn)
            cursor = conn.execute(
                """
                UPDATE episodes
                SET state=CASE
                        WHEN state='watched' OR watched_at IS NOT NULL THEN
                            CASE
                                WHEN subtitle_path IS NOT NULL OR embedded_subtitle_id IS NOT NULL THEN 'ready'
                                ELSE 'local'
                            END
                        ELSE state
                    END,
                    watched_at=NULL,delete_after=NULL,
                    playback_position=NULL,playback_duration=NULL,playback_updated_at=NULL,
                    playback_active_seconds=0,updated_at=?
                WHERE media_id=? AND (
                    state='watched' OR watched_at IS NOT NULL OR delete_after IS NOT NULL
                    OR playback_position IS NOT NULL OR playback_duration IS NOT NULL
                    OR playback_updated_at IS NOT NULL OR playback_active_seconds>0
                )
                """,
                (now, int(media_id)),
            )
        return max(0, int(cursor.rowcount or 0))

    def resumable_episodes(
        self,
        *,
        min_position: float = RESUME_MIN_POSITION,
        min_remaining: float = RESUME_MIN_REMAINING,
        limit: int = 12,
    ) -> list[LibraryEpisode]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM episodes
                WHERE state!='watched'
                  AND playback_position IS NOT NULL
                  AND playback_position>=?
                  AND (playback_duration IS NULL OR playback_duration-playback_position>=?)
                ORDER BY playback_updated_at DESC
                LIMIT ?
                """,
                (float(min_position), float(min_remaining), max(1, int(limit))),
            ).fetchall()
        return [self._episode_from_row(row) for row in rows]

    def has_episode(self, media_id: int, episode: int) -> bool:
        """True when a file or a download already covers the episode.

        A two-episode file ("01-02") covers both numbers (audit 17А).
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM episodes WHERE media_id=? AND ? BETWEEN COALESCE(media_episode,episode) "
                "AND COALESCE(media_episode_end,media_episode,episode) LIMIT 1",
                (media_id, episode),
            ).fetchone()
            if row:
                return True
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE media_id=? AND ? BETWEEN COALESCE(media_episode,episode) "
                "AND COALESCE(media_episode_end,media_episode,episode) AND state NOT IN ('error','missing') LIMIT 1",
                (media_id, episode),
            ).fetchone()
        return bool(row)

    def set_part_boundary(self, video_path: Path, seconds: float | None) -> bool:
        """Where the second episode of a two-episode file starts (from subtitle preparation)."""
        value = None if seconds is None else max(0.0, float(seconds))
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE episodes SET part_boundary=? WHERE video_path=? AND media_episode_end IS NOT NULL",
                (value, str(video_path)),
            )
        return bool(cursor.rowcount)

    def episodes_watched_since(self, since: float) -> dict[int, int]:
        """media id -> the highest episode marked watched here at or after ``since``.

        An AniList list fetched before such a watch cannot contain it yet: a
        library sync that started earlier must not take that progress back.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT media_id,MAX(COALESCE(media_episode_end,media_episode,episode)) AS number "
                "FROM episodes WHERE media_id IS NOT NULL AND watched_at IS NOT NULL AND watched_at>=? "
                "GROUP BY media_id",
                (float(since),),
            ).fetchall()
        return {int(row["media_id"]): int(row["number"]) for row in rows if row["number"]}

    _PROGRESS_MARKS_KEY = "anilist_progress_marks_v1:{media_id}"

    def note_anilist_progress(
        self,
        media_id: int,
        progress: int,
        *,
        now: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[list[float]]:
        """Remember when the list progress first reached each value (``[[progress, at], ...]``).

        Cleanup marks a local episode watched from AniList progress only when the progress
        reached it after the file was downloaded: a title re-downloaded for a rewatch is
        already counted, and its new files stay until they are watched. A lowered progress
        (a rewatch reset) forgets the marks above it. The first sight of a title is its
        first mark (files from before were downloaded before it).
        """
        key = self._PROGRESS_MARKS_KEY.format(media_id=int(media_id))
        progress = max(0, int(progress or 0))
        stamp = time.time() if now is None else float(now)
        with nullcontext(conn) if conn is not None else self.connect() as active:
            row = active.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            try:
                stored = json.loads(str(row["value"])) if row is not None else []
            except (TypeError, ValueError):
                stored = []
            marks = [
                [int(item[0]), float(item[1])]
                for item in stored
                if isinstance(item, list) and len(item) == 2 and int(item[0]) <= progress
            ]
            if not marks or marks[-1][0] < progress:
                marks.append([progress, stamp])
            if marks != stored:
                active.execute(
                    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, json.dumps(marks), stamp),
                )
        return marks

    @staticmethod
    def anilist_progress_reached_at(marks: list[list[float]], episode: int) -> float | None:
        """When the list progress first covered ``episode`` (None: it does not)."""
        return next((float(at) for value, at in marks if int(value) >= int(episode)), None)

    def raise_anime_progress(self, media_id: int, progress: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE anime SET progress=MAX(progress,?),updated_at=? WHERE media_id=?",
                (int(progress), time.time(), int(media_id)),
            )

    @staticmethod
    def _subtitle_job_owner_matches(
        conn: sqlite3.Connection,
        video_path: Path,
        generation: int | None,
        owner_token: str | None,
    ) -> bool:
        if generation is None:
            return True
        row = conn.execute(
            "SELECT generation,owner_token FROM subtitle_jobs WHERE video_path=?",
            (str(video_path),),
        ).fetchone()
        return bool(
            row is not None
            and int(row["generation"] or 0) == int(generation)
            and str(row["owner_token"] or "") == str(owner_token or "")
        )

    def subtitle_job_owned(self, video_path: Path, generation: int, owner_token: str) -> bool:
        with self.connect() as conn:
            return self._subtitle_job_owner_matches(conn, video_path, int(generation), str(owner_token))

    @classmethod
    def _lock_subtitle_job_owner(
        cls,
        conn: sqlite3.Connection,
        video_path: Path,
        generation: int | None,
        owner_token: str | None,
    ) -> bool:
        """Serialize an owner check with the mutation that follows it."""
        if generation is not None:
            conn.execute("BEGIN IMMEDIATE")
        return cls._subtitle_job_owner_matches(conn, video_path, generation, owner_token)

    def set_subtitle_ready(
        self,
        video_path: Path,
        subtitle_path: Path | None,
        embedded_subtitle_id: int | None = None,
        *,
        origin: str = "",
        generation: int | None = None,
        owner_token: str | None = None,
        clear_state_keys: Iterable[str] = (),
    ) -> bool:
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            previous = conn.execute(
                "SELECT state FROM episodes WHERE video_path=?", (str(video_path),)
            ).fetchone()
            now = time.time()
            resolved = self._transition_episode(conn, video_path, "ready", trigger="subtitle_ready")
            conn.execute(
                "UPDATE episodes SET subtitle_path=?,embedded_subtitle_id=?,subtitle_origin=?,"
                "state=?,updated_at=? WHERE video_path=?",
                (
                    str(subtitle_path) if subtitle_path else None,
                    embedded_subtitle_id,
                    str(origin or ""),
                    resolved,
                    now,
                    str(video_path),
                ),
            )
            for key in clear_state_keys:
                if str(key):
                    conn.execute("DELETE FROM state WHERE key=?", (str(key),))
            if generation is None:
                conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            else:
                conn.execute(
                    "DELETE FROM subtitle_jobs WHERE video_path=? AND generation=? AND owner_token=?",
                    (str(video_path), int(generation), str(owner_token or "")),
                )
            if previous is not None and str(previous["state"] or "") != resolved and resolved == "ready":
                version = str(time.time_ns())
                conn.execute(
                    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("ready_state_version", version, now),
                )
            return True

    def set_waiting_text_subtitles(
        self,
        video_path: Path,
        subtitle_path: Path | None = None,
        embedded_subtitle_id: int | None = None,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Keep a bitmap fallback for Library-only playback and retry text subs."""
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            resolved = self._transition_episode(
                conn, video_path, "waiting_text_subtitles", trigger="bitmap_detected"
            )
            conn.execute(
                "UPDATE episodes SET subtitle_path=?,embedded_subtitle_id=?,subtitle_origin='bitmap',"
                "state=?,updated_at=? WHERE video_path=?",
                (
                    str(subtitle_path) if subtitle_path else None,
                    embedded_subtitle_id,
                    resolved,
                    time.time(),
                    str(video_path),
                ),
            )
            return True

    def set_ocr_fallback_not_ready(
        self,
        video_path: Path,
        subtitle_path: Path | None,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Keep a cached OCR SRT playable without treating it as verified Ready."""
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            resolved = self._transition_episode(
                conn, video_path, "waiting_text_subtitles", trigger="ocr_fallback"
            )
            conn.execute(
                "UPDATE episodes SET subtitle_path=?,embedded_subtitle_id=NULL,subtitle_origin='ocr',"
                "state=?,updated_at=? WHERE video_path=?",
                (
                    str(subtitle_path) if subtitle_path else None,
                    resolved,
                    time.time(),
                    str(video_path),
                ),
            )
            return True

    def set_couldnt_sync_subtitle(
        self,
        video_path: Path,
        subtitle_path: Path,
        *,
        origin: str = "",
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Retain a confident raw text candidate while background sync retries."""
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            resolved = self._transition_episode(
                conn, video_path, "couldnt_sync", trigger="subtitle_sync_failed"
            )
            conn.execute(
                "UPDATE episodes SET subtitle_path=?,embedded_subtitle_id=NULL,subtitle_origin=?,"
                "state=?,updated_at=? WHERE video_path=?",
                (
                    str(subtitle_path),
                    str(origin or "raw_unsynced"),
                    resolved,
                    time.time(),
                    str(video_path),
                ),
            )
            return True

    def restore_ready_selection_for_upgrade(
        self,
        video_path: Path,
        subtitle_path: Path | None,
        embedded_subtitle_id: int | None,
        *,
        origin: str = "",
    ) -> bool:
        """Restore the old Ready selection while an upgrade check is pending.

        Unlike ``set_subtitle_ready`` this intentionally keeps the subtitle job:
        upgrade checks are observational and must never make a ready episode
        disappear while a better candidate is being evaluated.
        """
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM episodes WHERE video_path=?", (str(video_path),)).fetchone()
            if row is None:
                return False
            current = str(row["state"] or "local")
            resolved = transition_episode_state(current, "ready", trigger="subtitle_upgrade_restore")
            conn.execute(
                "UPDATE episodes SET subtitle_path=?,embedded_subtitle_id=?,subtitle_origin=?,"
                "state=?,updated_at=? WHERE video_path=?",
                (
                    str(subtitle_path) if subtitle_path else None,
                    embedded_subtitle_id,
                    str(origin or ""),
                    resolved,
                    time.time(),
                    str(video_path),
                ),
            )
            return resolved == "ready"

    def clear_subtitle_selection(
        self,
        video_path: Path,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Drop a stale prepared subtitle while keeping the retry job intact."""
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            resolved = self._transition_episode(
                conn, video_path, "waiting_subtitles", trigger="subtitle_invalidated"
            )
            conn.execute(
                "UPDATE episodes SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',"
                "state=?,updated_at=? WHERE video_path=?",
                (resolved, time.time(), str(video_path)),
            )
            return True

    def repair_bitmap_ready_rows(self) -> int:
        """Move legacy image-subtitle rows back to text-subtitle preparation."""
        now = time.time()
        repaired: list[tuple[str, int | None, int | None]] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT video_path,subtitle_path,subtitle_origin,media_id,episode FROM episodes "
                "WHERE state='ready' AND (subtitle_path IS NOT NULL OR subtitle_origin='bitmap')"
            ).fetchall()
            for row in rows:
                subtitle_raw = str(row["subtitle_path"] or "").strip()
                subtitle_path = Path(subtitle_raw) if subtitle_raw else None
                is_bitmap = str(row["subtitle_origin"] or "").casefold() == "bitmap"
                if subtitle_path is not None:
                    is_bitmap = is_bitmap or subtitle_path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
                if not is_bitmap:
                    continue
                video_path = str(row["video_path"])
                media_id = int(row["media_id"]) if row["media_id"] is not None else None
                episode = int(row["episode"]) if row["episode"] is not None else None
                conn.execute(
                    "UPDATE episodes SET state='waiting_text_subtitles',subtitle_origin='bitmap',updated_at=? "
                    "WHERE video_path=?",
                    (now, video_path),
                )
                conn.execute(
                    """
                    INSERT INTO subtitle_jobs(
                        video_path,media_id,episode,state,attempts,next_check,last_error,updated_at
                    ) VALUES(?,?,?,'pending',0,?,?,?)
                    ON CONFLICT(video_path) DO UPDATE SET
                        media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                        episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                        state='pending',next_check=excluded.next_check,
                        last_error=excluded.last_error,updated_at=excluded.updated_at
                    """,
                    (video_path, media_id, episode, now, err("jobs.waitingTextSubtitles"), now),
                )
                repaired.append((video_path, media_id, episode))
        return len(repaired)

    def repair_spurious_ready_subtitle_jobs(self, *, preserve_paths: set[str] | None = None) -> int:
        """Remove resolver jobs accidentally attached to valid ready rows.

        Versions through 0.5.79 could preserve a prepared subtitle during a
        library upsert but still enqueue a new job from the temporary scan
        result. The next maintenance pass then treated the valid selection as
        stale. Prefer the existing ready/watched selection when its external
        file still exists (or it references an embedded track).
        """
        removable: list[str] = []
        preserve = {str(path) for path in (preserve_paths or set())}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.video_path,e.subtitle_path,e.embedded_subtitle_id
                FROM episodes e
                JOIN subtitle_jobs j ON j.video_path=e.video_path
                WHERE e.state IN ('ready','watched')
                  AND (e.subtitle_path IS NOT NULL OR e.embedded_subtitle_id IS NOT NULL)
                """
            ).fetchall()
            for row in rows:
                video_path = str(row["video_path"])
                if video_path in preserve:
                    continue
                subtitle = str(row["subtitle_path"] or "").strip()
                valid_external = False
                if subtitle:
                    path = Path(subtitle)
                    try:
                        valid_external = path.is_file() and path.stat().st_size > 0
                    except OSError:
                        valid_external = False
                if valid_external or row["embedded_subtitle_id"] is not None:
                    removable.append(str(row["video_path"]))
            if removable:
                conn.executemany(
                    "DELETE FROM subtitle_jobs WHERE video_path=?",
                    ((video_path,) for video_path in removable),
                )
        return len(removable)

    def repair_stale_subtitle_selections(self, *, preserve_paths: set[str] | None = None) -> int:
        """Clear subtitle paths that are not currently considered validated.

        Upgrade checks are explicitly excluded: their current selection must
        remain visible until the candidate is accepted or rejected.
        """
        preserve = {str(path) for path in (preserve_paths or set())}
        now = time.time()
        changed = 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.video_path,e.state,e.subtitle_path,e.embedded_subtitle_id,
                       e.media_id,e.episode,
                       EXISTS(SELECT 1 FROM subtitle_jobs j WHERE j.video_path=e.video_path) AS has_job
                FROM episodes e
                WHERE e.subtitle_path IS NOT NULL OR e.embedded_subtitle_id IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                video_path = str(row["video_path"])
                state = str(row["state"] or "local")
                has_job = bool(row["has_job"])
                invalid_state = state not in {"ready", "watched", "waiting_text_subtitles", "couldnt_sync"}
                invalid_job = (
                    has_job
                    and state not in {"waiting_text_subtitles", "couldnt_sync"}
                    and video_path not in preserve
                )
                missing_ready_external = False
                subtitle_raw = str(row["subtitle_path"] or "").strip()
                if state == "ready" and subtitle_raw and row["embedded_subtitle_id"] is None:
                    try:
                        subtitle_file = Path(subtitle_raw)
                        missing_ready_external = not (
                            subtitle_file.is_file() and subtitle_file.stat().st_size > 0
                        )
                    except OSError:
                        missing_ready_external = True
                if not (invalid_state or invalid_job or missing_ready_external):
                    continue
                conn.execute(
                    "UPDATE episodes SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',"
                    f"state={_WAITING_UNLESS_WATCHED_SQL},updated_at=? WHERE video_path=?",
                    (now, video_path),
                )
                if missing_ready_external:
                    resolved_video = str(Path(video_path).expanduser().resolve())
                    digest = hashlib.sha1(resolved_video.encode("utf-8")).hexdigest()
                    candidate_fingerprint_key = "subtitle_candidate_set:" + digest
                    force_rebuild_key = "subtitle_force_rebuild:" + digest
                    conn.execute("DELETE FROM state WHERE key=?", (candidate_fingerprint_key,))
                    conn.execute(
                        "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (force_rebuild_key, "1", now),
                    )
                    conn.execute(
                        """
                        INSERT INTO subtitle_jobs(
                            video_path,media_id,episode,state,attempts,priority,next_check,last_error,updated_at
                        ) VALUES(?,?,?,'pending',0,260,?,?,?)
                        ON CONFLICT(video_path) DO UPDATE SET
                            media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                            episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                            state='pending',stage='queued',attempts=0,
                            priority=MAX(subtitle_jobs.priority,260),
                            next_check=excluded.next_check,lease_until=0,heartbeat_at=0,
                            progress_json='{}',action_code='',
                            last_error=excluded.last_error,updated_at=excluded.updated_at
                        """,
                        (
                            video_path,
                            row["media_id"],
                            row["episode"],
                            now,
                            err("jobs.subtitleCacheMissing"),
                            now,
                        ),
                    )
                changed += 1
        return changed

    def invalidate_subtitle(
        self,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
        error: str,
        *,
        keep_selection: bool = False,
        priority: int = 0,
    ) -> None:
        """Prepare the subtitles again from scratch (new job generation, forced rebuild).

        ``keep_selection`` leaves the current subtitles playable: the caller has stored
        an upgrade request that replaces them with the result or puts them back.
        """
        now = time.time()
        resolved_video = Path(video_path).expanduser().resolve()
        digest = hashlib.sha1(str(resolved_video).encode("utf-8")).hexdigest()
        candidate_fingerprint_key = "subtitle_candidate_set:" + digest
        force_rebuild_key = "subtitle_force_rebuild:" + digest
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, resolved_video.stem)
            # Invalidation means the current selection must be evaluated again,
            # even when provider discovery returns the exact same candidates.
            # Keeping the old fingerprint makes prepare-only skip the expensive
            # alignment forever and strands a previously working episode in
            # waiting_subtitles.
            conn.execute("DELETE FROM state WHERE key=?", (candidate_fingerprint_key,))
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (force_rebuild_key, "1", now),
            )
            if not keep_selection:
                conn.execute(
                    "UPDATE episodes SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',"
                    f"state={_WAITING_UNLESS_WATCHED_SQL},updated_at=? "
                    "WHERE video_path=?",
                    (now, str(resolved_video)),
                )
            conn.execute(
                """
                INSERT INTO subtitle_jobs(
                    video_path,media_id,episode,state,attempts,priority,next_check,last_error,updated_at
                ) VALUES(?,?,?,'pending',0,?,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                    episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                    state='pending',stage='queued',attempts=0,next_check=excluded.next_check,
                    priority=MAX(subtitle_jobs.priority,excluded.priority),
                    lease_until=0,heartbeat_at=0,progress_json='{}',action_code='',
                    generation=subtitle_jobs.generation+1,owner_token='',
                    last_error=excluded.last_error,updated_at=excluded.updated_at
                """,
                (str(resolved_video), media_id, episode, max(0, int(priority)), now, error[-1000:], now),
            )

    def queue_subtitle_job(
        self,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
        *,
        delay_seconds: float = 0,
        error: str = "",
        priority: int = 0,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, video_path.stem)
            conn.execute(
                """
                INSERT INTO subtitle_jobs(
                    video_path,media_id,episode,state,attempts,priority,next_check,last_error,updated_at
                )
                VALUES(?,?,?,'pending',0,?,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                    episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                    state='pending',stage='queued',next_check=excluded.next_check,
                    lease_until=0,heartbeat_at=0,progress_json='{}',action_code='',
                    priority=MAX(subtitle_jobs.priority,excluded.priority),
                    last_error=excluded.last_error,updated_at=excluded.updated_at
                """,
                (
                    str(video_path),
                    media_id,
                    episode,
                    max(0, int(priority)),
                    now + delay_seconds,
                    error,
                    now,
                ),
            )

    def note_subtitle_job_error(self, video_path: Path, error: str) -> bool:
        """Show why a job's preparation stopped (its card and diagnostics); state unchanged.

        last_error is a UI column: the update bumps ui_state_version through the trigger.
        """
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE subtitle_jobs SET last_error=?,updated_at=? WHERE video_path=?",
                (str(error), time.time(), str(video_path)),
            )
            return bool(cursor.rowcount)

    def delete_subtitle_job(
        self,
        video_path: Path,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        with self.connect() as conn:
            if generation is None:
                cursor = conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            else:
                cursor = conn.execute(
                    "DELETE FROM subtitle_jobs WHERE video_path=? AND generation=? AND owner_token=?",
                    (str(video_path), int(generation), str(owner_token or "")),
                )
            return bool(cursor.rowcount)

    def ensure_subtitle_job(
        self,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
    ) -> bool:
        """Create a missing resolver job without changing an existing backoff."""
        now = time.time()
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, video_path.stem)
            cursor = conn.execute(
                """
                INSERT INTO subtitle_jobs(
                    video_path,media_id,episode,state,attempts,next_check,last_error,updated_at
                ) VALUES(?,?,?,'pending',0,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                    episode=CASE
                        WHEN excluded.media_id IS NOT NULL THEN excluded.episode
                        ELSE COALESCE(excluded.episode,subtitle_jobs.episode)
                    END,
                    updated_at=excluded.updated_at
                """,
                (str(video_path), media_id, episode, now, err("jobs.waitingSubtitles"), now),
            )
        return bool(cursor.rowcount)

    def force_requeue_unresolved_subtitle_jobs(
        self,
        *,
        priority: int = 20,
        recover_processing: bool = True,
        keep_action_codes: Iterable[str] = REFRESH_KEEPS_ACTION_CODES,
    ) -> int:
        """Immediately retry every existing or reconstructable subtitle job.

        A blocking/manual maintenance pass may recover rows left in ``processing``
        by a killed process: only rows whose lease has run out, a live lease belongs
        to a worker (the agent, another window) that is preparing it right now.
        An *interactive* Refresh recovers none.  Live rows only get the priority,
        avoiding duplicate preparation while still making the UI track the
        requested check.  Jobs waiting for the person's answer
        (``keep_action_codes``) stay as they are.
        """
        now = time.time()
        priority = max(0, min(1000, int(priority)))
        affected_paths: set[str] = set()
        keep_codes = tuple(str(code) for code in keep_action_codes) or ("",)
        kept_codes = ",".join("?" for _ in keep_codes)
        upsert_guard = (
            "subtitle_jobs.state<>'processing' AND NOT (subtitle_jobs.state='needs_action' "
            f"AND subtitle_jobs.action_code IN ({kept_codes}))"
        )
        with self.connect() as conn:
            kept = {
                str(row["video_path"])
                for row in conn.execute(
                    "SELECT video_path FROM subtitle_jobs "
                    f"WHERE state='needs_action' AND action_code IN ({kept_codes})",
                    keep_codes,
                ).fetchall()
            }
            existing = conn.execute(
                "SELECT video_path FROM subtitle_jobs WHERE state IN ('pending','processing','needs_action')"
            ).fetchall()
            for row in existing:
                if str(row["video_path"]) not in kept:
                    affected_paths.add(str(row["video_path"]))
            conn.execute(
                "UPDATE subtitle_jobs SET state='pending',stage='queued',action_code='',next_check=?,"
                "priority=MAX(priority,?),last_error=?,progress_json='{}',updated_at=? "
                "WHERE state='pending'",
                (now, priority, err("jobs.manualRefresh"), now),
            )
            if recover_processing:
                conn.execute(
                    "UPDATE subtitle_jobs SET state='pending',stage='queued',action_code='',next_check=?,"
                    "priority=MAX(priority,?),last_error=?,progress_json='{}',updated_at=? "
                    "WHERE state='processing' AND lease_until<?",
                    (now, priority, err("jobs.manualRefresh"), now, now),
                )
            conn.execute(
                "UPDATE subtitle_jobs SET priority=MAX(priority,?),"
                "last_error=?,updated_at=? "
                "WHERE state='processing'",
                (priority, err("jobs.manualRefreshChecking"), now),
            )

            rows = conn.execute(
                """
                SELECT video_path,media_id,episode
                FROM episodes
                WHERE state IN ('local','waiting_subtitles','waiting_text_subtitles','couldnt_sync')
                  AND (
                    state IN ('waiting_text_subtitles','couldnt_sync')
                    OR (subtitle_path IS NULL AND embedded_subtitle_id IS NULL)
                  )
                """
            ).fetchall()
            for row in rows:
                video_path = str(row["video_path"])
                if video_path in kept:
                    continue
                affected_paths.add(video_path)
                # A processing row was handled above: recovered when its lease ran out,
                # otherwise its worker keeps it.
                conn.execute(
                    f"""
                    INSERT INTO subtitle_jobs(
                        video_path,media_id,episode,state,attempts,priority,next_check,last_error,updated_at
                    ) VALUES(?,?,?,'pending',0,?,?,?,?)
                    ON CONFLICT(video_path) DO UPDATE SET
                        media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                        episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                        state='pending',stage='queued',action_code='',next_check=excluded.next_check,
                        priority=MAX(subtitle_jobs.priority,excluded.priority),progress_json='{{}}',
                        last_error=excluded.last_error,updated_at=excluded.updated_at
                    WHERE {upsert_guard}
                    """,
                    (
                        video_path,
                        int(row["media_id"]) if row["media_id"] is not None else None,
                        int(row["episode"]) if row["episode"] is not None else None,
                        priority,
                        now,
                        err("jobs.manualRefresh"),
                        now,
                        *keep_codes,
                    ),
                )
        return len(affected_paths)

    @staticmethod
    def _subtitle_job_order_sql(alias: str = "j") -> str:
        """Prioritize the watchable frontier of every anime before middle episodes.

        Timestamp-only ordering made a freshly discovered batch effectively random
        from the viewer's perspective: episode 8 could be repaired before episode 1.
        A job is a frontier when it is either AniList's next unwatched episode or
        the earliest unresolved subtitle job for that title.  All title frontiers
        are processed before any middle episode; normal priority/backoff then break
        ties inside the same frontier tier.
        """
        return f"""
            CASE
              WHEN {alias}.media_id IS NOT NULL AND {alias}.episode IS NOT NULL
               AND (
                    {alias}.episode=COALESCE(a.progress,0)+1
                    OR {alias}.episode=(
                        SELECT MIN(j2.episode) FROM subtitle_jobs j2
                        WHERE j2.media_id={alias}.media_id
                          AND j2.episode IS NOT NULL
                          AND j2.state IN ('pending','processing')
                    )
               ) THEN 0
              ELSE 1
            END,
            {alias}.priority DESC,
            CASE
              WHEN {alias}.episode IS NOT NULL
              THEN ABS({alias}.episode-(COALESCE(a.progress,0)+1))
              ELSE 2147483647
            END,
            {alias}.next_check,
            {alias}.updated_at DESC
        """

    def due_subtitle_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT j.* FROM subtitle_jobs j LEFT JOIN anime a ON a.media_id=j.media_id "
                "WHERE j.state='pending' AND j.next_check<=? ORDER BY "
                + self._subtitle_job_order_sql("j")
                + " LIMIT ?",
                (time.time(), limit),
            ).fetchall()

    def claim_due_subtitle_jobs(
        self,
        limit: int = 10,
        *,
        lease_seconds: float = 25 * 60,
    ) -> list[sqlite3.Row]:
        """Atomically claim pending jobs and stamp this worker as the owner."""
        now = time.time()
        owner_token = secrets.token_hex(16)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT j.* FROM subtitle_jobs j LEFT JOIN anime a ON a.media_id=j.media_id "
                "WHERE (j.state='pending' AND j.next_check<=?) "
                "OR (j.state='processing' AND j.next_check<=?) ORDER BY "
                + self._subtitle_job_order_sql("j")
                + " LIMIT ?",
                (now, now, limit),
            ).fetchall()
            if not rows:
                return []
            paths = [str(row["video_path"]) for row in rows]
            placeholders = ",".join("?" for _ in paths)
            conn.execute(
                f"UPDATE subtitle_jobs SET state='processing',stage='discovering',"
                f"next_check=?,lease_until=?,heartbeat_at=?,action_code='',owner_token=?,updated_at=? "
                f"WHERE video_path IN ({placeholders})",
                (now + lease_seconds, now + lease_seconds, now, owner_token, now, *paths),
            )
            refreshed = conn.execute(
                f"SELECT * FROM subtitle_jobs WHERE video_path IN ({placeholders})",
                tuple(paths),
            ).fetchall()
            by_path = {str(row["video_path"]): row for row in refreshed}
            return [by_path[path] for path in paths if path in by_path]

    def claim_subtitle_jobs_for_paths(
        self,
        video_paths: list[Path] | tuple[Path, ...],
        *,
        limit: int = 10,
        lease_seconds: float = 25 * 60,
    ) -> list[sqlite3.Row]:
        """Claim due jobs only for supplied paths and stamp this worker as owner."""
        paths = list(dict.fromkeys(str(Path(path)) for path in video_paths if str(path)))
        if not paths or limit <= 0:
            return []
        now = time.time()
        owner_token = secrets.token_hex(16)
        placeholders = ",".join("?" for _ in paths)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT j.* FROM subtitle_jobs j LEFT JOIN anime a ON a.media_id=j.media_id "
                f"WHERE j.video_path IN ({placeholders}) "
                "AND ((j.state='pending' AND j.next_check<=?) "
                "OR (j.state='processing' AND j.next_check<=?)) ORDER BY "
                + self._subtitle_job_order_sql("j")
                + " LIMIT ?",
                (*paths, now, now, max(1, int(limit))),
            ).fetchall()
            if not rows:
                return []
            claimed = [str(row["video_path"]) for row in rows]
            claimed_placeholders = ",".join("?" for _ in claimed)
            conn.execute(
                f"UPDATE subtitle_jobs SET state='processing',stage='discovering',"
                f"next_check=?,lease_until=?,heartbeat_at=?,action_code='',owner_token=?,updated_at=? "
                f"WHERE video_path IN ({claimed_placeholders})",
                (now + lease_seconds, now + lease_seconds, now, owner_token, now, *claimed),
            )
            refreshed = conn.execute(
                f"SELECT * FROM subtitle_jobs WHERE video_path IN ({claimed_placeholders})",
                tuple(claimed),
            ).fetchall()
            by_path = {str(row["video_path"]): row for row in refreshed}
            return [by_path[path] for path in claimed if path in by_path]

    def defer_subtitle_job(
        self,
        video_path: Path,
        error: str,
        delay_seconds: float,
        *,
        stage: str = "retry_scheduled",
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Return a claimed job to pending without counting a media/preparation failure."""
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute(
                """
                UPDATE subtitle_jobs SET state='pending',stage=?,priority=0,
                lease_until=0,next_check=?,last_error=?,updated_at=?
                WHERE video_path=?
                """,
                (stage, now + delay_seconds, error[-1000:], now, str(video_path)),
            )
            conn.execute(
                f"UPDATE episodes SET state={_WAITING_UNLESS_SETTLED_SQL},updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )
            return True

    def reset_subtitle_job_attempts(
        self, video_path: Path, *, generation: int | None = None, owner_token: str | None = None
    ) -> bool:
        """Reset failure backoff after the discoverable candidate set changes."""
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            cursor = conn.execute(
                "UPDATE subtitle_jobs SET attempts=0,updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )
            return bool(cursor.rowcount)

    def postpone_subtitle_job(
        self,
        video_path: Path,
        error: str,
        delay_seconds: float,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute(
                """
                UPDATE subtitle_jobs SET attempts=attempts+1,state='pending',stage='retry_scheduled',priority=0,
                lease_until=0,next_check=?,last_error=?,updated_at=?
                WHERE video_path=?
                """,
                (now + delay_seconds, error[-1000:], now, str(video_path)),
            )
            conn.execute(
                f"UPDATE episodes SET state={_WAITING_UNLESS_SETTLED_SQL},updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )
            return True

    def postpone_bitmap_ocr_job(
        self,
        video_path: Path,
        error: str,
        delay_seconds: float,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Retry bitmap OCR without discarding the already prepared PGS source."""
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute(
                """
                UPDATE subtitle_jobs SET attempts=attempts+1,stage='retry_scheduled',priority=0,
                state='pending',lease_until=0,next_check=?,last_error=?,updated_at=?
                WHERE video_path=?
                """,
                (now + delay_seconds, error[-1000:], now, str(video_path)),
            )
            conn.execute(
                "UPDATE episodes SET state='waiting_text_subtitles',subtitle_origin='bitmap',updated_at=? "
                "WHERE video_path=?",
                (now, str(video_path)),
            )
            return True

    def update_subtitle_job_stage(
        self,
        video_path: Path,
        stage: str,
        *,
        progress: dict[str, object] | None = None,
        lease_seconds: float = 25 * 60,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Persist worker progress and renew its processing lease."""
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            cursor = conn.execute(
                """
                UPDATE subtitle_jobs
                SET state='processing',stage=?,heartbeat_at=?,lease_until=?,next_check=?,
                    progress_json=?,updated_at=?
                WHERE video_path=?
                """,
                (
                    str(stage),
                    now,
                    now + max(30.0, float(lease_seconds)),
                    now + max(30.0, float(lease_seconds)),
                    json.dumps(progress or {}, ensure_ascii=False),
                    now,
                    str(video_path),
                ),
            )
            return bool(cursor.rowcount)

    def mark_subtitle_job_needs_action(
        self,
        video_path: Path,
        error: str,
        action_code: str,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            conn.execute(
                """
                UPDATE subtitle_jobs
                SET state='needs_action',stage='needs_action',attempts=attempts+1,
                    priority=0,lease_until=0,next_check=0,last_error=?,action_code=?,updated_at=?
                WHERE video_path=?
                """,
                (error[-1000:], str(action_code), now, str(video_path)),
            )
            conn.execute(
                f"UPDATE episodes SET state={_WAITING_UNLESS_SETTLED_SQL},updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )
            return True

    def priority_subtitle_job_count(self, *, min_priority: int = 200) -> int:
        """Count manual/high-priority subtitle jobs still awaiting an attempt."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM subtitle_jobs "
                "WHERE state IN ('pending','processing') AND priority>=?",
                (max(0, int(min_priority)),),
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def reschedule_subtitle_job(
        self,
        video_path: Path,
        error: str,
        delay_seconds: float,
        *,
        count_attempt: bool = False,
        keep_priority: bool | None = None,
        action_code: str = "",
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Return a claimed job to pending (or needs_action) and leave its episode alone.

        For runs without a verdict on the subtitles (paused, timed out, a network
        error) and for jobs that were never started: the episode keeps its state and
        its playable fallback (OCR, raw unsynced, the bitmap source). Only a real
        attempt counts; a job nobody tried keeps its priority.
        """
        if keep_priority is None:
            keep_priority = not count_attempt
        now = time.time()
        with self.connect() as conn:
            if not self._lock_subtitle_job_owner(conn, video_path, generation, owner_token):
                return False
            if action_code:
                conn.execute(
                    """
                    UPDATE subtitle_jobs
                    SET state='needs_action',stage='needs_action',attempts=attempts+?,priority=0,
                        lease_until=0,next_check=0,last_error=?,action_code=?,progress_json='{}',updated_at=?
                    WHERE video_path=?
                    """,
                    (1 if count_attempt else 0, error[-1000:], str(action_code), now, str(video_path)),
                )
            else:
                conn.execute(
                    """
                    UPDATE subtitle_jobs
                    SET state='pending',stage='retry_scheduled',attempts=attempts+?,
                        priority=CASE WHEN ? THEN priority ELSE 0 END,
                        lease_until=0,next_check=?,last_error=?,progress_json='{}',updated_at=?
                    WHERE video_path=?
                    """,
                    (
                        1 if count_attempt else 0,
                        1 if keep_priority else 0,
                        now + max(0.0, float(delay_seconds)),
                        error[-1000:],
                        now,
                        str(video_path),
                    ),
                )
            return True

    def subtitle_jobs(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM subtitle_jobs ORDER BY next_check").fetchall()

    def subtitle_job_poll_hint(self, *, min_priority: int = 200) -> dict[str, float | int]:
        """Return the tiny scheduling state needed by foreground polling.

        The web UI must not keep a stale due subtitle row hot-looping after a
        worker has moved its next_check into the future.  Fetch only aggregate
        scheduling metadata instead of serializing every subtitle job.
        """
        now = time.time()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN state IN ('pending','processing') THEN 1 ELSE 0 END) AS active_count,
                  SUM(CASE WHEN state IN ('pending','processing') AND next_check<=? THEN 1 ELSE 0 END) AS due_count,
                  SUM(CASE WHEN state IN ('pending','processing') AND priority>=? THEN 1 ELSE 0 END) AS priority_count,
                  SUM(CASE WHEN state IN ('pending','processing') AND priority>=? AND next_check<=? THEN 1 ELSE 0 END) AS priority_due_count,
                  MIN(CASE WHEN state IN ('pending','processing') THEN next_check ELSE NULL END) AS next_check
                FROM subtitle_jobs
                """,
                (now, max(0, int(min_priority)), max(0, int(min_priority)), now),
            ).fetchone()
        return {
            "active_count": int((row["active_count"] if row is not None else 0) or 0),
            "due_count": int((row["due_count"] if row is not None else 0) or 0),
            "priority_count": int((row["priority_count"] if row is not None else 0) or 0),
            "priority_due_count": int((row["priority_due_count"] if row is not None else 0) or 0),
            "next_check": float((row["next_check"] if row is not None else 0.0) or 0.0),
        }

    def reset_pending_subtitle_jobs(self) -> int:
        """Make pending jobs immediately due after a resolver/validation upgrade."""
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE subtitle_jobs SET next_check=?,updated_at=? WHERE state='pending'",
                (now, now),
            )
        return int(cursor.rowcount or 0)

    def plan_episode_identity_repair(
        self,
        torrent_hash: str,
        video_path: Path,
        *,
        media_id: int,
        expected_media_episode: int,
        media_episode: int,
        release_episode: int,
        migrate_history: bool = False,
    ) -> dict:
        """Read-only diff for one user-confirmed file, never a franchise rule."""
        video = Path(video_path).expanduser().resolve()
        if (
            media_id < 1
            or expected_media_episode < 1
            or media_episode < 1
            or release_episode < 1
            or not torrent_hash.strip()
        ):
            raise ValueError(err("db.episodeNumbersPositive"))
        conflicts = []
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE video_path=?", (str(video),)).fetchone()
            before = dict(row) if row is not None else None
            if (
                row is None
                or int(row["media_id"] or 0) != media_id
                or int(row["media_episode"] or row["episode"] or 0) != expected_media_episode
                or str(row["torrent_hash"]).casefold() != torrent_hash.casefold()
            ):
                conflicts.append("File identity no longer matches the expected source")
            if row is not None and float(row["playback_updated_at"] or 0) > time.time() - 120:
                conflicts.append("Recent playback owns this file; stop playback and wait before repairing")
            target = conn.execute(
                "SELECT video_path FROM episodes WHERE media_id=? AND COALESCE(media_episode,episode)=? AND video_path!=?",
                (media_id, media_episode, str(video)),
            ).fetchall()
            if target:
                conflicts.append("Target local episode already has another file")
            ledgers = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM media_identity_ledger WHERE video_path=? OR torrent_hash=?",
                    (str(video), torrent_hash),
                )
            ]
            canonical_id = f"anime:{media_id}:episode:{media_episode}"
            target_ledger = conn.execute(
                "SELECT * FROM media_identity_ledger WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if target_ledger is not None and str(target_ledger["video_path"]) != str(video):
                conflicts.append("Target identity ledger belongs to another file")
            if any(
                item["video_path"] not in {"", str(video)} or item["media_id"] not in {None, media_id}
                for item in ledgers
            ):
                conflicts.append("Torrent identity ledger includes another file or media")
            if any(
                item["locked"]
                and (
                    item["media_id"] != media_id
                    or item["media_episode"] != media_episode
                    or item["release_episode"] != release_episode
                )
                for item in ledgers
            ):
                conflicts.append("A manually locked identity conflicts with the repair")
            jobs = [
                dict(item)
                for item in conn.execute("SELECT * FROM subtitle_jobs WHERE video_path=?", (str(video),))
            ]
            if any(float(item["lease_until"] or 0) > time.time() or item["owner_token"] for item in jobs):
                conflicts.append("An active subtitle job owns this file")
            download = conn.execute(
                "SELECT * FROM downloads WHERE torrent_hash=?", (torrent_hash,)
            ).fetchone()
            if download is not None and (
                download["media_id"] != media_id
                or int(download["media_episode"] or download["episode"] or 0) != expected_media_episode
                or download["is_batch"]
            ):
                conflicts.append("Download identity conflicts with the repair")
            key = f"download_intent:{media_id}:episode:{expected_media_episode}"
            intent_row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            intent = json.loads(intent_row["value"]) if intent_row else None
            if intent and str(intent.get("selected_hash") or "").casefold() not in {
                "",
                torrent_hash.casefold(),
            }:
                conflicts.append("A newer intent selected another release")
            if intent and (
                intent.get("owned_candidates")
                or str(intent.get("state") or "") in {"trying", "selecting", "recovery_required"}
            ):
                conflicts.append("An active acquisition owns this episode")
            history = conn.execute(
                "SELECT * FROM release_history WHERE lower(info_hash)=lower(?)", (torrent_hash,)
            ).fetchone()
            identity_row = conn.execute(
                "SELECT value FROM state WHERE key=?", ("release_identity:" + torrent_hash.casefold(),)
            ).fetchone()
            identity = json.loads(identity_row["value"]) if identity_row else None
            if identity and (
                identity.get("media_id"),
                identity.get("media_episode"),
                identity.get("release_episode"),
            ) != (media_id, media_episode, release_episode):
                conflicts.append("A confirmed file identity conflicts with the repair")
            subtitle_history = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM subtitle_history WHERE video_path=? ORDER BY id", (str(video),)
                )
            ]
            playlist_items = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM playlist_items WHERE video_path=? ORDER BY id", (str(video),)
                )
            ]
            consumption = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM consumption_media WHERE (kind='anime_episode' AND current_library_id IN (?,?)) OR media_uuid IN (SELECT media_uuid FROM consumption_media_aliases WHERE alias_type='video_path' AND alias_value=?)",
                    (f"{media_id}:{expected_media_episode}", f"{media_id}:{media_episode}", str(video)),
                )
            ]
            sync_entities = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM sync_entities WHERE kind='anime_episode' AND local_key IN (?,?)",
                    (f"{media_id}:{expected_media_episode}", f"{media_id}:{media_episode}"),
                )
            ]
            consumption_aliases = (
                [
                    dict(item)
                    for item in conn.execute(
                        "SELECT kind,alias_type,alias_value,media_uuid FROM consumption_media_aliases WHERE media_uuid IN (%s) ORDER BY alias_type,alias_value"
                        % ",".join("?" * len(consumption)),
                        [item["media_uuid"] for item in consumption],
                    )
                ]
                if consumption
                else []
            )
            if sync_entities or (consumption and not migrate_history):
                conflicts.append(
                    "Published consumption/mobile history requires a separate reviewed history migration"
                    + ("" if sync_entities else " (re-plan with --migrate-history to relabel it)")
                )
            elif consumption:
                # Reviewed relabel of ONE history entry that belongs to this file:
                # its watch time moves with the file to the corrected episode.
                target_alias = f"anilist:{media_id}:episode:{media_episode}"
                foreign = conn.execute(
                    "SELECT media_uuid FROM consumption_media_aliases WHERE kind='anime_episode' AND alias_type='external' AND alias_value=? AND media_uuid!=?",
                    (target_alias, consumption[0]["media_uuid"]),
                ).fetchone()
                if (
                    len(consumption) != 1
                    or consumption[0]["current_library_id"]
                    not in {"", f"{media_id}:{expected_media_episode}"}
                    or foreign is not None
                ):
                    conflicts.append(
                        "History relabel is ambiguous: several history entries own these episodes"
                    )
        fingerprint = None
        if video.is_file():
            digest = hashlib.sha256()
            with video.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            fingerprint = digest.hexdigest()
        else:
            conflicts.append("The expected video file is missing")
        return {
            "database_path": str(self.path.resolve()),
            "torrent_hash": torrent_hash,
            "video_path": str(video),
            "media_id": media_id,
            "expected_media_episode": expected_media_episode,
            "media_episode": media_episode,
            "release_episode": release_episode,
            "before": before,
            "download_before": dict(download) if download is not None else None,
            "ledger_before": ledgers,
            "jobs_before": jobs,
            "intent_before": intent,
            "history_before": dict(history) if history is not None else None,
            "identity_before": identity,
            "target_ledger_before": dict(target_ledger) if target_ledger is not None else None,
            "subtitle_history_before": subtitle_history,
            "playlist_items_before": playlist_items,
            "consumption_before": consumption,
            "sync_entities_before": sync_entities,
            "consumption_aliases_before": consumption_aliases,
            "migrate_history": bool(migrate_history),
            "fingerprint": fingerprint,
            "conflicts": conflicts,
        }

    def apply_episode_identity_repair(self, plan: dict) -> dict:
        """Apply a reviewed dry-run atomically, preserving bytes and playback truth."""
        if plan.get("conflicts"):
            raise ValueError("Identity repair conflict: " + "; ".join(plan["conflicts"]))
        parameters = {
            key: plan[key]
            for key in ("media_id", "expected_media_episode", "media_episode", "release_episode")
        }
        parameters["migrate_history"] = bool(plan.get("migrate_history", False))
        current = self.plan_episode_identity_repair(
            plan["torrent_hash"], Path(plan["video_path"]), **parameters
        )
        anchor = current.get("identity_before") or {}
        if (
            plan.get("database_path") == str(self.path.resolve())
            and anchor.get("source") == "user-confirmed-file"
            and anchor.get("video_path") == plan["video_path"]
            and anchor.get("fingerprint") == current.get("fingerprint") == plan.get("fingerprint")
            and (anchor.get("media_id"), anchor.get("media_episode"), anchor.get("release_episode"))
            == (plan["media_id"], plan["media_episode"], plan["release_episode"])
            and current.get("before") is not None
            and current["before"]["media_episode"] == plan["media_episode"]
        ):
            return {"identity": anchor, "backup": anchor.get("backup", ""), "already_applied": True}
        if current != plan:
            raise ValueError(err("db.repairSourceChanged"))
        backup = self.path.with_name(f"{self.path.name}.identity-repair-{time.time_ns()}.backup")
        with self.connect() as source:
            vacuum_into(source, backup)
        now = time.time()
        with self.connection_scope() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Recheck all mutable ownership records under the write lock.
            current = self.plan_episode_identity_repair(
                plan["torrent_hash"], Path(plan["video_path"]), **parameters
            )
            if current != plan:
                raise ValueError(err("db.repairSourceChanged"))
            video, hash_, media = plan["video_path"], plan["torrent_hash"], plan["media_id"]
            local, release = plan["media_episode"], plan["release_episode"]
            anchor = {
                "media_id": media,
                "media_episode": local,
                "release_episode": release,
                "source": "user-confirmed-file",
                "fingerprint": plan["fingerprint"],
                "video_path": video,
                "revision": 1,
                "cleanup_protected_until": now + 7 * 86400,
                "backup": str(backup),
            }
            raw = json.loads(plan["download_before"]["raw_json"]) if plan["download_before"] else {}
            raw["_release_identity"] = anchor
            # A two-episode file keeps its span from the confirmed first episode (audit 17А).
            span_end = "media_episode_end=CASE WHEN media_episode_end IS NOT NULL AND ? IS NOT NULL THEN ?+1 ELSE NULL END"
            conn.execute(
                f"UPDATE episodes SET episode=?,media_episode=?,release_episode=?,{span_end},delete_after=NULL,updated_at=? WHERE video_path=?",
                (local, local, release, local, local, now, video),
            )
            conn.execute("DELETE FROM state WHERE key=?", (season_zero_set_aside_key(video),))
            conn.execute(
                f"UPDATE downloads SET episode=?,media_episode=?,release_episode=?,{span_end},raw_json=?,updated_at=? WHERE torrent_hash=?",
                (local, local, release, local, local, json.dumps(raw), now, hash_),
            )
            conn.execute(_UI_STATE_BUMP_SQL)  # the downloads trigger leaves raw_json out
            conn.execute(
                "UPDATE release_history SET episode=?,media_episode=?,release_episode=? WHERE lower(info_hash)=lower(?)",
                (local, local, release, hash_),
            )
            conn.execute(
                "UPDATE subtitle_jobs SET episode=?,generation=generation+1,owner_token='',lease_until=0,updated_at=? WHERE video_path=?",
                (local, now, video),
            )
            conn.execute(
                "UPDATE subtitle_history SET episode=? WHERE video_path=? AND media_id=? AND episode=?",
                (local, video, media, plan["expected_media_episode"]),
            )
            conn.execute(
                "UPDATE playlist_items SET episode=?,updated_at=? WHERE video_path=? AND media_id=? AND episode=?",
                (local, now, video, media, plan["expected_media_episode"]),
            )
            conn.execute(
                "DELETE FROM media_identity_ledger WHERE video_path=? OR torrent_hash=?", (video, hash_)
            )
            created_at = min((float(item["created_at"]) for item in plan["ledger_before"]), default=now)
            conn.execute(
                "INSERT INTO media_identity_ledger(canonical_id,media_id,media_episode,release_episode,video_path,fingerprint,torrent_hash,source,provenance_json,locked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    f"anime:{media}:episode:{local}",
                    media,
                    local,
                    release,
                    video,
                    plan["fingerprint"],
                    hash_,
                    "user-confirmed-file",
                    json.dumps(anchor),
                    created_at,
                    now,
                ),
            )
            key = "release_identity:" + hash_.casefold()
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, json.dumps(anchor), now),
            )
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?)",
                (
                    f"identity_repair_audit:{hash_}:{time.time_ns()}",
                    json.dumps({"before": plan, "after": anchor, "backup": str(backup)}),
                    now,
                ),
            )
            if plan.get("migrate_history") and plan.get("consumption_before"):
                from .consumption import _json as _cjson

                item = plan["consumption_before"][0]
                media_uuid = item["media_uuid"]
                anime_row = conn.execute("SELECT title FROM anime WHERE media_id=?", (media,)).fetchone()
                title = f"{(anime_row['title'] if anime_row else '') or media} · {local}"
                try:
                    metadata = json.loads(item.get("metadata_json") or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                metadata.update(media_id=media, episode=local)
                conn.execute(
                    "UPDATE consumption_media SET title_snapshot=?,current_library_kind='anime_episode',current_library_id=?,metadata_json=?,deleted_at=NULL,updated_at=? WHERE media_uuid=?",
                    (title, f"{media}:{local}", _cjson(metadata), now, media_uuid),
                )
                conn.execute(
                    "DELETE FROM consumption_media_aliases WHERE kind='anime_episode' AND alias_type='external' AND alias_value=? AND media_uuid=?",
                    (f"anilist:{media}:episode:{plan['expected_media_episode']}", media_uuid),
                )
                for alias_type, alias_value in (
                    ("external", f"anilist:{media}:episode:{local}"),
                    ("video_path", video),
                ):
                    conn.execute(
                        "INSERT INTO consumption_media_aliases(kind,alias_type,alias_value,media_uuid,created_at) VALUES('anime_episode',?,?,?,?) "
                        "ON CONFLICT(kind,alias_type,alias_value) DO UPDATE SET media_uuid=excluded.media_uuid",
                        (alias_type, alias_value, media_uuid, now),
                    )
            intent = dict(plan["intent_before"]) if plan["intent_before"] else None
            if intent:
                intent.update(
                    state="waiting",
                    revision=int(intent.get("revision") or 0) + 1,
                    selected_hash="",
                    selected_title="",
                    detail="Selected file belongs to another local episode",
                    updated_at=now,
                )
                conn.execute(
                    "UPDATE state SET value=?,updated_at=? WHERE key=?",
                    (
                        json.dumps(intent),
                        now,
                        f"download_intent:{media}:episode:{plan['expected_media_episode']}",
                    ),
                )
        return {"identity": anchor, "backup": str(backup)}

    def upsert_download(self, item: DownloadItem) -> None:
        raw_json = json.dumps(item.raw, ensure_ascii=False)
        with self.connect() as conn:
            self._ensure_anime_parent(conn, item.media_id, item.name)
            previous = conn.execute(
                "SELECT raw_json FROM downloads WHERE torrent_hash=?", (item.torrent_hash,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO downloads(
                    torrent_hash,name,state,progress,save_path,content_path,media_id,episode,media_episode,release_episode,
                    is_batch,added_on,completed_on,raw_json,updated_at,media_episode_end
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(torrent_hash) DO UPDATE SET
                    name=excluded.name,state=excluded.state,progress=excluded.progress,
                    save_path=excluded.save_path,content_path=excluded.content_path,
                    media_id=COALESCE(downloads.media_id,excluded.media_id),
                    episode=COALESCE(downloads.media_episode,excluded.media_episode,downloads.episode,excluded.episode),
                    media_episode=COALESCE(downloads.media_episode,excluded.media_episode,downloads.episode,excluded.episode),
                    release_episode=COALESCE(downloads.release_episode,excluded.release_episode),
                    media_episode_end=COALESCE(excluded.media_episode_end,downloads.media_episode_end),
                    is_batch=CASE WHEN ? THEN 0 ELSE MAX(downloads.is_batch,excluded.is_batch) END,
                    added_on=excluded.added_on,
                    completed_on=excluded.completed_on,raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.torrent_hash,
                    item.name,
                    item.state,
                    item.progress,
                    item.save_path,
                    item.content_path,
                    item.media_id,
                    item.media_episode,
                    item.media_episode,
                    item.release_episode,
                    1 if item.is_batch else 0,
                    item.added_on,
                    item.completed_on,
                    raw_json,
                    time.time(),
                    self._span_end(
                        str(item.name or ""), item.media_episode, item.media_episode_end, batch=item.is_batch
                    ),
                    # The client saw a single video file: an earlier "series pack" flag was wrong.
                    1 if (item.raw or {}).get("single_video_file") and not item.is_batch else 0,
                ),
            )
            # The trigger leaves raw_json out (its client stats change on every poll):
            # anything else in it (an error, the backend, tags, sizes) still refreshes Home.
            if previous is not None and _download_ui_raw(
                _json_object(previous["raw_json"])
            ) != _download_ui_raw(_json_object(raw_json)):
                conn.execute(_UI_STATE_BUMP_SQL)

    def downloads(self) -> list[DownloadItem]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM downloads ORDER BY added_on DESC,name").fetchall()
        result: list[DownloadItem] = []
        for row in rows:
            result.append(
                DownloadItem(
                    torrent_hash=str(row["torrent_hash"]),
                    name=str(row["name"]),
                    state=str(row["state"]),
                    progress=float(row["progress"]),
                    save_path=str(row["save_path"]),
                    content_path=str(row["content_path"]),
                    media_id=int(row["media_id"]) if row["media_id"] is not None else None,
                    episode=(
                        int(row["media_episode"])
                        if row["media_episode"] is not None
                        else int(row["episode"])
                        if row["episode"] is not None
                        else None
                    ),
                    media_episode=(
                        int(row["media_episode"])
                        if row["media_episode"] is not None
                        else int(row["episode"])
                        if row["episode"] is not None
                        else None
                    ),
                    release_episode=(
                        int(row["release_episode"])
                        if row["release_episode"] is not None
                        else int(row["episode"])
                        if row["episode"] is not None
                        else None
                    ),
                    is_batch=bool(row["is_batch"]),
                    added_on=int(row["added_on"] or 0),
                    completed_on=int(row["completed_on"] or 0),
                    raw=_json_object(row["raw_json"]),
                    media_episode_end=(
                        int(row["media_episode_end"])
                        if "media_episode_end" in row.keys() and row["media_episode_end"] is not None
                        else None
                    ),
                )
            )
        return result

    def prune_downloads(self, active_hashes: set[str], *, written_before: float | None = None) -> None:
        """Delete download rows not in ``active_hashes``.

        ``written_before``: only rows last written before then (when the client
        listing started); a row an add wrote meanwhile, in this process or the
        agent, is not in that listing yet.
        """
        age = "" if written_before is None else " AND updated_at<?"
        age_params: tuple[object, ...] = () if written_before is None else (float(written_before),)
        with self.connect() as conn:
            if not active_hashes:
                conn.execute("DELETE FROM downloads WHERE 1=1" + age, age_params)
                return
            placeholders = ",".join("?" for _ in active_hashes)
            conn.execute(
                f"DELETE FROM downloads WHERE torrent_hash NOT IN ({placeholders})" + age,
                (*tuple(active_hashes), *age_params),
            )

    def download_hashes_written_since(self, since: float) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT torrent_hash FROM downloads WHERE updated_at>=?", (float(since),)
            ).fetchall()
        return {str(row["torrent_hash"] or "").strip().casefold() for row in rows if row["torrent_hash"]}

    def delete_torrent_records(self, torrent_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM subtitle_jobs WHERE video_path IN (SELECT video_path FROM episodes WHERE torrent_hash=?)",
                (torrent_hash,),
            )
            conn.execute("DELETE FROM episodes WHERE torrent_hash=?", (torrent_hash,))
            conn.execute("DELETE FROM downloads WHERE torrent_hash=?", (torrent_hash,))

    def delete_download_record(self, torrent_hash: str) -> None:
        """Forget the download row only; its episode rows (and their hash) stay."""
        with self.connect() as conn:
            conn.execute("DELETE FROM downloads WHERE torrent_hash=?", (torrent_hash,))

    def set_episode_torrent_hash(self, video_path: Path, torrent_hash: str) -> int:
        value = str(torrent_hash or "").strip().casefold()
        if not value:
            return 0
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE episodes SET torrent_hash=?,updated_at=? "
                "WHERE video_path=? AND (torrent_hash='' OR torrent_hash IS NULL)",
                (value, time.time(), str(video_path)),
            )
        return max(0, int(cursor.rowcount or 0))

    def record_release(
        self,
        info_hash: str,
        media_id: int,
        episode: int | None,
        title: str,
        score: float,
        *,
        release_episode: int | None = None,
        score_formula: str = "",
        score_context: str = "",
        numbering_metadata: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            anchor_row = conn.execute(
                "SELECT value FROM state WHERE key=?", ("release_identity:" + info_hash.casefold(),)
            ).fetchone()
            anchor = json.loads(anchor_row["value"]) if anchor_row else {}
            if anchor.get("source") == "user-confirmed-file":
                media_id, episode, release_episode = (
                    anchor["media_id"],
                    anchor["media_episode"],
                    anchor["release_episode"],
                )
                numbering_metadata = dict(anchor)
            self._ensure_anime_parent(conn, media_id, title)
            conn.execute(
                "INSERT OR REPLACE INTO release_history("
                "info_hash,media_id,episode,media_episode,release_episode,title,score,selected_at,"
                "score_formula,score_context"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    info_hash.lower(),
                    media_id,
                    episode,
                    episode,
                    release_episode if release_episode is not None else episode,
                    title,
                    score,
                    time.time(),
                    str(score_formula or ""),
                    str(score_context or ""),
                ),
            )
            if numbering_metadata is not None:
                conn.execute(
                    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (
                        "release_numbering:" + info_hash.casefold(),
                        json.dumps(numbering_metadata),
                        time.time(),
                    ),
                )

    def release_score_record(
        self, media_id: int, episode: int | None, torrent_hash: str
    ) -> tuple[float, str, str] | None:
        """(score, formula, context) of exactly this torrent — never another release's row."""
        value = str(torrent_hash or "").strip().lower()
        if not value:
            return None
        episode_clause = (
            "COALESCE(media_episode,episode) IS NULL"
            if episode is None
            else "COALESCE(media_episode,episode)=?"
        )
        episode_args: tuple[object, ...] = () if episode is None else (episode,)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT score,score_formula,score_context FROM release_history "
                f"WHERE info_hash=? AND media_id=? AND {episode_clause}",
                (value, media_id, *episode_args),
            ).fetchone()
        if row is None:
            return None
        return float(row["score"]), str(row["score_formula"] or ""), str(row["score_context"] or "")

    def release_metadata_by_hash(self, torrent_hash: str) -> tuple[int, int | None, int | None, float] | None:
        value = torrent_hash.strip().lower()
        if not value:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT media_id,episode,media_episode,release_episode,score "
                "FROM release_history WHERE info_hash=?",
                (value,),
            ).fetchone()
        if row is None or row["media_id"] is None:
            return None
        return (
            int(row["media_id"]),
            int(row["media_episode"])
            if row["media_episode"] is not None
            else int(row["episode"])
            if row["episode"] is not None
            else None,
            int(row["release_episode"])
            if row["release_episode"] is not None
            else int(row["episode"])
            if row["episode"] is not None
            else None,
            float(row["score"]),
        )

    def release_score(
        self,
        media_id: int,
        episode: int | None,
        torrent_hash: str = "",
    ) -> float | None:
        with self.connect() as conn:
            row = None
            episode_clause = (
                "COALESCE(media_episode,episode) IS NULL"
                if episode is None
                else "COALESCE(media_episode,episode)=?"
            )
            episode_args: tuple[object, ...] = () if episode is None else (episode,)
            if torrent_hash:
                row = conn.execute(
                    f"SELECT score FROM release_history WHERE info_hash=? AND media_id=? AND {episode_clause}",
                    (torrent_hash.lower(), media_id, *episode_args),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT score FROM release_history WHERE media_id=? AND {episode_clause} "
                    "ORDER BY selected_at DESC LIMIT 1",
                    (media_id, *episode_args),
                ).fetchone()
        return float(row["score"]) if row else None

    def episode_for_torrent(self, media_id: int, episode: int, torrent_hash: str) -> LibraryEpisode | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE media_id=? AND COALESCE(media_episode,episode)=? AND torrent_hash=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (media_id, episode, torrent_hash),
            ).fetchone()
        return self._episode_from_row(row) if row else None

    def episode_count_for_torrent(self, torrent_hash: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM episodes WHERE torrent_hash=?",
                (torrent_hash,),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def download_by_hash(self, torrent_hash: str) -> DownloadItem | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM downloads WHERE torrent_hash=?", (torrent_hash,)).fetchone()
        if row is None:
            return None
        return DownloadItem(
            torrent_hash=str(row["torrent_hash"]),
            name=str(row["name"]),
            state=str(row["state"]),
            progress=float(row["progress"]),
            save_path=str(row["save_path"]),
            content_path=str(row["content_path"]),
            media_id=int(row["media_id"]) if row["media_id"] is not None else None,
            episode=(
                int(row["media_episode"])
                if row["media_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            media_episode=(
                int(row["media_episode"])
                if row["media_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            release_episode=(
                int(row["release_episode"])
                if row["release_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            is_batch=bool(row["is_batch"]),
            added_on=int(row["added_on"] or 0),
            completed_on=int(row["completed_on"] or 0),
            raw=json.loads(row["raw_json"] or "{}"),
            media_episode_end=(
                int(row["media_episode_end"])
                if "media_episode_end" in row.keys() and row["media_episode_end"] is not None
                else None
            ),
        )

    def has_pending_upgrade(self, media_id: int, episode: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM upgrade_jobs WHERE media_id=? AND episode=? "
                "AND state IN ('downloading','ready_to_replace') LIMIT 1",
                (media_id, episode),
            ).fetchone()
        return bool(row)

    def record_upgrade(
        self,
        *,
        new_info_hash: str,
        old_torrent_hash: str,
        media_id: int,
        episode: int,
        old_score: float,
        new_score: float,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id)
            conn.execute(
                """
                INSERT OR REPLACE INTO upgrade_jobs(
                    new_info_hash,old_torrent_hash,media_id,episode,old_score,new_score,
                    state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'downloading',?,?)
                """,
                (
                    new_info_hash.lower(),
                    old_torrent_hash.lower(),
                    media_id,
                    episode,
                    old_score,
                    new_score,
                    now,
                    now,
                ),
            )

    def pending_upgrades(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM upgrade_jobs WHERE state IN ('downloading','ready_to_replace') "
                "ORDER BY created_at"
            ).fetchall()

    def complete_upgrade(self, new_info_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE upgrade_jobs SET state='completed',updated_at=? WHERE new_info_hash=?",
                (time.time(), new_info_hash.lower()),
            )

    def expire_upgrades(self, max_age_seconds: float) -> list[sqlite3.Row]:
        """Give up on pending upgrade jobs older than ``max_age_seconds``.

        The old file stays and the episode becomes eligible for upgrade checks again.
        """
        now = time.time()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM upgrade_jobs WHERE state IN ('downloading','ready_to_replace') AND created_at<?",
                (now - max(0.0, float(max_age_seconds)),),
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE upgrade_jobs SET state='expired',updated_at=? WHERE new_info_hash=?",
                    [(now, str(row["new_info_hash"])) for row in rows],
                )
        return rows

    @staticmethod
    def _sampled_video_fingerprint(video_path: Path, *, chunk_size: int = 128 * 1024) -> str | None:
        """Path-independent identity for safe subtitle-history recovery."""

        path = Path(video_path)
        try:
            size = path.stat().st_size
            digest = hashlib.sha256()
            digest.update(str(size).encode("ascii"))
            with path.open("rb") as handle:
                offsets = sorted(
                    {
                        0,
                        max(0, size // 2 - chunk_size // 2),
                        max(0, size - chunk_size),
                    }
                )
                for offset in offsets:
                    handle.seek(offset)
                    digest.update(offset.to_bytes(8, "big", signed=False))
                    digest.update(handle.read(chunk_size))
            return digest.hexdigest()
        except OSError:
            return None

    def record_subtitle_history(
        self,
        *,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
        source: str = "",
        candidate_name: str = "",
        candidate_path: Path | str | None = None,
        score: float | None = None,
        status: str = "selected",
        reason: str = "",
        details: dict[str, object] | None = None,
    ) -> int:
        now = time.time()
        detail_payload = dict(details or {})
        video_fingerprint = ""
        if str(status or "").casefold() in {"selected", "upgraded", "manual", "legacy"}:
            video_fingerprint = self._sampled_video_fingerprint(Path(video_path)) or ""
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, video_path.stem)
            cursor = conn.execute(
                """
                INSERT INTO subtitle_history(
                    video_path,media_id,episode,source,candidate_name,candidate_path,
                    score,status,reason,details_json,video_fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(video_path),
                    media_id,
                    episode,
                    str(source or ""),
                    str(candidate_name or ""),
                    str(candidate_path or ""),
                    score,
                    str(status or "selected"),
                    str(reason or ""),
                    json.dumps(detail_payload, ensure_ascii=False),
                    video_fingerprint,
                    now,
                ),
            )
        return int(cursor.lastrowid or 0)

    def subtitle_history(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.*,a.title AS anime_title
                FROM subtitle_history h
                LEFT JOIN anime a ON a.media_id=h.media_id
                ORDER BY h.created_at DESC,h.id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            result.append(
                {
                    "id": int(row["id"]),
                    "video_path": str(row["video_path"]),
                    "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
                    "episode": int(row["episode"]) if row["episode"] is not None else None,
                    "anime_title": str(row["anime_title"] or ""),
                    "source": str(row["source"] or ""),
                    "candidate_name": str(row["candidate_name"] or ""),
                    "candidate_path": str(row["candidate_path"] or ""),
                    "score": float(row["score"]) if row["score"] is not None else None,
                    "status": str(row["status"] or ""),
                    "reason": str(row["reason"] or ""),
                    "details": details if isinstance(details, dict) else {},
                    "created_at": float(row["created_at"] or 0),
                }
            )
        return result

    def selected_subtitle_fingerprint(self, video_path: Path) -> str:
        """The content fingerprint recorded with this path's latest subtitle selection ("" if none)."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT video_fingerprint FROM subtitle_history
                WHERE video_path=? AND status IN ('selected','upgraded','manual','legacy')
                  AND video_fingerprint!=''
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (str(video_path),),
            ).fetchone()
        return str(row["video_fingerprint"] or "") if row is not None else ""

    def latest_selected_subtitle(self, video_path: Path) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM subtitle_history
                WHERE video_path=? AND status IN ('selected','upgraded','manual','legacy')
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (str(video_path),),
            ).fetchone()
        if row is None:
            return None
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        return {
            "id": int(row["id"]),
            "source": str(row["source"] or ""),
            "candidate_name": str(row["candidate_name"] or ""),
            "candidate_path": str(row["candidate_path"] or ""),
            "score": float(row["score"]) if row["score"] is not None else None,
            "status": str(row["status"] or ""),
            "details": details if isinstance(details, dict) else {},
            "created_at": float(row["created_at"] or 0),
        }

    def subtitle_history_max_id(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM subtitle_history").fetchone()
        return int(row["n"] or 0) if row is not None else 0

    def latest_selected_subtitle_rows(
        self, *, after_id: int = 0, chunk_size: int = 200
    ) -> Iterator[tuple[LibraryEpisode, dict[str, object]]]:
        """Episodes with a prepared subtitle, each with its latest selected history row.

        The same rows as ``latest_selected_subtitle`` per episode, in one query; each
        ``details_json`` is read and decoded once, a chunk at a time. With ``after_id``
        only episodes whose latest selection is newer than that history row.
        """
        after_id = max(0, int(after_id))
        sql = (
            "SELECT e.*,h.id AS h_id,h.source AS h_source,h.candidate_name AS h_candidate_name,"
            "h.candidate_path AS h_candidate_path,h.score AS h_score,h.status AS h_status,"
            "h.created_at AS h_created_at "
            "FROM episodes e JOIN subtitle_history h ON h.id=("
            "SELECT id FROM subtitle_history WHERE video_path=e.video_path "
            f"AND status IN {_SELECTED_HISTORY_STATUSES_SQL} ORDER BY created_at DESC,id DESC LIMIT 1) "
            "WHERE e.subtitle_path IS NOT NULL AND h.id>?"
        )
        params: tuple[object, ...] = (after_id,)
        if after_id:
            sql += " AND e.video_path IN (SELECT video_path FROM subtitle_history WHERE id>?)"
            params += (after_id,)
        sql += " ORDER BY e.title COLLATE NOCASE, COALESCE(e.media_episode,e.episode), e.video_path"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        size = max(1, int(chunk_size))
        for start in range(0, len(rows), size):
            part = rows[start : start + size]
            ids = [int(row["h_id"]) for row in part]
            with self.connect() as conn:
                details_by_id = {
                    int(found["id"]): found["details_json"]
                    for found in conn.execute(
                        f"SELECT id,details_json FROM subtitle_history WHERE id IN ({','.join('?' * len(ids))})",
                        ids,
                    )
                }
            for row in part:
                history_id = int(row["h_id"])
                yield (
                    self._episode_from_row(row),
                    {
                        "id": history_id,
                        "source": str(row["h_source"] or ""),
                        "candidate_name": str(row["h_candidate_name"] or ""),
                        "candidate_path": str(row["h_candidate_path"] or ""),
                        "score": float(row["h_score"]) if row["h_score"] is not None else None,
                        "status": str(row["h_status"] or ""),
                        "details": _json_object(details_by_id.get(history_id)),
                        "created_at": float(row["h_created_at"] or 0),
                    },
                )

    def latest_selected_subtitle_for_media_or_filename(
        self,
        *,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
    ) -> dict[str, object] | None:
        """Recover a selection after a path move only when video identity matches.

        Exact-path history remains compatible with legacy rows.  Any recovery
        across a different path (AniList identity or basename) requires the
        sampled content fingerprint recorded when the selection was created.
        """

        video_path = Path(video_path)
        current_fingerprint = self._sampled_video_fingerprint(video_path)

        def payload_for_row(
            row: sqlite3.Row | None, *, require_fingerprint: bool
        ) -> dict[str, object] | None:
            if row is None:
                return None
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            # Keep caller-provided ``details`` stable.  The ownership fingerprint
            # is internal metadata stored in its own column.  The details fallback
            # accepts rows written by an aborted/local v8.6 attempt.
            historical_fingerprint = str(
                row["video_fingerprint"] or details.get("video_content_fingerprint") or ""
            ).strip()
            if historical_fingerprint:
                if not current_fingerprint or historical_fingerprint != current_fingerprint:
                    return None
            elif require_fingerprint:
                return None
            return {
                "id": int(row["id"]),
                "video_path": str(row["video_path"] or ""),
                "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
                "episode": int(row["episode"]) if row["episode"] is not None else None,
                "source": str(row["source"] or ""),
                "candidate_name": str(row["candidate_name"] or ""),
                "candidate_path": str(row["candidate_path"] or ""),
                "score": float(row["score"]) if row["score"] is not None else None,
                "status": str(row["status"] or ""),
                "details": details,
                "created_at": float(row["created_at"] or 0),
            }

        with self.connect() as conn:
            exact = conn.execute(
                """
                SELECT * FROM subtitle_history
                WHERE video_path=? AND status IN ('selected','upgraded','manual','legacy')
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (str(video_path),),
            ).fetchone()
            exact_payload = payload_for_row(exact, require_fingerprint=False)
            if exact_payload is not None:
                return exact_payload

            if not current_fingerprint:
                return None

            clauses: list[tuple[str, tuple[object, ...]]] = []
            if media_id is not None:
                if episode is None:
                    clauses.append(("media_id=? AND episode IS NULL", (int(media_id),)))
                else:
                    clauses.append(
                        (
                            "media_id=? AND episode=?",
                            (int(media_id), int(episode)),
                        )
                    )
            # The escape character itself first: a "\" in the name made the next character a
            # literal (or a wildcard, "\_"), so such a file never found its own selection.
            suffix = "%/" + video_path.name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(("video_path LIKE ? ESCAPE '\\'", (suffix,)))

            for where, args in clauses:
                rows = conn.execute(
                    f"""
                    SELECT * FROM subtitle_history
                    WHERE ({where}) AND status IN ('selected','upgraded','manual','legacy')
                    ORDER BY created_at DESC,id DESC LIMIT 50
                    """,
                    args,
                ).fetchall()
                for row in rows:
                    candidate = payload_for_row(row, require_fingerprint=True)
                    if candidate is not None:
                        return candidate
        return None

    def create_playlist(
        self,
        *,
        name: str,
        kind: str,
        media_id: int | None,
        items: list[dict[str, object]],
    ) -> int:
        now = time.time()
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, name)
            for item in items:
                raw_media_id = item.get("media_id")
                self._ensure_anime_parent(
                    conn,
                    int(raw_media_id) if raw_media_id is not None else None,
                    str(item.get("title") or name),
                )
            cursor = conn.execute(
                "INSERT INTO playlists(name,kind,media_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(name), str(kind), media_id, now, now),
            )
            playlist_id = int(cursor.lastrowid)
            for position, item in enumerate(items, start=1):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO playlist_items(
                        playlist_id,position,media_id,episode,video_path,title,state,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'pending',?,?)
                    """,
                    (
                        playlist_id,
                        position,
                        item.get("media_id"),
                        item.get("episode"),
                        str(item.get("video_path") or ""),
                        str(item.get("title") or ""),
                        now,
                        now,
                    ),
                )
        return playlist_id

    def playlists(self) -> list[dict[str, object]]:
        with self.connect() as conn:
            queues = conn.execute("SELECT * FROM playlists ORDER BY updated_at DESC,id DESC").fetchall()
            rows = conn.execute("SELECT * FROM playlist_items ORDER BY playlist_id,position,id").fetchall()
        grouped: dict[int, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(int(row["playlist_id"]), []).append(
                {
                    "id": int(row["id"]),
                    "position": int(row["position"]),
                    "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
                    "episode": int(row["episode"]) if row["episode"] is not None else None,
                    "video_path": str(row["video_path"]),
                    "title": str(row["title"] or ""),
                    "state": str(row["state"] or "pending"),
                }
            )
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
                "created_at": float(row["created_at"] or 0),
                "updated_at": float(row["updated_at"] or 0),
                "items": grouped.get(int(row["id"]), []),
            }
            for row in queues
        ]

    def playlist(self, playlist_id: int) -> dict[str, object] | None:
        return next((item for item in self.playlists() if int(item["id"]) == int(playlist_id)), None)

    def mark_playlist_item(self, item_id: int, state: str) -> None:
        now = time.time()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT playlist_id FROM playlist_items WHERE id=?", (int(item_id),)
            ).fetchone()
            conn.execute(
                "UPDATE playlist_items SET state=?,updated_at=? WHERE id=?",
                (str(state), now, int(item_id)),
            )
            if row is not None:
                conn.execute(
                    "UPDATE playlists SET updated_at=? WHERE id=?",
                    (now, int(row["playlist_id"])),
                )

    def upgrade_jobs(self, *, limit: int = 50) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.*,a.title AS anime_title
                FROM upgrade_jobs u LEFT JOIN anime a ON a.media_id=u.media_id
                ORDER BY u.updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "new_info_hash": str(row["new_info_hash"]),
                "old_torrent_hash": str(row["old_torrent_hash"]),
                "media_id": int(row["media_id"]),
                "anime_title": str(row["anime_title"] or ""),
                "episode": int(row["episode"]),
                "old_score": float(row["old_score"]),
                "new_score": float(row["new_score"]),
                "state": str(row["state"]),
                "created_at": float(row["created_at"] or 0),
                "updated_at": float(row["updated_at"] or 0),
            }
            for row in rows
        ]

    def schedule_cleanup(
        self,
        video_path: Path,
        after_hours: float,
        *,
        list_status: str | None = None,
        media_id: int | None = None,
        episode: int | None = None,
    ) -> int:
        """Mark a played episode as watched and queue its deletion.

        Playback can reach the tracker through an equivalent path spelling
        (symlink, normalized Unicode or a moved parent directory). Prefer the
        exact path, then a resolved-path match, and finally a unique
        ``media_id + episode`` row supplied by the tracking payload.
        """
        now = time.time()
        when = now + max(0, after_hours) * 3600
        requested = Path(video_path).expanduser()
        selected_path: str | None = None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT media_id,episode,media_episode,video_path FROM episodes WHERE video_path=?",
                (str(requested),),
            ).fetchone()
            if row is not None:
                selected_path = str(row["video_path"])
            else:
                try:
                    wanted_resolved = requested.resolve(strict=False)
                except OSError:
                    wanted_resolved = requested.absolute()
                candidates = conn.execute(
                    "SELECT media_id,episode,media_episode,video_path FROM episodes"
                ).fetchall()
                resolved_matches: list[sqlite3.Row] = []
                for candidate in candidates:
                    try:
                        candidate_resolved = (
                            Path(str(candidate["video_path"])).expanduser().resolve(strict=False)
                        )
                    except OSError:
                        candidate_resolved = Path(str(candidate["video_path"])).expanduser().absolute()
                    if candidate_resolved == wanted_resolved:
                        resolved_matches.append(candidate)
                if len(resolved_matches) == 1:
                    row = resolved_matches[0]
                    selected_path = str(row["video_path"])
                elif media_id is not None:
                    if episode is None:
                        fallback_rows = conn.execute(
                            "SELECT media_id,episode,media_episode,video_path FROM episodes "
                            "WHERE media_id=? AND COALESCE(media_episode,episode) IS NULL",
                            (int(media_id),),
                        ).fetchall()
                    else:
                        fallback_rows = conn.execute(
                            "SELECT media_id,episode,media_episode,video_path FROM episodes "
                            "WHERE media_id=? AND ? BETWEEN COALESCE(media_episode,episode) "
                            "AND COALESCE(media_episode_end,media_episode,episode)",
                            (int(media_id), int(episode)),
                        ).fetchall()
                    if len(fallback_rows) == 1:
                        row = fallback_rows[0]
                        selected_path = str(row["video_path"])

            if selected_path is None or row is None:
                return 0

            cursor = conn.execute(
                "UPDATE episodes SET watched_at=?,delete_after=?,state='watched',"
                "playback_position=NULL,playback_duration=NULL,playback_updated_at=NULL,"
                "playback_active_seconds=0,updated_at=? WHERE video_path=?",
                (now, when, now, selected_path),
            )
            if row["media_id"] is not None:
                status = str(list_status or "").strip()
                # Movies and specials are stored with episode=NULL locally,
                # while AniList still represents a completed one-part entry as
                # progress=1. Keep the local card in sync immediately.
                same_media = media_id is None or int(media_id) == int(row["media_id"])
                local_progress = (
                    int(episode)
                    if same_media and episode is not None
                    else int(row["media_episode"])
                    if row["media_episode"] is not None
                    else int(row["episode"])
                    if row["episode"] is not None
                    else 1
                )
                conn.execute(
                    "UPDATE anime SET progress=MAX(progress,?),"
                    "status=CASE WHEN ?!='' THEN ? ELSE status END,updated_at=? "
                    "WHERE media_id=?",
                    (local_progress, status, status, now, int(row["media_id"])),
                )
            return max(0, int(cursor.rowcount or 0))

    def set_anime_status(self, media_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE anime SET status=?,updated_at=? WHERE media_id=?",
                (str(status), time.time(), int(media_id)),
            )

    def set_anime_score(self, media_id: int, score: float | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE anime SET user_score=?,updated_at=? WHERE media_id=?",
                (float(score) if score is not None else None, time.time(), int(media_id)),
            )

    def schedule_anime_cleanup(self, media_id: int, after_hours: float) -> int:
        now = time.time()
        when = now + max(0.0, float(after_hours)) * 3600.0
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE episodes SET state='dropped',delete_after=?,"
                "playback_position=NULL,playback_duration=NULL,playback_updated_at=NULL,playback_active_seconds=0,updated_at=? "
                "WHERE media_id=?",
                (when, now, int(media_id)),
            )
            conn.execute(
                "UPDATE anime SET status='DROPPED',updated_at=? WHERE media_id=?",
                (now, int(media_id)),
            )
            return max(0, int(cursor.rowcount or 0))

    def pending_deletion_media_ids(self) -> set[int]:
        """Titles whose local files are scheduled for deletion from the anime menu."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT media_id FROM episodes "
                "WHERE state='dropped' AND delete_after IS NOT NULL AND media_id IS NOT NULL"
            ).fetchall()
        return {int(row["media_id"]) for row in rows}

    # What Delete/Drop replaced, so "Keep" can put the title back: the list
    # status before and each episode row's state before it became 'dropped'.
    @staticmethod
    def _drop_restore_key(media_id: int) -> str:
        return f"drop_restore:{int(media_id)}"

    def save_drop_restore(self, media_id: int, previous_status: str) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT video_path,state FROM episodes WHERE media_id=? AND state!='dropped'",
                (int(media_id),),
            ).fetchall()
        payload = {
            "status": str(previous_status or ""),
            "episodes": {str(row["video_path"]): str(row["state"] or "") for row in rows},
        }
        # Deleting again while the files are still pending keeps the original
        # status/states (a finished deletion leaves only a stale record).
        if int(media_id) in self.pending_deletion_media_ids():
            try:
                earlier = json.loads(self.get_state(self._drop_restore_key(media_id), "") or "{}")
            except (TypeError, ValueError):
                earlier = {}
            if isinstance(earlier, dict) and earlier:
                payload["status"] = str(earlier.get("status") or payload["status"])
                if isinstance(earlier.get("episodes"), dict):
                    payload["episodes"] = {**payload["episodes"], **earlier["episodes"]}
        self.set_state(self._drop_restore_key(media_id), json.dumps(payload, ensure_ascii=False))

    def pop_drop_restore(self, media_id: int) -> dict[str, Any]:
        raw = self.get_state(self._drop_restore_key(media_id), "")
        self.delete_state(self._drop_restore_key(media_id))
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def restore_dropped_episode_states(self, media_id: int, states: dict[str, str]) -> int:
        """Give kept episode rows back the state they had before Delete/Drop."""
        restored = 0
        now = time.time()
        with self.connect() as conn:
            for video_path, state in states.items():
                if not state or state == "dropped":
                    continue
                cursor = conn.execute(
                    "UPDATE episodes SET state=?,updated_at=? "
                    "WHERE media_id=? AND video_path=? AND state='dropped' AND delete_after IS NULL",
                    (str(state), now, int(media_id), str(video_path)),
                )
                restored += max(0, int(cursor.rowcount or 0))
        return restored

    def keep_dropped_anime(self, media_id: int) -> int:
        """Cancel the scheduled deletion of a dropped title's local files (AniList stays Dropped)."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE episodes SET delete_after=NULL,updated_at=? "
                "WHERE media_id=? AND state='dropped' AND delete_after IS NOT NULL",
                (time.time(), int(media_id)),
            )
            return max(0, int(cursor.rowcount or 0))

    # Episodes deleted from the anime menu while the title stayed in the list:
    # automatic downloading must not fetch them again (until the user allows it
    # or downloads one by hand). A JSON list of AniList episode numbers.
    @staticmethod
    def _redownload_block_key(media_id: int) -> str:
        return f"redownload_blocked:{int(media_id)}"

    def redownload_blocked(self, media_id: int) -> set[int]:
        try:
            values = json.loads(self.get_state(self._redownload_block_key(media_id), "") or "[]")
        except (TypeError, ValueError):
            return set()
        if not isinstance(values, list):
            return set()
        return {int(value) for value in values if isinstance(value, int) and value > 0}

    def block_redownload(self, media_id: int) -> set[int]:
        """Block re-downloading every episode the title has rows for now."""
        blocked = self.redownload_blocked(media_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(media_episode,episode) AS first,"
                "COALESCE(media_episode_end,media_episode,episode) AS last "
                "FROM episodes WHERE media_id=? AND COALESCE(media_episode,episode) IS NOT NULL",
                (int(media_id),),
            ).fetchall()
        for row in rows:
            first, last = int(row["first"]), int(row["last"])
            blocked.update(range(first, max(first, last) + 1))
        blocked = {episode for episode in blocked if episode > 0}
        if blocked:
            self.set_state(self._redownload_block_key(media_id), json.dumps(sorted(blocked)))
        return blocked

    def allow_redownload(self, media_id: int, episode: int | None = None) -> int:
        """Lift the block for one episode, or for the whole title; returns how many were lifted."""
        blocked = self.redownload_blocked(media_id)
        lifted = blocked if episode is None else blocked & {int(episode)}
        remaining = blocked - lifted
        if remaining:
            self.set_state(self._redownload_block_key(media_id), json.dumps(sorted(remaining)))
        elif blocked:
            self.delete_state(self._redownload_block_key(media_id))
        return len(lifted)

    def rating_prompted(self, media_id: int, episode: int) -> bool:
        return self.get_state(f"rating_prompted:{int(media_id)}:{int(episode)}", "") == "1"

    def mark_rating_prompted(self, media_id: int, episode: int) -> None:
        self.set_state(f"rating_prompted:{int(media_id)}:{int(episode)}", "1")

    def personal_release_schedule(
        self,
        anime_id: int,
        *,
        profile_id: str = "default",
        now: float | None = None,
        advance_unlocks: bool = True,
    ) -> PersonalReleaseSchedule | None:
        current = time.time() if now is None else float(now)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM personal_release_schedules WHERE profile_id=? AND anime_id=?",
                (str(profile_id), int(anime_id)),
            ).fetchone()
            if row is None:
                return None
            # The UPDATE took the write lock on every Home snapshot even when
            # nothing was due; check first so reads stay reads.
            if (
                bool(row["enabled"])
                and advance_unlocks
                and conn.execute(
                    "SELECT 1 FROM personal_release_items "
                    "WHERE schedule_id=? AND unlocked_once=0 AND unlock_at_utc<=? LIMIT 1",
                    (int(row["schedule_id"]), current),
                ).fetchone()
            ):
                conn.execute(
                    """
                    UPDATE personal_release_items
                    SET unlocked_once=1,updated_at=?
                    WHERE schedule_id=? AND unlocked_once=0 AND unlock_at_utc<=?
                    """,
                    (current, int(row["schedule_id"]), current),
                )
            items = conn.execute(
                """
                SELECT * FROM personal_release_items
                WHERE schedule_id=? ORDER BY ordinal,logical_episode_id
                """,
                (int(row["schedule_id"]),),
            ).fetchall()
            try:
                rule = normalize_schedule_rule(
                    str(row["interval_unit"] or "weeks"),
                    int(row["interval_every"] or 1),
                    version=int(row["rule_version"] or 1),
                )
            except (ValueError, TypeError):
                # Legacy rows are always weekly. A corrupt future rule must not
                # silently turn into an arbitrary custom cadence.
                if str(row["cadence"] or "").casefold() != "weekly":
                    raise
                rule = PersonalReleaseRule()
            return PersonalReleaseSchedule(
                schedule_id=int(row["schedule_id"]),
                anime_id=int(row["anime_id"]),
                enabled=bool(row["enabled"]),
                start_local_datetime=str(row["start_local_datetime"]),
                timezone_iana=str(row["timezone_iana"]),
                first_episode=int(row["first_episode_id"]),
                revision=int(row["revision"]),
                rewatch=bool(row["rewatch"]),
                rule=rule,
                cycle_id=max(1, int(row["cycle_id"] or 1)),
                items=tuple(
                    PersonalReleaseItem(
                        episode=int(item["logical_episode_id"]),
                        ordinal=int(item["ordinal"]),
                        unlock_at_utc=float(item["unlock_at_utc"]),
                        nominal_local_datetime=str(item["nominal_local_datetime"]),
                        unlocked=bool(item["unlocked_once"]),
                        watched_at=float(item["watched_at"]) if item["watched_at"] is not None else None,
                        dst_adjusted=bool(item["dst_adjusted"]),
                    )
                    for item in items
                ),
            )

    def active_personal_release_schedules(self, *, now: float | None = None) -> list[PersonalReleaseSchedule]:
        current = time.time() if now is None else float(now)
        with self.connect() as conn:
            ids = [
                int(row["anime_id"])
                for row in conn.execute(
                    "SELECT anime_id FROM personal_release_schedules WHERE enabled=1 ORDER BY anime_id"
                ).fetchall()
            ]
        return [
            schedule
            for anime_id in ids
            if (schedule := self.personal_release_schedule(anime_id, now=current)) is not None
        ]

    def personal_release_upcoming_counts(
        self, start_utc: float, end_utc: float, *, profile_id: str = "default"
    ) -> dict[int, int]:
        """Unwatched items unlocking in (start, end] per anime with an enabled schedule (0 included)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.anime_id AS anime_id,
                       COUNT(i.logical_episode_id) AS upcoming
                FROM personal_release_schedules s
                LEFT JOIN personal_release_items i
                  ON i.schedule_id=s.schedule_id AND i.watched_at IS NULL
                 AND i.unlock_at_utc>? AND i.unlock_at_utc<=?
                WHERE s.enabled=1 AND s.profile_id=?
                GROUP BY s.anime_id
                """,
                (float(start_utc), float(end_utc), str(profile_id)),
            ).fetchall()
        return {int(row["anime_id"]): int(row["upcoming"] or 0) for row in rows}

    def save_personal_release_schedule(
        self,
        anime_id: int,
        *,
        episodes: list[int],
        start_local_datetime: str,
        timezone_iana: str,
        rewatch: bool,
        rule_unit: str = "weeks",
        rule_every: int = 1,
        expected_revision: int | None = None,
        new_cycle: bool = False,
        profile_id: str = "default",
        now: float | None = None,
    ) -> PersonalReleaseSchedule:
        ordered = [int(value) for value in episodes]
        if not ordered or any(value < 1 for value in ordered) or len(set(ordered)) != len(ordered):
            raise ValueError(err("db.scheduleEpisodesUnique"))
        rule = normalize_schedule_rule(rule_unit, rule_every)
        current = time.time() if now is None else float(now)
        materialized = materialize_schedule_items(
            ordered,
            start_local_datetime=start_local_datetime,
            timezone_iana=timezone_iana,
            rule=rule,
        )
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM personal_release_schedules WHERE profile_id=? AND anime_id=?",
                (str(profile_id), int(anime_id)),
            ).fetchone()
            actual_revision = int(previous["revision"]) if previous is not None else 0
            if expected_revision is not None and int(expected_revision) != actual_revision:
                raise PersonalScheduleRevisionConflict(int(expected_revision), actual_revision)
            if previous is not None and bool(previous["enabled"]) and not new_cycle:
                try:
                    previous_rule = normalize_schedule_rule(
                        str(previous["interval_unit"] or "weeks"),
                        int(previous["interval_every"] or 1),
                        version=int(previous["rule_version"] or 1),
                    )
                except (ValueError, TypeError):
                    previous_rule = PersonalReleaseRule()
                reanchored = reanchored_start_local_datetime(
                    [
                        PersonalReleaseItem(
                            episode=int(row["logical_episode_id"]),
                            ordinal=int(row["ordinal"]),
                            unlock_at_utc=float(row["unlock_at_utc"]),
                            nominal_local_datetime=str(row["nominal_local_datetime"]),
                            unlocked=bool(row["unlocked_once"]),
                            watched_at=float(row["watched_at"]) if row["watched_at"] is not None else None,
                        )
                        for row in conn.execute(
                            "SELECT * FROM personal_release_items WHERE schedule_id=?",
                            (int(previous["schedule_id"]),),
                        ).fetchall()
                    ],
                    previous_rule=previous_rule,
                    rule=rule,
                    timezone_iana=timezone_iana,
                    now=current,
                )
                if reanchored is not None:
                    start_local_datetime = reanchored
                    materialized = materialize_schedule_items(
                        ordered,
                        start_local_datetime=start_local_datetime,
                        timezone_iana=timezone_iana,
                        rule=rule,
                    )
            revision = actual_revision + 1
            cadence = "weekly" if rule.is_default_weekly else "interval"
            if previous is None:
                cursor = conn.execute(
                    """
                    INSERT INTO personal_release_schedules(
                        profile_id,anime_id,enabled,start_local_datetime,timezone_iana,cadence,
                        rule_version,interval_unit,interval_every,interval_clock,cycle_id,
                        first_episode_id,revision,rewatch,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(profile_id),
                        int(anime_id),
                        1,
                        str(start_local_datetime),
                        str(timezone_iana),
                        cadence,
                        rule.version,
                        rule.unit,
                        rule.every,
                        rule.clock,
                        1,
                        ordered[0],
                        revision,
                        int(bool(rewatch)),
                        current,
                        current,
                    ),
                )
                schedule_id = int(cursor.lastrowid)
                cycle_id = 1
                preserved: dict[int, sqlite3.Row] = {}
            else:
                schedule_id = int(previous["schedule_id"])
                previous_cycle = max(1, int(previous["cycle_id"] or 1))
                cycle_id = previous_cycle + 1 if bool(new_cycle) else previous_cycle
                preserved = (
                    {}
                    if new_cycle
                    else {
                        int(row["logical_episode_id"]): row
                        for row in conn.execute(
                            "SELECT * FROM personal_release_items WHERE schedule_id=?",
                            (schedule_id,),
                        ).fetchall()
                        # Released (time passed, maybe not marked open yet) or
                        # watched episodes keep their real stored dates.
                        if bool(row["unlocked_once"])
                        or row["watched_at"] is not None
                        or float(row["unlock_at_utc"]) <= current
                    }
                )
                conn.execute(
                    """
                    UPDATE personal_release_schedules
                    SET enabled=1,start_local_datetime=?,timezone_iana=?,cadence=?,
                        rule_version=?,interval_unit=?,interval_every=?,interval_clock=?,cycle_id=?,
                        first_episode_id=?,revision=?,rewatch=?,updated_at=?
                    WHERE schedule_id=?
                    """,
                    (
                        str(start_local_datetime),
                        str(timezone_iana),
                        cadence,
                        rule.version,
                        rule.unit,
                        rule.every,
                        rule.clock,
                        cycle_id,
                        ordered[0],
                        revision,
                        int(bool(rewatch)),
                        current,
                        schedule_id,
                    ),
                )
                conn.execute("DELETE FROM personal_release_items WHERE schedule_id=?", (schedule_id,))
            for item in materialized:
                old = preserved.get(item.episode)
                unlock_at = float(old["unlock_at_utc"]) if old is not None else item.unlock_at_utc
                nominal = (
                    str(old["nominal_local_datetime"]) if old is not None else item.nominal_local_datetime
                )
                dst_adjusted = int(bool(old["dst_adjusted"])) if old is not None else int(item.dst_adjusted)
                unlocked = (
                    int(bool(old["unlocked_once"]) or float(old["unlock_at_utc"]) <= current)
                    if old is not None
                    else int(item.unlock_at_utc <= current)
                )
                watched_at = old["watched_at"] if old is not None else None
                item_revision = int(old["schedule_revision"]) if old is not None else revision
                conn.execute(
                    """
                    INSERT INTO personal_release_items(
                        schedule_id,logical_episode_id,ordinal,unlock_at_utc,nominal_local_datetime,
                        schedule_revision,dst_adjusted,unlocked_once,watched_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        schedule_id,
                        item.episode,
                        item.ordinal,
                        unlock_at,
                        nominal,
                        item_revision,
                        dst_adjusted,
                        unlocked,
                        watched_at,
                        current,
                    ),
                )
            conn.execute(
                "UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=? WHERE key='ui_state_version'",
                (current,),
            )
        result = self.personal_release_schedule(anime_id, profile_id=profile_id, now=current)
        if result is None:
            raise RuntimeError(err("db.scheduleSaveFailed"))
        return result

    def disable_personal_release_schedule(self, anime_id: int, *, profile_id: str = "default") -> bool:
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE personal_release_schedules SET enabled=0,revision=revision+1,updated_at=?
                WHERE profile_id=? AND anime_id=? AND enabled!=0
                """,
                (now, str(profile_id), int(anime_id)),
            )
            if int(cursor.rowcount or 0):
                conn.execute(
                    "UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=? WHERE key='ui_state_version'",
                    (now,),
                )
                return True
            return False

    def mark_personal_release_watched(
        self, anime_id: int, episode: int, *, watched_at: float | None = None, profile_id: str = "default"
    ) -> bool:
        now = time.time() if watched_at is None else float(watched_at)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT schedule_id FROM personal_release_schedules WHERE profile_id=? AND anime_id=? AND enabled=1",
                (str(profile_id), int(anime_id)),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                """
                UPDATE personal_release_items
                SET watched_at=COALESCE(watched_at,?),unlocked_once=1,updated_at=?
                WHERE schedule_id=? AND logical_episode_id=?
                """,
                (now, now, int(row["schedule_id"]), int(episode)),
            )
            if int(cursor.rowcount or 0):
                conn.execute(
                    "UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=? WHERE key='ui_state_version'",
                    (now,),
                )
                return True
            return False

    def personal_release_item_retained(self, media_id: int | None, episode: int | None) -> bool:
        if media_id is None or episode is None:
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM personal_release_schedules s
                JOIN personal_release_items i ON i.schedule_id=s.schedule_id
                WHERE s.enabled=1 AND s.anime_id=? AND i.logical_episode_id=? AND i.watched_at IS NULL
                LIMIT 1
                """,
                (int(media_id), int(episode)),
            ).fetchone()
            return row is not None

    def personal_release_retained_items(self) -> set[tuple[int, int]]:
        """Every (media id, episode) personal_release_item_retained() is True for, in one query."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT s.anime_id AS media_id, i.logical_episode_id AS episode
                FROM personal_release_schedules s
                JOIN personal_release_items i ON i.schedule_id=s.schedule_id
                WHERE s.enabled=1 AND i.watched_at IS NULL
                """
            ).fetchall()
        return {
            (int(row["media_id"]), int(row["episode"]))
            for row in rows
            if row["media_id"] is not None and row["episode"] is not None
        }

    def reconcile_anilist_progress(self, media_id: int, progress: int) -> int:
        """Undo local watched markers for episodes no longer watched on AniList.

        AniList progress is authoritative after an explicit library sync.  If the
        user decreases progress there, every local episode above the new value is
        returned to ``ready`` and removed from the cleanup queue.
        """
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE episodes
                SET state='ready',
                    watched_at=NULL,
                    delete_after=NULL,
                    updated_at=?
                WHERE media_id=?
                  AND episode IS NOT NULL
                  AND COALESCE(media_episode_end,episode)>?
                  AND (
                      state='watched'
                      OR watched_at IS NOT NULL
                  )
                """,
                (now, int(media_id), max(0, int(progress))),
            )
            return max(0, int(cursor.rowcount or 0))

    def repair_missing_cleanup_schedule(self, after_hours: float, *, library_root: Path | None = None) -> int:
        """Repair watched rows whose cleanup timestamp was never persisted.

        The primary repair handles explicit ``state='watched'`` rows. A second
        conservative branch repairs recent managed episodes only when both
        AniList progress and recorded active playback prove the episode was
        watched. This avoids treating arbitrary local files as disposable.
        Managed means a torrent hash, or ``downloaded_at`` for a file inside
        ``library_root``: older versions also set ``downloaded_at`` for a video
        dropped on the window, which stays the user's own file.
        """
        delay_seconds = max(0.0, float(after_hours)) * 3600.0
        now = time.time()
        try:
            root = Path(library_root).expanduser().resolve() if library_root is not None else None
        except OSError:
            root = None

        def downloaded_inside_root(video_path: object) -> bool:
            if root is None:
                return False
            try:
                return root in Path(str(video_path)).expanduser().resolve(strict=False).parents
            except OSError:
                return False

        with self.connect() as conn:
            explicit = conn.execute(
                """
                UPDATE episodes
                SET watched_at=COALESCE(watched_at, playback_updated_at, updated_at, downloaded_at, ?),
                    delete_after=COALESCE(watched_at, playback_updated_at, updated_at, downloaded_at, ?) + ?,
                    updated_at=?
                WHERE state='watched'
                  AND delete_after IS NULL
                """,
                (now, now, delay_seconds, now),
            )
            candidates = conn.execute(
                """
                SELECT e.id,e.torrent_hash,e.video_path
                FROM episodes e
                JOIN anime a ON a.media_id=e.media_id
                WHERE e.state!='watched'
                  AND e.watched_at IS NULL
                  AND e.delete_after IS NULL
                  AND e.episode IS NOT NULL
                  AND COALESCE(e.media_episode_end,e.episode)<=a.progress
                  AND (e.torrent_hash!='' OR e.downloaded_at IS NOT NULL)
                  AND e.playback_duration IS NOT NULL
                  AND e.playback_duration>0
                  AND e.playback_active_seconds >=
                      CASE
                          WHEN e.playback_duration * 0.65 > 180
                          THEN e.playback_duration * 0.65
                          ELSE 180
                      END
                """
            ).fetchall()
            owned_ids = [
                int(row["id"])
                for row in candidates
                if str(row["torrent_hash"] or "").strip() or downloaded_inside_root(row["video_path"])
            ]
            evidence_count = 0
            for row_id in owned_ids:
                evidence = conn.execute(
                    """
                    UPDATE episodes
                    SET state='watched',
                        watched_at=COALESCE(playback_updated_at, updated_at, downloaded_at, ?),
                        delete_after=COALESCE(playback_updated_at, updated_at, downloaded_at, ?) + ?,
                        playback_position=NULL,
                        updated_at=?
                    WHERE id=? AND state!='watched' AND watched_at IS NULL AND delete_after IS NULL
                    """,
                    (now, now, delay_seconds, now, row_id),
                )
                evidence_count += max(0, int(evidence.rowcount or 0))
            return max(0, int(explicit.rowcount or 0)) + evidence_count

    def reconcile_watched_cleanup(self, after_hours: float) -> int:
        """Repair watched rows and apply the current cleanup delay.

        Older versions could retain ``watched_at``/``delete_after`` while a
        library scan changed the visible state back to ``ready``.  The cleanup
        delay is a user setting, so pending watched rows are also rescheduled
        from their original ``watched_at`` timestamp whenever the manager starts.
        """
        delay_seconds = max(0.0, float(after_hours)) * 3600.0
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE episodes
                SET state='watched',
                    delete_after=watched_at + ?,
                    updated_at=?
                WHERE watched_at IS NOT NULL
                  AND delete_after IS NOT NULL
                  AND (
                      state!='watched'
                      OR ABS(delete_after - (watched_at + ?)) > 0.5
                  )
                """,
                (delay_seconds, now, delay_seconds),
            )
            return max(0, int(cursor.rowcount or 0))

    def due_cleanup(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT e.* FROM episodes e
                WHERE e.delete_after IS NOT NULL AND e.delete_after<=?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM personal_release_schedules s
                    JOIN personal_release_items i ON i.schedule_id=s.schedule_id
                    WHERE s.enabled=1 AND s.anime_id=e.media_id
                      AND i.logical_episode_id=e.episode AND i.watched_at IS NULL
                  )
                """,
                (time.time(),),
            ).fetchall()

    def merge_episode_path(self, old_path: Path, new_path: Path) -> bool:
        """Move/merge one persisted episode row to its canonical video path.

        Product path migrations can move the physical file while SQLite still
        contains the old absolute path.  A later library scan then inserts the
        same file again under the new path.  Merge the historical row instead
        of counting the same physical episode twice.
        """
        old_value = str(old_path.expanduser())
        new_value = str(new_path.expanduser())
        if old_value == new_value:
            return False

        now = time.time()
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM episodes WHERE video_path=?", (old_value,)).fetchone()
            if old is None:
                return False
            new = conn.execute("SELECT * FROM episodes WHERE video_path=?", (new_value,)).fetchone()

            if new is None:
                conn.execute(
                    "UPDATE episodes SET video_path=?,updated_at=? WHERE video_path=?",
                    (new_value, now, old_value),
                )
            else:
                old_state = str(old["state"] or "local")
                new_state = str(new["state"] or "local")
                state = stronger_episode_state(old_state, new_state)

                old_playback_at = float(old["playback_updated_at"] or 0.0)
                new_playback_at = float(new["playback_updated_at"] or 0.0)
                playback_source = old if old_playback_at > new_playback_at else new

                media_id = new["media_id"] if new["media_id"] is not None else old["media_id"]
                title = str(new["title"] or old["title"] or "")
                if new["media_id"] is None and old["media_id"] is not None:
                    title = str(old["title"] or title)

                conn.execute(
                    """
                    UPDATE episodes SET
                        media_id=?,title=?,episode=?,subtitle_path=?,embedded_subtitle_id=?,
                        subtitle_origin=?,state=?,torrent_hash=?,downloaded_at=?,watched_at=?,
                        delete_after=?,playback_position=?,playback_duration=?,playback_updated_at=?,
                        playback_active_seconds=?,updated_at=?
                    WHERE video_path=?
                    """,
                    (
                        media_id,
                        title,
                        new["episode"] if new["episode"] is not None else old["episode"],
                        new["subtitle_path"] or old["subtitle_path"],
                        new["embedded_subtitle_id"]
                        if new["embedded_subtitle_id"] is not None
                        else old["embedded_subtitle_id"],
                        str(new["subtitle_origin"] or old["subtitle_origin"] or ""),
                        state,
                        str(new["torrent_hash"] or old["torrent_hash"] or ""),
                        new["downloaded_at"] if new["downloaded_at"] is not None else old["downloaded_at"],
                        new["watched_at"] if new["watched_at"] is not None else old["watched_at"],
                        new["delete_after"] if new["delete_after"] is not None else old["delete_after"],
                        playback_source["playback_position"],
                        max(float(old["playback_duration"] or 0.0), float(new["playback_duration"] or 0.0))
                        or None,
                        playback_source["playback_updated_at"],
                        max(
                            float(old["playback_active_seconds"] or 0.0),
                            float(new["playback_active_seconds"] or 0.0),
                        ),
                        now,
                        new_value,
                    ),
                )
                conn.execute("DELETE FROM episodes WHERE video_path=?", (old_value,))

            old_job = conn.execute("SELECT * FROM subtitle_jobs WHERE video_path=?", (old_value,)).fetchone()
            new_job = conn.execute("SELECT * FROM subtitle_jobs WHERE video_path=?", (new_value,)).fetchone()
            if old_job is not None:
                if new_job is None:
                    conn.execute(
                        "UPDATE subtitle_jobs SET video_path=?,updated_at=? WHERE video_path=?",
                        (new_value, now, old_value),
                    )
                else:
                    conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (old_value,))

            conn.execute(
                "UPDATE subtitle_history SET video_path=? WHERE video_path=?", (new_value, old_value)
            )

            # Playlist rows have a UNIQUE(playlist_id, video_path) constraint.
            # Remove an obsolete duplicate before rewriting any remaining rows.
            conn.execute(
                """
                DELETE FROM playlist_items
                WHERE video_path=? AND EXISTS (
                    SELECT 1 FROM playlist_items AS canonical
                    WHERE canonical.playlist_id=playlist_items.playlist_id
                      AND canonical.video_path=?
                )
                """,
                (old_value, new_value),
            )
            conn.execute("UPDATE playlist_items SET video_path=? WHERE video_path=?", (new_value, old_value))
        return True

    def set_episode_identity(
        self, video_path: Path, media_id: int | None, episode: int | None, title: str
    ) -> bool:
        """Give one file another title and episode (an extra file the person kept).

        Unlike upsert_episode this can clear both: a file of no known title has neither.
        Its subtitle job starts over for the new identity.
        """
        now = time.time()
        with self.connect() as conn:
            if media_id is not None:
                self._ensure_anime_parent(conn, media_id, Path(video_path).stem)
            cursor = conn.execute(
                "UPDATE episodes SET media_id=?,episode=?,media_episode=?,release_episode=?,"
                "media_episode_end=NULL,title=?,updated_at=? WHERE video_path=?",
                (media_id, episode, episode, episode, str(title), now, str(video_path)),
            )
            conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            if episode is not None:
                # A set-aside special that gets an episode is decided: no longer set aside.
                conn.execute("DELETE FROM state WHERE key=?", (season_zero_set_aside_key(video_path),))
            return bool(cursor.rowcount)

    def delete_episode_record(self, video_path: Path) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            conn.execute("DELETE FROM episodes WHERE video_path=?", (str(video_path),))

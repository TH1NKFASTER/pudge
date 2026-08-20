from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .language import IMAGE_SUBTITLE_EXTENSIONS
from .manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from .episode_state import transition_episode_state, stronger_episode_state


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

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);


-- Cheap cross-process UI invalidation marker. These triggers bump one integer
-- only when data that can change rendered UI state changes. Playback heartbeat
-- fields are intentionally excluded so watching a video does not rebuild the UI
-- every second.
CREATE TRIGGER IF NOT EXISTS anime_ui_state_insert AFTER INSERT ON anime BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS anime_ui_state_update AFTER UPDATE ON anime BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS anime_ui_state_delete AFTER DELETE ON anime BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS episodes_ui_state_insert AFTER INSERT ON episodes BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS episodes_ui_state_update
AFTER UPDATE OF media_id,title,episode,video_path,subtitle_path,embedded_subtitle_id,subtitle_origin,state,torrent_hash,downloaded_at,watched_at,delete_after ON episodes BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS episodes_ui_state_delete AFTER DELETE ON episodes BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS downloads_ui_state_insert AFTER INSERT ON downloads BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS downloads_ui_state_update AFTER UPDATE ON downloads BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS downloads_ui_state_delete AFTER DELETE ON downloads BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;

CREATE TRIGGER IF NOT EXISTS subtitle_jobs_ui_state_insert AFTER INSERT ON subtitle_jobs BEGIN
    UPDATE state SET value=CAST(value AS INTEGER)+1,updated_at=CAST(strftime('%s','now') AS REAL) WHERE key='ui_state_version';
END;
CREATE TRIGGER IF NOT EXISTS subtitle_jobs_ui_state_update AFTER UPDATE ON subtitle_jobs BEGIN
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
    reading_direction TEXT NOT NULL DEFAULT 'rtl',
    anilist_id INTEGER,
    cover_url TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    user_score REAL,
    source_fingerprint TEXT NOT NULL DEFAULT '',
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
    created_at REAL NOT NULL,
    FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audiobook_bookmarks_book
ON audiobook_bookmarks(book_id,position);

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
"""

LATEST_SCHEMA_VERSION = 7


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


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing_database = self.path.is_file() and self.path.stat().st_size > 0
        with self.connect() as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            previous_version = int(version_row[0] if version_row else 0)
            if existing_database and previous_version < LATEST_SCHEMA_VERSION:
                self._backup_before_migration(conn, previous_version)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _execute_sql_script(conn, SCHEMA)
                self._migrate(conn)
                integrity = conn.execute("PRAGMA quick_check").fetchone()
                if integrity is None or str(integrity[0]).casefold() != "ok":
                    raise sqlite3.DatabaseError(
                        f"database integrity check failed: {integrity[0] if integrity else 'no result'}"
                    )
            except BaseException:
                conn.rollback()
                raise

    def _backup_before_migration(self, conn: sqlite3.Connection, version: int) -> Path:
        """Create a consistent last-known-good copy before changing the schema."""

        backup_path = self.path.with_name(f"{self.path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
        destination = sqlite3.connect(backup_path)
        try:
            conn.backup(destination)
        finally:
            destination.close()
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
            conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        # Keep additive compatibility checks idempotent for databases created by
        # local 0.7 checkpoints before the numbered v3 migration existed.
        self._ensure_column(conn, "manga_books", "anilist_id", "INTEGER")
        self._ensure_column(conn, "manga_books", "cover_url", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "manga_books", "site_url", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "manga_books", "user_score", "REAL")
        self._ensure_column(conn, "manga_books", "source_fingerprint", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "audiobooks", "speed", "REAL NOT NULL DEFAULT 1")
        self._ensure_column(conn, "audiobooks", "last_played_at", "REAL NOT NULL DEFAULT 0")
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
        episode_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
        }
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
        subtitle_job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(subtitle_jobs)").fetchall()
        }
        if "priority" not in subtitle_job_columns:
            conn.execute(
                "ALTER TABLE subtitle_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
            )
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
            """
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
                created_at REAL NOT NULL,
                FOREIGN KEY(book_id) REFERENCES audiobooks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_audiobook_bookmarks_book
            ON audiobook_bookmarks(book_id,position);
            CREATE TABLE IF NOT EXISTS reading_audio_links (
                ln_book_id INTEGER PRIMARY KEY,
                audiobook_id INTEGER NOT NULL,
                alignment_mode TEXT NOT NULL DEFAULT 'chapter',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(audiobook_id) REFERENCES audiobooks(id) ON DELETE CASCADE
            );
            """
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
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

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
            """
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
            """
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
            rows = conn.execute(
                f"SELECT key,value FROM state WHERE key IN ({placeholders})", keys
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM state WHERE key=?", (key,))


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
                    progress,episodes,format,season_year,start_date,studio,media_status,end_date,mean_score,user_score,duration,next_airing_episode,next_airing_at,relations_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    title=excluded.title,titles_json=excluded.titles_json,
                    synonyms_json=excluded.synonyms_json,cover_url=excluded.cover_url,
                    site_url=excluded.site_url,status=excluded.status,
                    progress=excluded.progress,episodes=excluded.episodes,format=excluded.format,
                    season_year=excluded.season_year,start_date=excluded.start_date,studio=excluded.studio,media_status=excluded.media_status,end_date=excluded.end_date,mean_score=excluded.mean_score,
                    user_score=excluded.user_score,duration=excluded.duration,next_airing_episode=excluded.next_airing_episode,
                    next_airing_at=excluded.next_airing_at,relations_json=excluded.relations_json,updated_at=excluded.updated_at
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
                    time.time(),
                ),
            )

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
            season_year=int(row["season_year"]) if "season_year" in row.keys() and row["season_year"] is not None else None,
            start_date=str(row["start_date"]) if "start_date" in row.keys() and row["start_date"] else None,
            studio=str(row["studio"] or "") if "studio" in row.keys() else "",
            media_status=str(row["media_status"]) if row["media_status"] else None,
            end_date=str(row["end_date"]) if row["end_date"] else None,
            mean_score=int(row["mean_score"]) if row["mean_score"] is not None else None,
            user_score=float(row["user_score"]) if "user_score" in row.keys() and row["user_score"] is not None else None,
            duration=int(row["duration"]) if row["duration"] is not None else None,
            next_airing_episode=(
                int(row["next_airing_episode"])
                if row["next_airing_episode"] is not None
                else None
            ),
            next_airing_at=int(row["next_airing_at"]) if row["next_airing_at"] else None,
            relations=list(json.loads(row["relations_json"] or "[]")) if "relations_json" in row.keys() else [],
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
        conn.execute(
            "INSERT OR IGNORE INTO anime(media_id,title,updated_at) VALUES(?,?,?)",
            (int(media_id), str(title or f"AniList {int(media_id)}"), now),
        )

    def upsert_episode(self, episode: LibraryEpisode, *, downloaded_at: float | None = None) -> None:
        now = time.time()
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
                    downloaded_at,watched_at,delete_after,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,episodes.media_id),
                    title=excluded.title,
                    episode=COALESCE(excluded.media_episode,episodes.episode),
                    media_episode=COALESCE(excluded.media_episode,episodes.media_episode,episodes.episode),
                    release_episode=COALESCE(excluded.release_episode,episodes.release_episode),
                    subtitle_path=CASE
                        WHEN excluded.subtitle_path IS NOT NULL THEN excluded.subtitle_path
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles')
                        THEN episodes.subtitle_path
                        ELSE NULL
                    END,
                    embedded_subtitle_id=CASE
                        WHEN excluded.embedded_subtitle_id IS NOT NULL THEN excluded.embedded_subtitle_id
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles')
                        THEN episodes.embedded_subtitle_id
                        ELSE NULL
                    END,
                    subtitle_origin=CASE
                        WHEN excluded.subtitle_origin!='' THEN excluded.subtitle_origin
                        WHEN episodes.state IN ('ready','watched','waiting_text_subtitles')
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
        row = conn.execute(
            "SELECT state FROM episodes WHERE video_path=?", (str(video_path),)
        ).fetchone()
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

    def episode_by_path(self, video_path: Path) -> LibraryEpisode | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE video_path=?", (str(video_path),)
            ).fetchone()
        return self._episode_from_row(row) if row else None

    def ready_episode(self, media_id: int, episode: int | None = None) -> LibraryEpisode | None:
        sql = "SELECT * FROM episodes WHERE media_id=? AND state IN ('ready','local','watched','waiting_subtitles','waiting_text_subtitles')"
        params: list[object] = [media_id]
        if episode is not None:
            sql += " AND COALESCE(media_episode,episode)=?"
            params.append(episode)
        sql += " ORDER BY CASE WHEN COALESCE(media_episode,episode) IS NULL THEN 1 ELSE 0 END, COALESCE(media_episode,episode) LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return self._episode_from_row(row) if row else None

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> LibraryEpisode:
        return LibraryEpisode(
            media_id=int(row["media_id"]) if row["media_id"] is not None else None,
            title=str(row["title"]),
            episode=(
                int(row["media_episode"])
                if "media_episode" in row.keys() and row["media_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            media_episode=(
                int(row["media_episode"])
                if "media_episode" in row.keys() and row["media_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            release_episode=(
                int(row["release_episode"])
                if "release_episode" in row.keys() and row["release_episode"] is not None
                else int(row["episode"])
                if row["episode"] is not None
                else None
            ),
            video_path=Path(str(row["video_path"])),
            subtitle_path=Path(str(row["subtitle_path"])) if row["subtitle_path"] else None,
            embedded_subtitle_id=(
                int(row["embedded_subtitle_id"])
                if row["embedded_subtitle_id"] is not None
                else None
            ),
            subtitle_origin=(str(row["subtitle_origin"] or "") if "subtitle_origin" in row.keys() else ""),
            state=str(row["state"]),
            torrent_hash=str(row["torrent_hash"] or ""),
            watched_at=float(row["watched_at"]) if row["watched_at"] else None,
            delete_after=float(row["delete_after"]) if row["delete_after"] else None,
            playback_position=(
                float(row["playback_position"])
                if "playback_position" in row.keys() and row["playback_position"] is not None
                else None
            ),
            playback_duration=(
                float(row["playback_duration"])
                if "playback_duration" in row.keys() and row["playback_duration"] is not None
                else None
            ),
            playback_updated_at=(
                float(row["playback_updated_at"])
                if "playback_updated_at" in row.keys() and row["playback_updated_at"] is not None
                else None
            ),
            playback_active_seconds=(
                float(row["playback_active_seconds"] or 0.0)
                if "playback_active_seconds" in row.keys()
                else 0.0
            ),
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
        with self.connect() as conn:
            if duration > 0 and position >= duration * 0.95:
                conn.execute(
                    "UPDATE episodes SET playback_position=NULL,playback_duration=?,"
                    "playback_updated_at=NULL,playback_active_seconds=playback_active_seconds+?,updated_at=? WHERE video_path=?",
                    (duration, active_seconds, now, str(video_path)),
                )
                return
            conn.execute(
                "UPDATE episodes SET playback_position=?,playback_duration=?,"
                "playback_updated_at=?,playback_active_seconds=playback_active_seconds+?,updated_at=? WHERE video_path=?",
                (position, duration or None, now, active_seconds, now, str(video_path)),
            )

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

    def clear_playback(self, video_path: Path) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE episodes SET playback_position=NULL,playback_duration=NULL,"
                "playback_updated_at=NULL,updated_at=? WHERE video_path=?",
                (time.time(), str(video_path)),
            )

    def resumable_episodes(
        self,
        *,
        min_position: float = 30.0,
        min_remaining: float = 60.0,
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
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM episodes WHERE media_id=? AND COALESCE(media_episode,episode)=? LIMIT 1",
                (media_id, episode),
            ).fetchone()
            if row:
                return True
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE media_id=? AND COALESCE(media_episode,episode)=? AND state NOT IN ('error','missing') LIMIT 1",
                (media_id, episode),
            ).fetchone()
        return bool(row)

    def set_subtitle_ready(
        self,
        video_path: Path,
        subtitle_path: Path | None,
        embedded_subtitle_id: int | None = None,
        *,
        origin: str = "",
    ) -> None:
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT state FROM episodes WHERE video_path=?", (str(video_path),)
            ).fetchone()
            now = time.time()
            resolved = self._transition_episode(
                conn, video_path, "ready", trigger="subtitle_ready"
            )
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
            conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            if previous is not None and str(previous["state"] or "") != resolved and resolved == "ready":
                version = str(time.time_ns())
                conn.execute(
                    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("ready_state_version", version, now),
                )


    def set_waiting_text_subtitles(
        self,
        video_path: Path,
        subtitle_path: Path | None = None,
        embedded_subtitle_id: int | None = None,
    ) -> None:
        """Keep a bitmap fallback for Library-only playback and retry text subs."""
        with self.connect() as conn:
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

    def clear_subtitle_selection(self, video_path: Path) -> None:
        """Drop a stale prepared subtitle while keeping the retry job intact."""
        with self.connect() as conn:
            resolved = self._transition_episode(
                conn, video_path, "waiting_subtitles", trigger="subtitle_invalidated"
            )
            conn.execute(
                "UPDATE episodes SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',"
                "state=?,updated_at=? WHERE video_path=?",
                (resolved, time.time(), str(video_path)),
            )

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
                    ) VALUES(?,?,?,'pending',0,?,'Waiting for Japanese text subtitles',?)
                    ON CONFLICT(video_path) DO UPDATE SET
                        media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                        episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                        state='pending',next_check=excluded.next_check,
                        last_error=excluded.last_error,updated_at=excluded.updated_at
                    """,
                    (video_path, media_id, episode, now, now),
                )
                repaired.append((video_path, media_id, episode))
        return len(repaired)

    def repair_spurious_ready_subtitle_jobs(self) -> int:
        """Remove resolver jobs accidentally attached to valid ready rows.

        Versions through 0.5.79 could preserve a prepared subtitle during a
        library upsert but still enqueue a new job from the temporary scan
        result. The next maintenance pass then treated the valid selection as
        stale. Prefer the existing ready/watched selection when its external
        file still exists (or it references an embedded track).
        """
        removable: list[str] = []
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

    def repair_stale_subtitle_selections(self) -> int:
        """Clear subtitle paths that are not currently considered validated.

        A row with an active subtitle job is awaiting validation, so any older
        prepared path must not be exposed to the player. The same applies to
        rows whose state is already ``local`` or ``waiting_subtitles``.
        """
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE episodes
                SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',
                    state='waiting_subtitles',updated_at=?
                WHERE (subtitle_path IS NOT NULL OR embedded_subtitle_id IS NOT NULL)
                  AND (
                    state NOT IN ('ready','watched','waiting_text_subtitles')
                    OR (
                        video_path IN (SELECT video_path FROM subtitle_jobs)
                        AND state!='waiting_text_subtitles'
                    )
                  )
                """,
                (now,),
            )
        return int(cursor.rowcount or 0)

    def invalidate_subtitle(
        self,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
        error: str,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, video_path.stem)
            conn.execute(
                "UPDATE episodes SET subtitle_path=NULL,embedded_subtitle_id=NULL,subtitle_origin='',"
                "state='waiting_subtitles',updated_at=? "
                "WHERE video_path=?",
                (now, str(video_path)),
            )
            conn.execute(
                """
                INSERT INTO subtitle_jobs(
                    video_path,media_id,episode,state,attempts,next_check,last_error,updated_at
                ) VALUES(?,?,?,'pending',0,?,?,?)
                ON CONFLICT(video_path) DO UPDATE SET
                    media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                    episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                    state='pending',stage='queued',attempts=0,next_check=excluded.next_check,
                    lease_until=0,heartbeat_at=0,progress_json='{}',action_code='',
                    last_error=excluded.last_error,updated_at=excluded.updated_at
                """,
                (str(video_path), media_id, episode, now, error[-1000:], now),
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

    def delete_subtitle_job(self, video_path: Path) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM subtitle_jobs WHERE video_path=?",
                (str(video_path),),
            )

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
                ) VALUES(?,?,?,'pending',0,?,'Waiting for Japanese subtitles',?)
                ON CONFLICT(video_path) DO NOTHING
                """,
                (str(video_path), media_id, episode, now, now),
            )
        return bool(cursor.rowcount)

    def force_requeue_unresolved_subtitle_jobs(
        self,
        *,
        priority: int = 20,
        recover_processing: bool = True,
    ) -> int:
        """Immediately retry every existing or reconstructable subtitle job.

        A blocking/manual maintenance pass may recover rows left in ``processing``
        by a killed process.  An *interactive* Refresh must not do that while
        another maintenance process owns the lock: those rows can be actively
        executing already.  In that case we only raise their priority and leave
        the processing lease intact, avoiding duplicate preparation while still
        making the UI track the requested check.
        """
        now = time.time()
        priority = max(0, min(1000, int(priority)))
        affected_paths: set[str] = set()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT video_path FROM subtitle_jobs "
                "WHERE state IN ('pending','processing','needs_action')"
            ).fetchall()
            for row in existing:
                affected_paths.add(str(row["video_path"]))
            conn.execute(
                "UPDATE subtitle_jobs SET state='pending',stage='queued',action_code='',next_check=?,"
                "priority=MAX(priority,?),last_error='Manual refresh',updated_at=? "
                "WHERE state='pending'",
                (now, priority, now),
            )
            if recover_processing:
                conn.execute(
                    "UPDATE subtitle_jobs SET state='pending',stage='queued',action_code='',next_check=?,"
                    "priority=MAX(priority,?),last_error='Manual refresh',updated_at=? "
                    "WHERE state='processing'",
                    (now, priority, now),
                )
            else:
                conn.execute(
                    "UPDATE subtitle_jobs SET priority=MAX(priority,?),"
                    "last_error='Manual refresh (already checking)',updated_at=? "
                    "WHERE state='processing'",
                    (priority, now),
                )

            rows = conn.execute(
                """
                SELECT video_path,media_id,episode
                FROM episodes
                WHERE state IN ('local','waiting_subtitles','waiting_text_subtitles')
                  AND (
                    state='waiting_text_subtitles'
                    OR (subtitle_path IS NULL AND embedded_subtitle_id IS NULL)
                  )
                """
            ).fetchall()
            for row in rows:
                video_path = str(row["video_path"])
                affected_paths.add(video_path)
                conn.execute(
                    """
                    INSERT INTO subtitle_jobs(
                        video_path,media_id,episode,state,attempts,priority,next_check,last_error,updated_at
                    ) VALUES(?,?,?,'pending',0,?,?,'Manual refresh',?)
                    ON CONFLICT(video_path) DO UPDATE SET
                        media_id=COALESCE(excluded.media_id,subtitle_jobs.media_id),
                        episode=COALESCE(excluded.episode,subtitle_jobs.episode),
                        state='pending',stage='queued',action_code='',next_check=excluded.next_check,
                        priority=MAX(subtitle_jobs.priority,excluded.priority),
                        last_error=excluded.last_error,updated_at=excluded.updated_at
                    """,
                    (
                        video_path,
                        int(row["media_id"]) if row["media_id"] is not None else None,
                        int(row["episode"]) if row["episode"] is not None else None,
                        priority,
                        now,
                        now,
                    ),
                )
        return len(affected_paths)

    def due_subtitle_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM subtitle_jobs WHERE state='pending' AND next_check<=? "
                "ORDER BY priority DESC,next_check,updated_at DESC LIMIT ?",
                (time.time(), limit),
            ).fetchall()

    def claim_due_subtitle_jobs(
        self,
        limit: int = 10,
        *,
        lease_seconds: float = 25 * 60,
    ) -> list[sqlite3.Row]:
        """Atomically claim pending jobs so two refresh threads cannot run them twice."""
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM subtitle_jobs "
                "WHERE (state='pending' AND next_check<=?) "
                "OR (state='processing' AND next_check<=?) "
                "ORDER BY priority DESC,next_check,updated_at DESC LIMIT ?",
                (now, now, limit),
            ).fetchall()
            if rows:
                paths = [str(row["video_path"]) for row in rows]
                placeholders = ",".join("?" for _ in paths)
                conn.execute(
                    f"UPDATE subtitle_jobs SET state='processing',stage='discovering',"
                    f"next_check=?,lease_until=?,heartbeat_at=?,action_code='',updated_at=? "
                    f"WHERE video_path IN ({placeholders})",
                    (now + lease_seconds, now + lease_seconds, now, now, *paths),
                )
        return rows

    def claim_subtitle_jobs_for_paths(
        self,
        video_paths: list[Path] | tuple[Path, ...],
        *,
        limit: int = 10,
        lease_seconds: float = 25 * 60,
    ) -> list[sqlite3.Row]:
        """Claim due jobs only for the supplied newly completed videos."""
        paths = list(dict.fromkeys(str(Path(path)) for path in video_paths if str(path)))
        if not paths or limit <= 0:
            return []
        now = time.time()
        placeholders = ",".join("?" for _ in paths)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT * FROM subtitle_jobs WHERE video_path IN ({placeholders}) "
                "AND ((state='pending' AND next_check<=?) "
                "OR (state='processing' AND next_check<=?)) "
                "ORDER BY priority DESC,next_check,updated_at DESC LIMIT ?",
                (*paths, now, now, max(1, int(limit))),
            ).fetchall()
            if rows:
                claimed = [str(row["video_path"]) for row in rows]
                claimed_placeholders = ",".join("?" for _ in claimed)
                conn.execute(
                    f"UPDATE subtitle_jobs SET state='processing',stage='discovering',"
                    f"next_check=?,lease_until=?,heartbeat_at=?,action_code='',updated_at=? "
                    f"WHERE video_path IN ({claimed_placeholders})",
                    (now + lease_seconds, now + lease_seconds, now, now, *claimed),
                )
        return rows

    def defer_subtitle_job(self, video_path: Path, error: str, delay_seconds: float) -> None:
        """Return a claimed job to pending without counting a media/preparation failure."""
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE subtitle_jobs SET state='pending',stage='retry_scheduled',priority=0,
                lease_until=0,next_check=?,last_error=?,updated_at=?
                WHERE video_path=?
                """,
                (now + delay_seconds, error[-1000:], now, str(video_path)),
            )
            conn.execute(
                "UPDATE episodes SET state='waiting_subtitles',updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )

    def postpone_subtitle_job(self, video_path: Path, error: str, delay_seconds: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE subtitle_jobs SET attempts=attempts+1,stage='retry_scheduled',priority=0,
                lease_until=0,next_check=?,last_error=?,updated_at=?
                WHERE video_path=?
                """,
                (time.time() + delay_seconds, error[-1000:], time.time(), str(video_path)),
            )
            conn.execute(
                "UPDATE subtitle_jobs SET state='pending' WHERE video_path=?",
                (str(video_path),),
            )
            conn.execute(
                "UPDATE episodes SET state='waiting_subtitles',updated_at=? WHERE video_path=?",
                (time.time(), str(video_path)),
            )

    def update_subtitle_job_stage(
        self,
        video_path: Path,
        stage: str,
        *,
        progress: dict[str, object] | None = None,
        lease_seconds: float = 25 * 60,
    ) -> None:
        """Persist worker progress and renew its processing lease."""
        now = time.time()
        with self.connect() as conn:
            conn.execute(
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

    def mark_subtitle_job_needs_action(
        self,
        video_path: Path,
        error: str,
        action_code: str,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
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
                "UPDATE episodes SET state='waiting_subtitles',updated_at=? WHERE video_path=?",
                (now, str(video_path)),
            )

    def priority_subtitle_job_count(self, *, min_priority: int = 200) -> int:
        """Count manual/high-priority subtitle jobs still awaiting an attempt."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM subtitle_jobs "
                "WHERE state IN ('pending','processing') AND priority>=?",
                (max(0, int(min_priority)),),
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def subtitle_jobs(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM subtitle_jobs ORDER BY next_check").fetchall()

    def reset_pending_subtitle_jobs(self) -> int:
        """Make pending jobs immediately due after a resolver/validation upgrade."""
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE subtitle_jobs SET next_check=?,updated_at=? WHERE state='pending'",
                (now, now),
            )
        return int(cursor.rowcount or 0)

    def upsert_download(self, item: DownloadItem) -> None:
        with self.connect() as conn:
            self._ensure_anime_parent(conn, item.media_id, item.name)
            conn.execute(
                """
                INSERT INTO downloads(
                    torrent_hash,name,state,progress,save_path,content_path,media_id,episode,media_episode,release_episode,
                    is_batch,added_on,completed_on,raw_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(torrent_hash) DO UPDATE SET
                    name=excluded.name,state=excluded.state,progress=excluded.progress,
                    save_path=excluded.save_path,content_path=excluded.content_path,
                    media_id=COALESCE(downloads.media_id,excluded.media_id),
                    episode=COALESCE(downloads.media_episode,excluded.media_episode,downloads.episode,excluded.episode),
                    media_episode=COALESCE(downloads.media_episode,excluded.media_episode,downloads.episode,excluded.episode),
                    release_episode=COALESCE(downloads.release_episode,excluded.release_episode),
                    is_batch=MAX(downloads.is_batch,excluded.is_batch),added_on=excluded.added_on,
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
                    json.dumps(item.raw, ensure_ascii=False),
                    time.time(),
                ),
            )

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
                    episode=(int(row["media_episode"]) if row["media_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
                    media_episode=(int(row["media_episode"]) if row["media_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
                    release_episode=(int(row["release_episode"]) if row["release_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
                    is_batch=bool(row["is_batch"]),
                    added_on=int(row["added_on"] or 0),
                    completed_on=int(row["completed_on"] or 0),
                    raw=json.loads(row["raw_json"] or "{}"),
                )
            )
        return result

    def prune_downloads(self, active_hashes: set[str]) -> None:
        with self.connect() as conn:
            if not active_hashes:
                conn.execute("DELETE FROM downloads")
                return
            placeholders = ",".join("?" for _ in active_hashes)
            conn.execute(
                f"DELETE FROM downloads WHERE torrent_hash NOT IN ({placeholders})",
                tuple(active_hashes),
            )

    def delete_torrent_records(self, torrent_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subtitle_jobs WHERE video_path IN (SELECT video_path FROM episodes WHERE torrent_hash=?)", (torrent_hash,))
            conn.execute("DELETE FROM episodes WHERE torrent_hash=?", (torrent_hash,))
            conn.execute("DELETE FROM downloads WHERE torrent_hash=?", (torrent_hash,))

    def attach_download_metadata(
        self,
        torrent_hash: str,
        media_id: int,
        episode: int | None,
        is_batch: bool,
        *,
        release_episode: int | None = None,
    ) -> None:
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id)
            conn.execute(
                "UPDATE downloads SET media_id=?,episode=?,media_episode=?,"
                "release_episode=COALESCE(?,release_episode),is_batch=?,updated_at=? WHERE torrent_hash=?",
                (media_id, episode, episode, release_episode, int(is_batch), time.time(), torrent_hash),
            )

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
    ) -> None:
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, title)
            conn.execute(
                "INSERT OR REPLACE INTO release_history("
                "info_hash,media_id,episode,media_episode,release_episode,title,score,selected_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    info_hash.lower(), media_id, episode, episode,
                    release_episode if release_episode is not None else episode,
                    title, score, time.time(),
                ),
            )

    def release_metadata_by_hash(
        self, torrent_hash: str
    ) -> tuple[int, int | None, int | None, float] | None:
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
            int(row["media_episode"]) if row["media_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None,
            int(row["release_episode"]) if row["release_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None,
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
            episode_clause = "COALESCE(media_episode,episode) IS NULL" if episode is None else "COALESCE(media_episode,episode)=?"
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

    def episode_for_torrent(
        self, media_id: int, episode: int, torrent_hash: str
    ) -> LibraryEpisode | None:
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
            row = conn.execute(
                "SELECT * FROM downloads WHERE torrent_hash=?", (torrent_hash,)
            ).fetchone()
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
            episode=(int(row["media_episode"]) if row["media_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
            media_episode=(int(row["media_episode"]) if row["media_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
            release_episode=(int(row["release_episode"]) if row["release_episode"] is not None else int(row["episode"]) if row["episode"] is not None else None),
            is_batch=bool(row["is_batch"]),
            added_on=int(row["added_on"] or 0),
            completed_on=int(row["completed_on"] or 0),
            raw=json.loads(row["raw_json"] or "{}"),
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
        with self.connect() as conn:
            self._ensure_anime_parent(conn, media_id, video_path.stem)
            cursor = conn.execute(
                """
                INSERT INTO subtitle_history(
                    video_path,media_id,episode,source,candidate_name,candidate_path,
                    score,status,reason,details_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
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
                    json.dumps(details or {}, ensure_ascii=False),
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
            result.append({
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
            })
        return result

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

    def latest_selected_subtitle_for_media_or_filename(
        self,
        *,
        video_path: Path,
        media_id: int | None,
        episode: int | None,
    ) -> dict[str, object] | None:
        """Find the most recent successful subtitle selection after path/brand moves.

        Brand migrations can move both the managed video root and cache root, so
        an exact historical ``video_path`` is not always available anymore.
        Prefer the current AniList identity and fall back to the exact video
        filename, which is stable across a root-directory rename.
        """
        clauses: list[tuple[str, tuple[object, ...]]] = [
            ("video_path=?", (str(video_path),)),
        ]
        if media_id is not None:
            if episode is None:
                clauses.append(("media_id=? AND episode IS NULL", (int(media_id),)))
            else:
                clauses.append(("media_id=? AND episode=?", (int(media_id), int(episode))))
        suffix = "%/" + video_path.name.replace("%", "\\%").replace("_", "\\_")
        clauses.append(("video_path LIKE ? ESCAPE '\\'", (suffix,)))

        with self.connect() as conn:
            row = None
            for where, args in clauses:
                row = conn.execute(
                    f"""
                    SELECT * FROM subtitle_history
                    WHERE ({where}) AND status IN ('selected','upgraded','manual','legacy')
                    ORDER BY created_at DESC,id DESC LIMIT 1
                    """,
                    args,
                ).fetchone()
                if row is not None:
                    break
        if row is None:
            return None
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
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
            "details": details if isinstance(details, dict) else {},
            "created_at": float(row["created_at"] or 0),
        }

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
            queues = conn.execute(
                "SELECT * FROM playlists ORDER BY updated_at DESC,id DESC"
            ).fetchall()
            rows = conn.execute(
                "SELECT * FROM playlist_items ORDER BY playlist_id,position,id"
            ).fetchall()
        grouped: dict[int, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(int(row["playlist_id"]), []).append({
                "id": int(row["id"]),
                "position": int(row["position"]),
                "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
                "episode": int(row["episode"]) if row["episode"] is not None else None,
                "video_path": str(row["video_path"]),
                "title": str(row["title"] or ""),
                "state": str(row["state"] or "pending"),
            })
        return [{
            "id": int(row["id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "media_id": int(row["media_id"]) if row["media_id"] is not None else None,
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "items": grouped.get(int(row["id"]), []),
        } for row in queues]

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

    def delete_playlist(self, playlist_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM playlists WHERE id=?", (int(playlist_id),))

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
        return [{
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
        } for row in rows]

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
                        candidate_resolved = Path(str(candidate["video_path"])).expanduser().resolve(strict=False)
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
                            "WHERE media_id=? AND COALESCE(media_episode,episode)=?",
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

    def delete_anime(self, media_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM anime WHERE media_id=?", (int(media_id),))

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

    def rating_prompted(self, media_id: int, episode: int) -> bool:
        return self.get_state(f"rating_prompted:{int(media_id)}:{int(episode)}", "") == "1"

    def mark_rating_prompted(self, media_id: int, episode: int) -> None:
        self.set_state(f"rating_prompted:{int(media_id)}:{int(episode)}", "1")

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
                  AND episode>?
                  AND (
                      state='watched'
                      OR watched_at IS NOT NULL
                  )
                """,
                (now, int(media_id), max(0, int(progress))),
            )
            return max(0, int(cursor.rowcount or 0))

    def repair_missing_cleanup_schedule(self, after_hours: float) -> int:
        """Repair watched rows whose cleanup timestamp was never persisted.

        The primary repair handles explicit ``state='watched'`` rows. A second
        conservative branch repairs recent managed episodes only when both
        AniList progress and recorded active playback prove the episode was
        watched. This avoids treating arbitrary local files as disposable.
        """
        delay_seconds = max(0.0, float(after_hours)) * 3600.0
        now = time.time()
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
            evidence = conn.execute(
                """
                UPDATE episodes
                SET state='watched',
                    watched_at=COALESCE(playback_updated_at, updated_at, downloaded_at, ?),
                    delete_after=COALESCE(playback_updated_at, updated_at, downloaded_at, ?) + ?,
                    playback_position=NULL,
                    updated_at=?
                WHERE id IN (
                    SELECT e.id
                    FROM episodes e
                    JOIN anime a ON a.media_id=e.media_id
                    WHERE e.state!='watched'
                      AND e.watched_at IS NULL
                      AND e.delete_after IS NULL
                      AND e.episode IS NOT NULL
                      AND e.episode<=a.progress
                      AND (e.torrent_hash!='' OR e.downloaded_at IS NOT NULL)
                      AND e.playback_duration IS NOT NULL
                      AND e.playback_duration>0
                      AND e.playback_active_seconds >=
                          CASE
                              WHEN e.playback_duration * 0.65 > 180
                              THEN e.playback_duration * 0.65
                              ELSE 180
                          END
                )
                """,
                (now, now, delay_seconds, now),
            )
            return max(0, int(explicit.rowcount or 0)) + max(0, int(evidence.rowcount or 0))

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
                "SELECT * FROM episodes WHERE delete_after IS NOT NULL AND delete_after<=?",
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
                        new["embedded_subtitle_id"] if new["embedded_subtitle_id"] is not None else old["embedded_subtitle_id"],
                        str(new["subtitle_origin"] or old["subtitle_origin"] or ""),
                        state,
                        str(new["torrent_hash"] or old["torrent_hash"] or ""),
                        new["downloaded_at"] if new["downloaded_at"] is not None else old["downloaded_at"],
                        new["watched_at"] if new["watched_at"] is not None else old["watched_at"],
                        new["delete_after"] if new["delete_after"] is not None else old["delete_after"],
                        playback_source["playback_position"],
                        max(float(old["playback_duration"] or 0.0), float(new["playback_duration"] or 0.0)) or None,
                        playback_source["playback_updated_at"],
                        max(float(old["playback_active_seconds"] or 0.0), float(new["playback_active_seconds"] or 0.0)),
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

            conn.execute("UPDATE subtitle_history SET video_path=? WHERE video_path=?", (new_value, old_value))

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

    def delete_episode_record(self, video_path: Path) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subtitle_jobs WHERE video_path=?", (str(video_path),))
            conn.execute("DELETE FROM episodes WHERE video_path=?", (str(video_path),))

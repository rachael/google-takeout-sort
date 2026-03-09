"""
SQLite database schema and access layer.

The database lives at <destination>/.takeout-sort/index.db and persists all
state so that every phase (download, index, organise) is fully resumable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per physical photo/video file discovered.
CREATE TABLE IF NOT EXISTS photos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Source location
    source_zip          TEXT,           -- NULL if already extracted
    source_path         TEXT NOT NULL UNIQUE,  -- path inside ZIP or on disk

    -- Identity / deduplication
    content_hash        TEXT,           -- SHA-256 hex digest (populated on index)
    file_size           INTEGER,

    -- Original filename components
    original_filename   TEXT NOT NULL,
    extension           TEXT NOT NULL,

    -- Timestamps (Unix seconds, from JSON metadata preferred over EXIF)
    taken_ts            INTEGER,
    creation_ts         INTEGER,

    -- Geo
    latitude            REAL,
    longitude           REAL,
    altitude            REAL,

    -- Rich metadata
    title               TEXT,
    description         TEXT,
    people              TEXT,           -- JSON-encoded list of names
    google_url          TEXT,
    is_edited           INTEGER NOT NULL DEFAULT 0,  -- boolean

    -- Raw Google JSON sidecar (stored for reference / future use)
    raw_json            TEXT,

    -- Output
    final_path          TEXT,           -- absolute path after organise phase

    -- Processing state
    status              TEXT NOT NULL DEFAULT 'discovered',
    -- discovered → indexed → organised
    -- 'skipped' if duplicate of an already-organised file

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_hash   ON photos (content_hash);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos (status);
CREATE INDEX IF NOT EXISTS idx_photos_taken  ON photos (taken_ts);

-- One row per Google Photos album discovered.
CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    location        TEXT,
    album_ts        INTEGER,    -- album-level date from metadata.json
    raw_json        TEXT,
    source_path     TEXT        -- directory the album was found in
);

-- Many-to-many: which photos belong to which albums.
CREATE TABLE IF NOT EXISTS photo_albums (
    photo_id    INTEGER NOT NULL REFERENCES photos (id) ON DELETE CASCADE,
    album_id    INTEGER NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
    PRIMARY KEY (photo_id, album_id)
);

-- Track each Takeout ZIP download.
CREATE TABLE IF NOT EXISTS downloads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    filename        TEXT,
    expected_bytes  INTEGER,
    local_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending → downloading → downloaded → extracting → extracted → error
    error_msg       TEXT,
    started_at      TEXT,
    finished_at     TEXT
);

-- Trigger to keep updated_at fresh on photos.
CREATE TRIGGER IF NOT EXISTS photos_updated_at
    AFTER UPDATE ON photos
    FOR EACH ROW
BEGIN
    UPDATE photos SET updated_at = datetime('now') WHERE id = NEW.id;
END;
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (and if necessary create) the SQLite database, applying the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for an explicit BEGIN/COMMIT/ROLLBACK transaction."""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Convenience query helpers
# ---------------------------------------------------------------------------

def upsert_photo(conn: sqlite3.Connection, **fields) -> int:
    """Insert a photo row; returns the row id."""
    cols = ", ".join(fields)
    placeholders = ", ".join("?" * len(fields))
    sql = f"INSERT OR IGNORE INTO photos ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if cur.lastrowid:
        return cur.lastrowid
    # Row already exists — fetch its id by source_path
    row = conn.execute(
        "SELECT id FROM photos WHERE source_path = ?", (fields["source_path"],)
    ).fetchone()
    return row["id"] if row else -1


def upsert_album(conn: sqlite3.Connection, **fields) -> int:
    """Insert an album row; returns the row id."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO albums (name, description, location, album_ts, raw_json, source_path) "
        "VALUES (:name, :description, :location, :album_ts, :raw_json, :source_path)",
        fields,
    )
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute(
        "SELECT id FROM albums WHERE name = ?", (fields["name"],)
    ).fetchone()
    return row["id"] if row else -1


def link_photo_album(conn: sqlite3.Connection, photo_id: int, album_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
        (photo_id, album_id),
    )


def get_photo_by_hash(conn: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM photos WHERE content_hash = ? AND status = 'organised' LIMIT 1",
        (content_hash,),
    ).fetchone()


def count_by_status(conn: sqlite3.Connection, table: str = "photos") -> dict[str, int]:
    assert table in ("photos", "albums"), f"Unexpected table: {table!r}"
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def iter_photos(
    conn: sqlite3.Connection,
    status: str | None = None,
    batch_size: int = 500,
):
    """Yield rows from the photos table, optionally filtered by status, in batches."""
    offset = 0
    while True:
        if status:
            rows = conn.execute(
                f"SELECT * FROM photos WHERE status = ? LIMIT {batch_size} OFFSET {offset}",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM photos LIMIT {batch_size} OFFSET {offset}",
            ).fetchall()
        if not rows:
            break
        yield from rows
        offset += batch_size


def iter_albums(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM albums").fetchall()


def get_album_photos(conn: sqlite3.Connection, album_id: int):
    return conn.execute(
        "SELECT p.* FROM photos p "
        "JOIN photo_albums pa ON pa.photo_id = p.id "
        "WHERE pa.album_id = ?",
        (album_id,),
    ).fetchall()


def get_photo_albums(conn: sqlite3.Connection, photo_id: int):
    return conn.execute(
        "SELECT a.* FROM albums a "
        "JOIN photo_albums pa ON pa.album_id = a.id "
        "WHERE pa.photo_id = ?",
        (photo_id,),
    ).fetchall()

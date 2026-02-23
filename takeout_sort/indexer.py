"""
Indexer: scan Google Takeout source directories into the SQLite database.

Handles both:
- Pre-extracted directories (user already unzipped everything)
- ZIP files that haven't been extracted yet (streaming extraction)

The indexer works in two passes:
  Pass 1 — discover all media files and their JSON sidecars, insert rows.
  Pass 2 — compute SHA-256 hashes and mark duplicates.

Both passes are resumable: rows already in the DB are skipped.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Callable

import sqlite3

from .db import (
    link_photo_album,
    open_db,
    transaction,
    upsert_album,
    upsert_photo,
)
from .metadata import parse_album_json, parse_google_json, PhotoMeta
from .utils import (
    MEDIA_EXTENSIONS,
    find_json_sidecar,
    is_json_sidecar,
    is_media_file,
    sanitise_folder_name,
    sha256,
    walk_files,
)

# The root folder name Google puts inside every Takeout ZIP.
_TAKEOUT_ROOT = "Takeout"
_PHOTOS_ROOT = "Google Photos"

# Name of the album-level metadata file.
_ALBUM_METADATA = "metadata.json"

# Folders that look like "Photos from YYYY" are the main library dump, not albums.
import re
_YEAR_FOLDER_RE = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)


def _is_year_folder(name: str) -> bool:
    return bool(_YEAR_FOLDER_RE.match(name))


def _looks_like_album(name: str) -> bool:
    return not _is_year_folder(name) and name not in {"", _TAKEOUT_ROOT, _PHOTOS_ROOT}


# ---------------------------------------------------------------------------
# In-memory parse of a Google Photos JSON sidecar found inside a ZIP
# ---------------------------------------------------------------------------

def _parse_json_bytes(data: bytes) -> dict:
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Extracted-directory indexing
# ---------------------------------------------------------------------------

def index_directory(
    source_dir: Path,
    conn: sqlite3.Connection,
    progress_cb: Callable[[str], None] | None = None,
) -> int:
    """
    Recursively index media files found under *source_dir*.

    Returns the number of new photos inserted.
    """
    inserted = 0

    # Walk the directory tree, collecting album folders along the way.
    # Structure we expect:
    #   source_dir/
    #     Takeout/Google Photos/<folder-name>/<media> + <media>.json
    #     (or the Google Photos folder may be the source_dir itself)

    photos_root = _find_photos_root(source_dir)
    if photos_root is None:
        photos_root = source_dir  # fall back: treat source as photos root

    for folder in sorted(photos_root.iterdir()):
        if not folder.is_dir():
            continue
        album_id: int | None = None

        # Check for album metadata.json
        meta_file = folder / _ALBUM_METADATA
        if _looks_like_album(folder.name) and meta_file.exists():
            album_meta = parse_album_json(meta_file)
            album_name = sanitise_folder_name(
                album_meta.get("title") or folder.name
            )
            album_ts_node = album_meta.get("date") or {}
            try:
                album_ts = int(album_ts_node.get("timestamp", 0)) or None
            except (ValueError, TypeError):
                album_ts = None

            with transaction(conn):
                album_id = upsert_album(
                    conn,
                    name=album_name,
                    description=album_meta.get("description", ""),
                    location=album_meta.get("location", ""),
                    album_ts=album_ts,
                    raw_json=json.dumps(album_meta),
                    source_path=str(folder),
                )

        # Index media files inside this folder
        for media_path in sorted(folder.iterdir()):
            if not media_path.is_file():
                continue
            if not is_media_file(media_path):
                continue

            if progress_cb:
                progress_cb(media_path.name)

            json_path = find_json_sidecar(media_path)
            meta: PhotoMeta | None = None
            raw_json_str: str | None = None
            if json_path:
                meta = parse_google_json(json_path)
                raw_json_str = json.dumps(meta.raw)

            photo_id = _insert_photo(
                conn,
                source_path=str(media_path),
                source_zip=None,
                filename=media_path.name,
                extension=media_path.suffix.lower(),
                meta=meta,
                raw_json=raw_json_str,
                file_size=media_path.stat().st_size,
            )
            if photo_id and photo_id > 0:
                inserted += 1

            if album_id and photo_id and photo_id > 0:
                with transaction(conn):
                    link_photo_album(conn, photo_id, album_id)

    return inserted


# ---------------------------------------------------------------------------
# ZIP-streaming indexer (for not-yet-extracted ZIPs)
# ---------------------------------------------------------------------------

def index_zip(
    zip_path: Path,
    conn: sqlite3.Connection,
    progress_cb: Callable[[str], None] | None = None,
) -> int:
    """
    Stream-index a single Google Takeout ZIP without fully extracting it first.

    This reads the central directory and small JSON sidecars into memory, but
    does NOT extract the large media files — they will be extracted later during
    the organise phase.

    Returns the number of new photos inserted.
    """
    inserted = 0
    zip_str = str(zip_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Build a quick lookup: name_inside_zip → ZipInfo
            info_map: dict[str, zipfile.ZipInfo] = {zi.filename: zi for zi in zf.infolist()}

            # Find all JSON sidecars and parse them ahead of time.
            json_map: dict[str, dict] = {}
            for zi in zf.infolist():
                if zi.filename.lower().endswith(".json") and zi.file_size < 1_000_000:
                    try:
                        data = zf.read(zi.filename)
                        parsed = _parse_json_bytes(data)
                        json_map[zi.filename] = parsed
                    except Exception:
                        pass

            # Now process media files.
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                name = zi.filename
                p = Path(name)
                if p.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                if progress_cb:
                    progress_cb(p.name)

                # Find corresponding JSON sidecar inside the ZIP.
                raw_json: dict = {}
                for candidate_name in _json_candidates_in_zip(name):
                    if candidate_name in json_map:
                        raw_json = json_map[candidate_name]
                        break

                meta = _dict_to_meta(raw_json)

                # Determine album membership from path.
                folder_name = _album_name_from_zip_path(name)
                album_id: int | None = None
                if folder_name and _looks_like_album(folder_name):
                    with transaction(conn):
                        album_id = upsert_album(
                            conn,
                            name=sanitise_folder_name(folder_name),
                            description="",
                            location="",
                            album_ts=None,
                            raw_json=None,
                            source_path=name,
                        )

                photo_id = _insert_photo(
                    conn,
                    source_path=name,
                    source_zip=zip_str,
                    filename=p.name,
                    extension=p.suffix.lower(),
                    meta=meta,
                    raw_json=json.dumps(raw_json) if raw_json else None,
                    file_size=zi.file_size,
                )
                if photo_id and photo_id > 0:
                    inserted += 1

                if album_id and photo_id and photo_id > 0:
                    with transaction(conn):
                        link_photo_album(conn, photo_id, album_id)

    except zipfile.BadZipFile:
        pass  # Corrupt or incomplete ZIP — skip

    return inserted


# ---------------------------------------------------------------------------
# Hash pass (pass 2): compute SHA-256 for all 'discovered' rows
# ---------------------------------------------------------------------------

def compute_hashes(
    conn: sqlite3.Connection,
    progress_cb: Callable[[str, int], None] | None = None,
    batch_size: int = 200,
) -> None:
    """
    Compute SHA-256 for every photo with status='discovered' that has been
    extracted to disk (source_zip IS NULL).

    When a duplicate is found, its album memberships are transferred to the
    canonical row before it is marked 'skipped'.  This ensures that a photo
    present in both a year-folder and an album folder retains its album
    membership on the single canonical row that will be organised.
    """
    rows = conn.execute(
        "SELECT id, source_path, source_zip FROM photos "
        "WHERE status = 'discovered' AND source_zip IS NULL"
    ).fetchall()

    # Seed with already-hashed canonical rows.
    hash_to_id: dict[str, int] = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT content_hash, id FROM photos WHERE content_hash IS NOT NULL AND status = 'indexed'"
        ).fetchall()
        if r[0]
    }

    for row in rows:
        photo_id = row["id"]
        path = Path(row["source_path"])
        if not path.exists():
            continue
        try:
            h = sha256(path)
        except OSError:
            continue

        if progress_cb:
            progress_cb(path.name, photo_id)

        if h in hash_to_id:
            canonical_id = hash_to_id[h]
            # Transfer album memberships to the canonical row so that the
            # organiser sees the complete album list.
            for link in conn.execute(
                "SELECT album_id FROM photo_albums WHERE photo_id = ?", (photo_id,)
            ).fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
                    (canonical_id, link["album_id"]),
                )
            conn.execute(
                "UPDATE photos SET content_hash = ?, status = 'skipped', updated_at = datetime('now') WHERE id = ?",
                (h, photo_id),
            )
        else:
            hash_to_id[h] = photo_id
            conn.execute(
                "UPDATE photos SET content_hash = ?, status = 'indexed', updated_at = datetime('now') WHERE id = ?",
                (h, photo_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_photos_root(base: Path) -> Path | None:
    """Walk up to two levels to find the 'Google Photos' folder."""
    candidate = base / _TAKEOUT_ROOT / _PHOTOS_ROOT
    if candidate.is_dir():
        return candidate
    candidate2 = base / _PHOTOS_ROOT
    if candidate2.is_dir():
        return candidate2
    # Maybe source_dir IS the Google Photos root
    for child in base.iterdir():
        if child.is_dir() and (
            _is_year_folder(child.name) or (child / _ALBUM_METADATA).exists()
        ):
            return base
    return None


def _json_candidates_in_zip(media_name: str) -> list[str]:
    """
    Generate candidate JSON sidecar names for a media file path inside a ZIP.
    Mirrors the logic in utils.find_json_sidecar.
    """
    from .utils import _JSON_STEM_MAX
    p = Path(media_name)
    stem = p.stem
    ext = p.suffix
    parent = str(p.parent)
    if parent == ".":
        parent = ""

    def full(s: str) -> str:
        return f"{parent}/{s}" if parent else s

    candidates = [full(f"{p.name}.json")]
    if len(stem) > _JSON_STEM_MAX:
        trunc = stem[:_JSON_STEM_MAX]
        candidates.append(full(f"{trunc}{ext}.json"))

    for pattern in (r"-[Ee]dited$", r"_[Ee]dited$"):
        base_stem = re.sub(pattern, "", stem)
        if base_stem != stem:
            candidates.append(full(f"{base_stem}{ext}.json"))
    return candidates


def _album_name_from_zip_path(zip_member_name: str) -> str | None:
    """
    Extract the containing folder name for a member path like:
        Takeout/Google Photos/My Album/photo.jpg → 'My Album'
        Takeout/Google Photos/Photos from 2023/photo.jpg → 'Photos from 2023'
    """
    parts = Path(zip_member_name).parts
    # Find "Google Photos" segment
    try:
        gp_idx = next(i for i, p in enumerate(parts) if p == _PHOTOS_ROOT)
    except StopIteration:
        # Fall back: second-to-last component
        if len(parts) >= 2:
            return parts[-2]
        return None
    if gp_idx + 1 < len(parts) - 1:
        return parts[gp_idx + 1]
    return None


def _dict_to_meta(raw: dict) -> PhotoMeta:
    """Convert a raw JSON dict (from a sidecar) to a PhotoMeta."""
    from .metadata import _parse_ts, _parse_geo

    return PhotoMeta(
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        taken_ts=_parse_ts(raw.get("photoTakenTime")),
        creation_ts=_parse_ts(raw.get("creationTime")),
        geo=_parse_geo(raw.get("geoData")) or _parse_geo(raw.get("geoDataExif")),
        people=[p["name"] for p in raw.get("people", []) if "name" in p],
        google_url=raw.get("url", ""),
        raw=raw,
    )


def _insert_photo(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    source_zip: str | None,
    filename: str,
    extension: str,
    meta: PhotoMeta | None,
    raw_json: str | None,
    file_size: int,
) -> int:
    """Insert a photo row (or skip if already present). Returns the row id."""
    m = meta or PhotoMeta()
    with transaction(conn):
        return upsert_photo(
            conn,
            source_zip=source_zip,
            source_path=source_path,
            content_hash=None,
            file_size=file_size,
            original_filename=filename,
            extension=extension,
            taken_ts=m.taken_ts,
            creation_ts=m.creation_ts,
            latitude=m.geo.latitude if m.geo else None,
            longitude=m.geo.longitude if m.geo else None,
            altitude=m.geo.altitude if m.geo else None,
            title=m.title or None,
            description=m.description or None,
            people=json.dumps(m.people) if m.people else None,
            google_url=m.google_url or None,
            is_edited=0,
            raw_json=raw_json,
            final_path=None,
            status="discovered",
        )

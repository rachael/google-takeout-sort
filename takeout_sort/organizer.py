"""
Organizer: move indexed photos into the final Library/Albums structure.

Directory layout produced
-------------------------
<destination>/
├── Library/
│   ├── 2023/
│   │   └── 03/
│   │       └── 15/
│   │           ├── IMG_1234.jpg
│   │           └── IMG_1234.xmp
│   └── No Date/
│       ├── unknown.jpg
│       └── unknown.xmp
└── Albums/
    └── Vacation Summer/
        ├── IMG_5678.jpg   ← hard link or symlink to Library copy
        └── IMG_5678.xmp   ← copy of XMP (cheap, always readable)

Space strategy
--------------
- Photos are *moved* (renamed/shutil.move) from the source into Library/.
- Album entries are hard links by default (zero extra bytes).
- If hard links are not possible (cross-fs or Windows without privileges),
  the script falls back to symlinks, then to copies.
- The --no-albums-in-library flag moves photos directly into Albums/ (the
  album folder becomes the primary location); Library/ contains only photos
  not in any album.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from .db import get_album_photos, get_photo_albums, iter_albums, iter_photos, transaction
from .metadata import PhotoMeta, apply_metadata, parse_google_json
from .utils import (
    LinkStrategy,
    choose_link_strategy,
    create_link,
    human_bytes,
    safe_move,
    sanitise_folder_name,
    sha256,
)


class FolderDepth(str, Enum):
    DAY = "day"       # YYYY/MM/DD
    MONTH = "month"   # YYYY/MM
    YEAR = "year"     # YYYY


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def organise(
    conn: sqlite3.Connection,
    destination: Path,
    *,
    depth: FolderDepth = FolderDepth.DAY,
    albums_in_library: bool = True,
    link_strategy: LinkStrategy | None = None,  # None → auto-detect
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> None:
    """
    Move/link all 'indexed' photos into Library/ and Albums/ under *destination*.

    Parameters
    ----------
    conn
        Open SQLite connection (DB must have been indexed already).
    destination
        Root output directory.
    depth
        Date-folder depth inside Library/.
    albums_in_library
        If True, every photo appears in Library/ regardless of album membership.
        If False, photos that belong to at least one album are placed ONLY in
        their album folder(s); Library/ only holds album-less photos.
    link_strategy
        Override the auto-detected link strategy (HARD/SYMLINK/COPY).
    progress_cb
        Called with (current_filename, current_index, total_count).
    """
    library_dir = destination / "Library"
    albums_dir = destination / "Albums"
    library_dir.mkdir(parents=True, exist_ok=True)
    albums_dir.mkdir(parents=True, exist_ok=True)

    # Collect all photos that still need organising.
    photos = conn.execute(
        "SELECT * FROM photos WHERE status IN ('indexed', 'discovered')"
    ).fetchall()
    total = len(photos)

    for idx, row in enumerate(photos, 1):
        if progress_cb:
            progress_cb(row["original_filename"], idx, total)

        _organise_photo(
            conn=conn,
            row=row,
            library_dir=library_dir,
            albums_dir=albums_dir,
            depth=depth,
            albums_in_library=albums_in_library,
            link_strategy=link_strategy,
        )

    # --- Album metadata pass: also write XMP for album-linked photos ---
    # (XMP sidecars in album folders point to the same metadata)
    _write_album_xmps(conn, albums_dir)


# ---------------------------------------------------------------------------
# Per-photo logic
# ---------------------------------------------------------------------------

def _organise_photo(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    library_dir: Path,
    albums_dir: Path,
    depth: FolderDepth,
    albums_in_library: bool,
    link_strategy: LinkStrategy | None,
) -> None:
    photo_id = row["id"]
    source_path = row["source_path"]
    source_zip = row["source_zip"]

    # Resolve the source file — may need to extract from ZIP first.
    src = _resolve_source(row, library_dir)
    if src is None:
        _set_status(conn, photo_id, "error")
        return

    # Build metadata
    meta = _row_to_meta(row)

    # Determine album membership
    album_rows = get_photo_albums(conn, photo_id)
    in_album = len(album_rows) > 0

    # Decide where the primary (moved) copy goes.
    if not albums_in_library and in_album:
        # Primary destination = first album folder
        primary_dir = _album_dir(albums_dir, album_rows[0]["name"])
    else:
        # Primary destination = Library/YYYY/MM/DD/
        primary_dir = _date_dir(library_dir, meta, depth)

    primary_dst = _unique_dest(primary_dir, row["original_filename"])

    # Move the file.
    try:
        safe_move(src, primary_dst)
    except Exception as e:
        _set_status(conn, photo_id, "error")
        return

    # Write metadata sidecar + embed EXIF.
    apply_metadata(primary_dst, meta)

    # Record final path.
    conn.execute(
        "UPDATE photos SET final_path = ?, status = 'organised', updated_at = datetime('now') WHERE id = ?",
        (str(primary_dst), photo_id),
    )
    conn.commit()

    # If we need album links AND the primary went to Library, create links in Albums/.
    if albums_in_library and in_album:
        _create_album_links(
            primary_dst, album_rows, albums_dir, link_strategy
        )
    elif not albums_in_library and in_album and len(album_rows) > 1:
        # Primary went to first album; link into remaining albums.
        for album_row in album_rows[1:]:
            _create_album_links(primary_dst, [album_row], albums_dir, link_strategy)

    # If --no-albums-in-library and photo has no album, it already went to Library.


def _create_album_links(
    primary_dst: Path,
    album_rows: list,
    albums_dir: Path,
    link_strategy: LinkStrategy | None,
) -> None:
    for album_row in album_rows:
        album_dir = _album_dir(albums_dir, album_row["name"])
        strategy = link_strategy or choose_link_strategy(primary_dst, album_dir)
        dst = _unique_dest(album_dir, primary_dst.name)
        try:
            create_link(primary_dst, dst, strategy, overwrite=False)
            # XMP sidecar in album folder: always copy (tiny, no space concern).
            xmp_src = primary_dst.with_suffix(".xmp")
            if xmp_src.exists():
                import shutil
                shutil.copy2(xmp_src, dst.with_suffix(".xmp"))
        except Exception:
            pass  # Non-fatal; the primary is already placed correctly.


# ---------------------------------------------------------------------------
# XMP-only pass for album folders (for photos whose primary is in Library)
# ---------------------------------------------------------------------------

def _write_album_xmps(conn: sqlite3.Connection, albums_dir: Path) -> None:
    """
    Ensure every album folder has XMP sidecars next to all its (linked) media.
    This is a no-op if they were already written during _create_album_links.
    """
    pass  # Already handled inline above.


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _date_dir(library_dir: Path, meta: PhotoMeta, depth: FolderDepth) -> Path:
    dt = meta.taken_dt
    if dt is None and meta.creation_ts is not None:
        dt = datetime.fromtimestamp(meta.creation_ts, tz=timezone.utc)

    if dt is None:
        return library_dir / "No Date"

    if depth == FolderDepth.YEAR:
        return library_dir / str(dt.year)
    if depth == FolderDepth.MONTH:
        return library_dir / f"{dt.year}" / f"{dt.month:02d}"
    # DAY
    return library_dir / f"{dt.year}" / f"{dt.month:02d}" / f"{dt.day:02d}"


def _album_dir(albums_dir: Path, album_name: str) -> Path:
    safe = sanitise_folder_name(album_name)
    d = albums_dir / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unique_dest(directory: Path, filename: str) -> Path:
    """
    Return a destination Path that doesn't conflict with existing files.

    If ``directory/filename`` already exists, appends ``_2``, ``_3``, …
    before the extension.
    """
    directory.mkdir(parents=True, exist_ok=True)
    dst = directory / filename
    if not dst.exists():
        return dst

    stem = dst.stem
    ext = dst.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Source resolution (may need to extract from ZIP)
# ---------------------------------------------------------------------------

def _resolve_source(row: sqlite3.Row, staging_dir: Path) -> Path | None:
    """
    Return the absolute path of the media file, extracting from ZIP if necessary.
    Returns None if the file cannot be found/extracted.
    """
    source_zip = row["source_zip"]
    source_path = row["source_path"]

    if source_zip is None:
        # Already on disk
        p = Path(source_path)
        return p if p.exists() else None

    # Need to extract from ZIP into a temporary staging location.
    zip_path = Path(source_zip)
    if not zip_path.exists():
        return None

    staging = staging_dir / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    extract_to = staging / Path(source_path).name

    if extract_to.exists():
        return extract_to  # Already extracted in a previous run

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            data = zf.read(source_path)
        extract_to.write_bytes(data)
        return extract_to
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


# ---------------------------------------------------------------------------
# Row → PhotoMeta
# ---------------------------------------------------------------------------

def _row_to_meta(row: sqlite3.Row) -> PhotoMeta:
    from .metadata import GeoPoint

    people: list[str] = []
    if row["people"]:
        try:
            people = json.loads(row["people"])
        except (json.JSONDecodeError, TypeError):
            pass

    geo = None
    if row["latitude"] is not None and row["longitude"] is not None:
        geo = GeoPoint(
            latitude=row["latitude"],
            longitude=row["longitude"],
            altitude=row["altitude"],
        )

    return PhotoMeta(
        title=row["title"] or "",
        description=row["description"] or "",
        taken_ts=row["taken_ts"],
        creation_ts=row["creation_ts"],
        geo=geo,
        people=people,
        google_url=row["google_url"] or "",
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _set_status(conn: sqlite3.Connection, photo_id: int, status: str) -> None:
    conn.execute(
        "UPDATE photos SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, photo_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Summary / stats
# ---------------------------------------------------------------------------

def print_summary(conn: sqlite3.Connection, destination: Path) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM photos GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}

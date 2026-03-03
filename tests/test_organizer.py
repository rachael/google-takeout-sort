"""
Tests for the organizer module.

We index a synthetic Takeout tree, then run organise() and assert that
files end up in the right places.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from takeout_sort.db import open_db
from takeout_sort.indexer import compute_hashes, index_directory
from takeout_sort.organizer import FolderDepth, organise


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_google_json(title: str, ts: int = 1672531200, lat: float = 0.0, lon: float = 0.0) -> str:
    return json.dumps({
        "title": title,
        "description": "Test photo",
        "photoTakenTime": {"timestamp": str(ts)},
        "creationTime": {"timestamp": str(ts)},
        "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
        "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        "people": [],
        "url": "https://photos.google.com/photo/test",
    })


def _make_album_json(title: str) -> str:
    return json.dumps({
        "title": title,
        "description": "",
        "access": "private",
        "date": {"timestamp": "1672531200"},
        "location": "",
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
    })


@pytest.fixture
def setup(tmp_path):
    """
    Creates:
      source/
        Takeout/Google Photos/
          Photos from 2023/
            solo.jpg + solo.jpg.json       ← not in any album
            shared.jpg + shared.jpg.json   ← also in an album
          My Album/
            metadata.json
            shared.jpg + shared.jpg.json
      dest/  ← where Library/ and Albums/ will be created
    """
    source = tmp_path / "source"
    year_dir = source / "Takeout" / "Google Photos" / "Photos from 2023"
    year_dir.mkdir(parents=True)

    (year_dir / "solo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    (year_dir / "solo.jpg.json").write_text(_make_google_json("solo.jpg", ts=1672531200))

    (year_dir / "shared.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 120)
    (year_dir / "shared.jpg.json").write_text(_make_google_json("shared.jpg", ts=1672531200))

    album_dir = source / "Takeout" / "Google Photos" / "My Album"
    album_dir.mkdir()
    (album_dir / "metadata.json").write_text(_make_album_json("My Album"))
    (album_dir / "shared.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 120)
    (album_dir / "shared.jpg.json").write_text(_make_google_json("shared.jpg", ts=1672531200))

    dest = tmp_path / "dest"
    dest.mkdir()

    conn = open_db(dest / ".takeout-sort" / "index.db")
    index_directory(source, conn)

    # Run the hash pass so that duplicates across year-folder and album are
    # detected and album memberships are transferred to the canonical row.
    compute_hashes(conn)

    # Any remaining 'discovered' rows (e.g. ZIPs not yet extracted) become
    # 'indexed' so the organiser picks them up.
    conn.execute("UPDATE photos SET status = 'indexed' WHERE status = 'discovered'")
    conn.commit()

    return source, dest, conn


# ---------------------------------------------------------------------------
# Basic organise tests
# ---------------------------------------------------------------------------

def test_organise_creates_library_dir(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    assert (dest / "Library").is_dir()


def test_organise_creates_albums_dir(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    assert (dest / "Albums").is_dir()


def test_organise_solo_photo_in_library(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    library = dest / "Library"
    all_jpgs = list(library.rglob("solo.jpg"))
    assert len(all_jpgs) == 1


def test_organise_writes_xmp_sidecar(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    library = dest / "Library"
    xmps = list(library.rglob("*.xmp"))
    assert len(xmps) >= 1


def test_organise_albums_in_library_true(setup):
    """With --albums-in-library, shared photo should appear in Library/."""
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    library = dest / "Library"
    album_folder = dest / "Albums" / "My Album"

    shared_in_library = list(library.rglob("shared.jpg"))
    shared_in_album = list(album_folder.rglob("shared.jpg")) if album_folder.exists() else []

    assert len(shared_in_library) == 1


def test_organise_albums_in_library_false(setup):
    """With --no-albums-in-library, shared photo should NOT appear in Library/."""
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=False)
    library = dest / "Library"
    shared_in_library = list(library.rglob("shared.jpg"))
    # shared.jpg is in an album, so it should not be in Library/
    assert len(shared_in_library) == 0


def test_organise_depth_day(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    library = dest / "Library"
    # ts=1672531200 → 2023-01-01
    expected = library / "2023" / "01" / "01"
    assert expected.is_dir()


def test_organise_depth_month(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.MONTH, albums_in_library=True)
    library = dest / "Library"
    expected = library / "2023" / "01"
    assert expected.is_dir()
    # Should NOT have day-level subfolder
    assert not (expected / "01").is_dir()


def test_organise_depth_year(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.YEAR, albums_in_library=True)
    library = dest / "Library"
    expected = library / "2023"
    assert expected.is_dir()


def test_organise_updates_status(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    rows = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE status = 'organised'"
    ).fetchone()
    assert rows[0] > 0


def test_organise_records_final_path(setup):
    source, dest, conn = setup
    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    rows = conn.execute(
        "SELECT final_path FROM photos WHERE status = 'organised'"
    ).fetchall()
    for row in rows:
        assert row["final_path"] is not None
        assert Path(row["final_path"]).exists()


def test_organise_xmp_includes_google_url_by_default(setup):
    """The Google Photos URL should appear in XMP sidecars by default."""
    source, dest, conn = setup
    # Re-index with a URL in the sidecar
    conn.execute(
        "UPDATE photos SET google_url = 'https://photos.google.com/photo/TESTURL' "
        "WHERE status = 'indexed'"
    )
    conn.commit()

    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True,
             include_google_metadata=True)

    xmps = list((dest / "Library").rglob("*.xmp"))
    assert xmps, "No XMP sidecars found"
    assert any("TESTURL" in x.read_text() for x in xmps)


def test_organise_xmp_excludes_google_url_with_flag(setup):
    """With include_google_metadata=False the URL must be absent from all XMP files."""
    source, dest, conn = setup
    conn.execute(
        "UPDATE photos SET google_url = 'https://photos.google.com/photo/SECRETURL' "
        "WHERE status = 'indexed'"
    )
    conn.commit()

    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True,
             include_google_metadata=False)

    xmps = list((dest / "Library").rglob("*.xmp"))
    assert xmps, "No XMP sidecars found"
    assert not any("SECRETURL" in x.read_text() for x in xmps)


def test_organise_progress_callback_is_called(setup):
    source, dest, conn = setup
    calls = []
    organise(
        conn, dest,
        depth=FolderDepth.DAY,
        albums_in_library=True,
        progress_cb=lambda name, idx, total: calls.append((name, idx, total)),
    )
    assert len(calls) > 0
    # Each call should have a non-empty filename and sensible index
    for name, idx, total in calls:
        assert name
        assert 1 <= idx <= total


def test_organise_no_date_photo(tmp_path):
    """A photo with no timestamp goes into Library/No Date/."""
    source = tmp_path / "source"
    year_dir = source / "Takeout" / "Google Photos" / "Photos from 2023"
    year_dir.mkdir(parents=True)
    (year_dir / "nodate.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
    # No JSON sidecar → no date

    dest = tmp_path / "dest"
    dest.mkdir()
    conn = open_db(dest / ".takeout-sort" / "index.db")
    index_directory(source, conn)
    conn.execute("UPDATE photos SET status = 'indexed' WHERE status = 'discovered'")
    conn.commit()

    organise(conn, dest, depth=FolderDepth.DAY, albums_in_library=True)
    no_date = dest / "Library" / "No Date" / "nodate.jpg"
    assert no_date.exists()

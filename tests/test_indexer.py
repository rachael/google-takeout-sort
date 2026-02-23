"""
Integration-style tests for the indexer.

We build a small synthetic Takeout directory tree, run the indexer,
and assert that the database contains the expected rows.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from takeout_sort.db import open_db
from takeout_sort.indexer import index_directory, index_zip


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_google_json(title: str, ts: int = 1672531200) -> str:
    return json.dumps({
        "title": title,
        "description": "",
        "photoTakenTime": {"timestamp": str(ts)},
        "creationTime": {"timestamp": str(ts)},
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        "people": [],
        "url": "",
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
def takeout_tree(tmp_path) -> Path:
    """
    Creates a minimal Google Photos takeout directory structure:

    tmp_path/
      Takeout/
        Google Photos/
          Photos from 2023/
            photo1.jpg
            photo1.jpg.json
            photo2.jpg            ← no sidecar (edge case)
          Vacation/
            metadata.json
            photo1.jpg            ← same file appears in album
            photo1.jpg.json
    """
    root = tmp_path / "Takeout" / "Google Photos"
    year_dir = root / "Photos from 2023"
    year_dir.mkdir(parents=True)

    # Library photo with sidecar
    (year_dir / "photo1.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)  # minimal JPEG
    (year_dir / "photo1.jpg.json").write_text(_make_google_json("photo1.jpg", 1672531200))

    # Library photo without sidecar
    (year_dir / "photo2.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    # Album
    album_dir = root / "Vacation"
    album_dir.mkdir()
    (album_dir / "metadata.json").write_text(_make_album_json("Vacation"))
    (album_dir / "photo1.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    (album_dir / "photo1.jpg.json").write_text(_make_google_json("photo1.jpg", 1672531200))

    return tmp_path


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / ".takeout-sort" / "index.db")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# index_directory tests
# ---------------------------------------------------------------------------

def test_index_directory_discovers_photos(takeout_tree, db):
    n = index_directory(takeout_tree, db)
    assert n >= 2  # at least photo1 and photo2 from the year folder

    rows = db.execute("SELECT * FROM photos").fetchall()
    filenames = {r["original_filename"] for r in rows}
    assert "photo1.jpg" in filenames
    assert "photo2.jpg" in filenames


def test_index_directory_creates_album(takeout_tree, db):
    index_directory(takeout_tree, db)
    albums = db.execute("SELECT * FROM albums").fetchall()
    album_names = {a["name"] for a in albums}
    assert "Vacation" in album_names


def test_index_directory_links_photo_to_album(takeout_tree, db):
    index_directory(takeout_tree, db)
    links = db.execute("SELECT * FROM photo_albums").fetchall()
    assert len(links) >= 1


def test_index_directory_parses_metadata(takeout_tree, db):
    index_directory(takeout_tree, db)
    row = db.execute(
        "SELECT * FROM photos WHERE original_filename = 'photo1.jpg' LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["taken_ts"] == 1672531200


def test_index_directory_handles_missing_sidecar(takeout_tree, db):
    """photo2.jpg has no sidecar — it should still be indexed."""
    index_directory(takeout_tree, db)
    row = db.execute(
        "SELECT * FROM photos WHERE original_filename = 'photo2.jpg'"
    ).fetchone()
    assert row is not None
    assert row["taken_ts"] is None  # no metadata available


# ---------------------------------------------------------------------------
# index_zip tests
# ---------------------------------------------------------------------------

def _make_takeout_zip(tmp_path: Path) -> Path:
    """Build a minimal Takeout ZIP in tmp_path."""
    zip_path = tmp_path / "takeout-001.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Year folder photo + sidecar
        zf.writestr(
            "Takeout/Google Photos/Photos from 2023/photo_zip.jpg",
            b"\xff\xd8\xff" + b"\x00" * 80,
        )
        zf.writestr(
            "Takeout/Google Photos/Photos from 2023/photo_zip.jpg.json",
            _make_google_json("photo_zip.jpg", 1680000000),
        )
        # Album photo
        zf.writestr(
            "Takeout/Google Photos/Road Trip/photo_zip.jpg",
            b"\xff\xd8\xff" + b"\x00" * 80,
        )
        zf.writestr(
            "Takeout/Google Photos/Road Trip/photo_zip.jpg.json",
            _make_google_json("photo_zip.jpg", 1680000000),
        )
        zf.writestr(
            "Takeout/Google Photos/Road Trip/metadata.json",
            _make_album_json("Road Trip"),
        )
    return zip_path


def test_index_zip_discovers_photos(tmp_path, db):
    zip_path = _make_takeout_zip(tmp_path)
    n = index_zip(zip_path, db)
    assert n >= 1
    rows = db.execute("SELECT * FROM photos").fetchall()
    filenames = {r["original_filename"] for r in rows}
    assert "photo_zip.jpg" in filenames


def test_index_zip_creates_album(tmp_path, db):
    zip_path = _make_takeout_zip(tmp_path)
    index_zip(zip_path, db)
    albums = db.execute("SELECT * FROM albums").fetchall()
    names = {a["name"] for a in albums}
    assert "Road Trip" in names


def test_index_zip_bad_zip(tmp_path, db):
    """A corrupt ZIP should be silently skipped."""
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"NOT A ZIP")
    n = index_zip(bad, db)
    assert n == 0

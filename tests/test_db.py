"""Tests for the database schema and helpers."""

import sqlite3
from pathlib import Path

import pytest

from takeout_sort.db import (
    count_by_status,
    get_album_photos,
    get_photo_albums,
    iter_albums,
    iter_photos,
    link_photo_album,
    open_db,
    transaction,
    upsert_album,
    upsert_photo,
)


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / ".takeout-sort" / "index.db")
    yield conn
    conn.close()


def test_open_db_creates_schema(db):
    tables = {
        r[0]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "photos" in tables
    assert "albums" in tables
    assert "photo_albums" in tables
    assert "downloads" in tables


def test_upsert_photo_returns_id(db):
    photo_id = upsert_photo(
        db,
        source_zip=None,
        source_path="/fake/photo.jpg",
        content_hash=None,
        file_size=1024,
        original_filename="photo.jpg",
        extension=".jpg",
        taken_ts=1672531200,
        creation_ts=None,
        latitude=None,
        longitude=None,
        altitude=None,
        title="Test",
        description=None,
        people=None,
        google_url=None,
        is_edited=0,
        raw_json=None,
        final_path=None,
        status="discovered",
    )
    assert photo_id > 0


def test_upsert_photo_idempotent(db):
    kwargs = dict(
        source_zip=None,
        source_path="/fake/photo2.jpg",
        content_hash=None,
        file_size=1024,
        original_filename="photo2.jpg",
        extension=".jpg",
        taken_ts=None,
        creation_ts=None,
        latitude=None,
        longitude=None,
        altitude=None,
        title=None,
        description=None,
        people=None,
        google_url=None,
        is_edited=0,
        raw_json=None,
        final_path=None,
        status="discovered",
    )
    db.commit()  # close any implicit transaction before first insert
    id1 = upsert_photo(db, **kwargs)
    db.commit()
    id2 = upsert_photo(db, **kwargs)
    db.commit()
    # source_path is UNIQUE so the second INSERT is ignored; upsert_photo
    # returns the existing row's id or our -1 sentinel — either way it must
    # not be a brand-new, different row.
    assert id1 == id2 or id2 == -1


def test_upsert_album_returns_id(db):
    album_id = upsert_album(
        db,
        name="Vacation 2023",
        description="Beach photos",
        location="Spain",
        album_ts=1672531200,
        raw_json="{}",
        source_path="/fake/album",
    )
    assert album_id > 0


def test_link_photo_album(db):
    photo_id = upsert_photo(
        db,
        source_zip=None,
        source_path="/fake/linked.jpg",
        content_hash=None,
        file_size=512,
        original_filename="linked.jpg",
        extension=".jpg",
        taken_ts=None,
        creation_ts=None,
        latitude=None,
        longitude=None,
        altitude=None,
        title=None,
        description=None,
        people=None,
        google_url=None,
        is_edited=0,
        raw_json=None,
        final_path=None,
        status="discovered",
    )
    album_id = upsert_album(
        db,
        name="Summer Album",
        description="",
        location="",
        album_ts=None,
        raw_json=None,
        source_path="/fake/summer",
    )
    db.commit()  # close any implicit transaction from the upserts above
    link_photo_album(db, photo_id, album_id)
    db.commit()

    albums = get_photo_albums(db, photo_id)
    assert len(albums) == 1
    assert albums[0]["name"] == "Summer Album"


def test_count_by_status(db):
    for path in ["/fake/a.jpg", "/fake/b.jpg"]:
        upsert_photo(
            db,
            source_zip=None,
            source_path=path,
            content_hash=None,
            file_size=1,
            original_filename=path.split("/")[-1],
            extension=".jpg",
            taken_ts=None,
            creation_ts=None,
            latitude=None,
            longitude=None,
            altitude=None,
            title=None,
            description=None,
            people=None,
            google_url=None,
            is_edited=0,
            raw_json=None,
            final_path=None,
            status="discovered",
        )
    stats = count_by_status(db)
    assert stats.get("discovered", 0) >= 2


def test_iter_photos(db):
    upsert_photo(
        db,
        source_zip=None,
        source_path="/fake/iter.jpg",
        content_hash=None,
        file_size=1,
        original_filename="iter.jpg",
        extension=".jpg",
        taken_ts=None,
        creation_ts=None,
        latitude=None,
        longitude=None,
        altitude=None,
        title=None,
        description=None,
        people=None,
        google_url=None,
        is_edited=0,
        raw_json=None,
        final_path=None,
        status="discovered",
    )
    rows = list(iter_photos(db, status="discovered", batch_size=10))
    assert len(rows) >= 1


def test_iter_photos_no_matches_returns_empty(db):
    rows = list(iter_photos(db, status="organised"))
    assert rows == []


def test_iter_albums_returns_all(db):
    for name in ("Album A", "Album B"):
        upsert_album(
            db, name=name, description="", location="",
            album_ts=None, raw_json=None, source_path=f"/fake/{name}",
        )
    db.commit()
    albums = list(iter_albums(db))
    names = {a["name"] for a in albums}
    assert "Album A" in names
    assert "Album B" in names


def test_get_album_photos_returns_all_in_album(db):
    """get_album_photos should return every photo linked to an album."""
    # Insert two photos and one album
    def _photo(path):
        return upsert_photo(
            db, source_zip=None, source_path=path, content_hash=None,
            file_size=1, original_filename=path.split("/")[-1], extension=".jpg",
            taken_ts=None, creation_ts=None, latitude=None, longitude=None,
            altitude=None, title=None, description=None, people=None,
            google_url=None, is_edited=0, raw_json=None, final_path=None,
            status="discovered",
        )

    pid1 = _photo("/fake/a.jpg")
    pid2 = _photo("/fake/b.jpg")
    db.commit()

    album_id = upsert_album(
        db, name="Test Album", description="", location="",
        album_ts=None, raw_json=None, source_path="/fake/album",
    )
    db.commit()
    link_photo_album(db, pid1, album_id)
    link_photo_album(db, pid2, album_id)
    db.commit()

    photos = get_album_photos(db, album_id)
    assert len(photos) == 2


def test_count_by_status_empty_table(db):
    result = count_by_status(db)
    assert result == {}


def test_transaction_rolls_back_on_exception(db):
    """The transaction() context manager should rollback if an exception is raised."""
    db.commit()
    try:
        with transaction(db):
            db.execute(
                "INSERT INTO photos (source_path, original_filename, extension, status) "
                "VALUES ('/tx/rollback.jpg', 'rollback.jpg', '.jpg', 'discovered')"
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    row = db.execute(
        "SELECT id FROM photos WHERE source_path = '/tx/rollback.jpg'"
    ).fetchone()
    assert row is None, "Row should have been rolled back"


def test_upsert_album_unique_name_is_idempotent(db):
    """Upserting the same album name twice should return the same id."""
    kwargs = dict(
        name="Unique Album", description="", location="",
        album_ts=None, raw_json=None, source_path="/fake/unique",
    )
    db.commit()
    id1 = upsert_album(db, **kwargs)
    db.commit()
    id2 = upsert_album(db, **kwargs)
    db.commit()
    assert id1 == id2 or id2 == -1

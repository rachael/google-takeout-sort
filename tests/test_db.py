"""Tests for the database schema and helpers."""

import sqlite3
from pathlib import Path

import pytest

from takeout_sort.db import (
    count_by_status,
    get_photo_albums,
    iter_photos,
    link_photo_album,
    open_db,
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

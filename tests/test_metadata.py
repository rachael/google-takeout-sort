"""Tests for metadata parsing, XMP sidecar writing, and EXIF embedding."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from takeout_sort.metadata import (
    GeoPoint,
    PhotoMeta,
    apply_metadata,
    parse_album_json,
    parse_google_json,
    write_xmp_sidecar,
)


# ---------------------------------------------------------------------------
# parse_google_json
# ---------------------------------------------------------------------------

SAMPLE_JSON = {
    "title": "IMG_1234.jpg",
    "description": "A sunny day",
    "photoTakenTime": {"timestamp": "1672531200", "formatted": "Jan 1, 2023, 12:00:00 AM UTC"},
    "creationTime": {"timestamp": "1672531210", "formatted": "Jan 1, 2023, 12:00:10 AM UTC"},
    "geoData": {
        "latitude": 48.8566,
        "longitude": 2.3522,
        "altitude": 35.0,
        "latitudeSpan": 0.001,
        "longitudeSpan": 0.001,
    },
    "geoDataExif": {
        "latitude": 0.0,
        "longitude": 0.0,
        "altitude": 0.0,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    },
    "people": [{"name": "Alice"}, {"name": "Bob"}],
    "url": "https://photos.google.com/photo/abc123",
}


def test_parse_google_json_full(tmp_path):
    p = tmp_path / "photo.jpg.json"
    p.write_text(json.dumps(SAMPLE_JSON))
    meta = parse_google_json(p)

    assert meta.title == "IMG_1234.jpg"
    assert meta.description == "A sunny day"
    assert meta.taken_ts == 1672531200
    assert meta.creation_ts == 1672531210
    assert meta.geo is not None
    assert abs(meta.geo.latitude - 48.8566) < 1e-4
    assert abs(meta.geo.longitude - 2.3522) < 1e-4
    assert meta.geo.altitude == 35.0
    assert meta.people == ["Alice", "Bob"]
    assert "abc123" in meta.google_url


def test_parse_google_json_prefers_geodata_over_geodataexif(tmp_path):
    """geoData (user-corrected) should win over geoDataExif (0,0)."""
    p = tmp_path / "photo.jpg.json"
    p.write_text(json.dumps(SAMPLE_JSON))
    meta = parse_google_json(p)
    assert meta.geo.latitude == pytest.approx(48.8566, abs=1e-4)


def test_parse_google_json_missing_geo(tmp_path):
    data = dict(SAMPLE_JSON)
    data["geoData"] = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    data["geoDataExif"] = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    p = tmp_path / "photo.jpg.json"
    p.write_text(json.dumps(data))
    meta = parse_google_json(p)
    assert meta.geo is None


def test_parse_google_json_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("NOT JSON {{{")
    meta = parse_google_json(p)
    assert meta.title == ""
    assert meta.taken_ts is None


def test_parse_google_json_missing_file(tmp_path):
    meta = parse_google_json(tmp_path / "nonexistent.json")
    assert meta.taken_ts is None


# ---------------------------------------------------------------------------
# PhotoMeta helpers
# ---------------------------------------------------------------------------

def test_photometa_taken_dt():
    meta = PhotoMeta(taken_ts=1672531200)
    dt = meta.taken_dt
    assert dt is not None
    assert dt.year == 2023
    assert dt.month == 1
    assert dt.day == 1


def test_photometa_best_ts_prefers_taken():
    meta = PhotoMeta(taken_ts=100, creation_ts=200)
    assert meta.best_ts == 100


def test_photometa_best_ts_falls_back():
    meta = PhotoMeta(taken_ts=None, creation_ts=200)
    assert meta.best_ts == 200


# ---------------------------------------------------------------------------
# write_xmp_sidecar
# ---------------------------------------------------------------------------

def test_write_xmp_sidecar_creates_file(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(
        title="Test Photo",
        description="A description",
        taken_ts=1672531200,
        geo=GeoPoint(latitude=48.8566, longitude=2.3522, altitude=35.0),
        people=["Alice"],
        google_url="https://photos.google.com/photo/abc",
    )
    xmp = write_xmp_sidecar(photo, meta)
    assert xmp.exists()
    assert xmp.suffix == ".xmp"


def test_write_xmp_sidecar_contains_date(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(taken_ts=1672531200)
    xmp = write_xmp_sidecar(photo, meta)
    content = xmp.read_text()
    assert "2023" in content


def test_write_xmp_sidecar_contains_geo(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(
        taken_ts=1672531200,
        geo=GeoPoint(latitude=48.8566, longitude=2.3522),
    )
    xmp = write_xmp_sidecar(photo, meta)
    content = xmp.read_text()
    assert "GPSLatitude" in content
    assert "GPSLongitude" in content


def test_write_xmp_sidecar_escapes_xml(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(title="<Test & 'Photo'>")
    xmp = write_xmp_sidecar(photo, meta)
    content = xmp.read_text()
    assert "<Test" not in content  # raw < must be escaped
    assert "&lt;" in content or "Test" in content


def test_write_xmp_sidecar_no_geo(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(taken_ts=1672531200, geo=None)
    xmp = write_xmp_sidecar(photo, meta)
    content = xmp.read_text()
    assert "GPSLatitude" not in content


def test_write_xmp_sidecar_includes_google_url_by_default(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(google_url="https://photos.google.com/photo/abc123")
    xmp = write_xmp_sidecar(photo, meta)
    assert "abc123" in xmp.read_text()


def test_write_xmp_sidecar_excludes_google_url_when_flag_false(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(google_url="https://photos.google.com/photo/abc123")
    xmp = write_xmp_sidecar(photo, meta, include_google_metadata=False)
    assert "abc123" not in xmp.read_text()


def test_write_xmp_sidecar_standard_fields_present_regardless_of_flag(tmp_path):
    """Date, GPS, title, description must appear even with --no-google-metadata."""
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(
        title="My Photo",
        taken_ts=1672531200,
        geo=GeoPoint(latitude=48.8566, longitude=2.3522),
        google_url="https://photos.google.com/photo/abc123",
    )
    xmp = write_xmp_sidecar(photo, meta, include_google_metadata=False)
    content = xmp.read_text()
    assert "2023" in content            # date present
    assert "GPSLatitude" in content     # geo present
    assert "My Photo" in content        # title present
    assert "abc123" not in content      # URL absent


# ---------------------------------------------------------------------------
# PhotoMeta edge cases
# ---------------------------------------------------------------------------

def test_photometa_taken_dt_none():
    meta = PhotoMeta(taken_ts=None)
    assert meta.taken_dt is None


def test_photometa_best_ts_both_none():
    meta = PhotoMeta(taken_ts=None, creation_ts=None)
    assert meta.best_ts is None


# ---------------------------------------------------------------------------
# parse_album_json
# ---------------------------------------------------------------------------

def test_parse_album_json_basic(tmp_path):
    data = {
        "title": "Summer Holiday",
        "description": "Beach pics",
        "access": "private",
        "date": {"timestamp": "1672531200"},
        "location": "Spain",
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
    }
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps(data))
    result = parse_album_json(p)
    assert result["title"] == "Summer Holiday"
    assert result["location"] == "Spain"


def test_parse_album_json_missing_file(tmp_path):
    result = parse_album_json(tmp_path / "nonexistent.json")
    assert result == {}


def test_parse_album_json_corrupt(tmp_path):
    p = tmp_path / "metadata.json"
    p.write_text("NOT JSON")
    result = parse_album_json(p)
    assert result == {}


# ---------------------------------------------------------------------------
# embed_exif
# ---------------------------------------------------------------------------

def test_embed_exif_unsupported_format_returns_false(tmp_path):
    from takeout_sort.metadata import embed_exif
    # PNG is not supported by piexif
    png = tmp_path / "photo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    meta = PhotoMeta(taken_ts=1672531200)
    result = embed_exif(png, meta)
    assert result is False


def test_embed_exif_heic_returns_false(tmp_path):
    from takeout_sort.metadata import embed_exif
    heic = tmp_path / "photo.heic"
    heic.write_bytes(b"FAKE HEIC DATA")
    meta = PhotoMeta(taken_ts=1672531200)
    result = embed_exif(heic, meta)
    assert result is False


# ---------------------------------------------------------------------------
# apply_metadata
# ---------------------------------------------------------------------------

def test_apply_metadata_creates_xmp(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(taken_ts=1672531200)
    apply_metadata(photo, meta)
    assert photo.with_suffix(".xmp").exists()


def test_apply_metadata_no_google_metadata_flag(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    meta = PhotoMeta(
        taken_ts=1672531200,
        google_url="https://photos.google.com/photo/secret",
    )
    apply_metadata(photo, meta, include_google_metadata=False)
    xmp = photo.with_suffix(".xmp")
    assert xmp.exists()
    assert "secret" not in xmp.read_text()

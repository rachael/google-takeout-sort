"""Tests for metadata parsing, XMP sidecar writing, and EXIF embedding."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from takeout_sort.metadata import (
    GeoPoint,
    PhotoMeta,
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

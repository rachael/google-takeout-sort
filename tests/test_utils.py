"""Tests for utility helpers."""

import os
from pathlib import Path

import pytest

from takeout_sort.utils import (
    find_json_sidecar,
    human_bytes,
    is_json_sidecar,
    is_media_file,
    sanitise_folder_name,
    sha256,
)


# ---------------------------------------------------------------------------
# human_bytes
# ---------------------------------------------------------------------------

def test_human_bytes_bytes():
    assert human_bytes(512) == "512.0 B"


def test_human_bytes_kb():
    assert "KB" in human_bytes(2048)


def test_human_bytes_gb():
    assert "GB" in human_bytes(2 * 1024 ** 3)


# ---------------------------------------------------------------------------
# sanitise_folder_name
# ---------------------------------------------------------------------------

def test_sanitise_removes_illegal():
    assert "<bad>" not in sanitise_folder_name("<bad>")


def test_sanitise_truncates():
    long_name = "a" * 300
    assert len(sanitise_folder_name(long_name)) <= 200


def test_sanitise_empty_fallback():
    assert sanitise_folder_name("   ") == "Unnamed"


def test_sanitise_strips_trailing_dots():
    result = sanitise_folder_name("folder...")
    assert not result.endswith(".")


# ---------------------------------------------------------------------------
# is_media_file / is_json_sidecar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["photo.jpg", "clip.mp4", "raw.heic", "img.JPEG"])
def test_is_media_file_true(name):
    assert is_media_file(Path(name))


@pytest.mark.parametrize("name", ["notes.txt", "metadata.json", "photo.jpg.json"])
def test_is_media_file_false(name):
    assert not is_media_file(Path(name))


@pytest.mark.parametrize("name", ["photo.jpg.json", "clip.mp4.json", "img.heic.json"])
def test_is_json_sidecar_true(name):
    assert is_json_sidecar(Path(name))


@pytest.mark.parametrize("name", ["photo.jpg", "metadata.json", "album.json"])
def test_is_json_sidecar_false(name):
    assert not is_json_sidecar(Path(name))


# ---------------------------------------------------------------------------
# find_json_sidecar
# ---------------------------------------------------------------------------

def test_find_json_sidecar_exact(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"FAKE")
    sidecar = tmp_path / "photo.jpg.json"
    sidecar.write_text("{}")
    assert find_json_sidecar(photo) == sidecar


def test_find_json_sidecar_truncated(tmp_path):
    # Stem longer than 46 chars
    long_stem = "a" * 60
    photo = tmp_path / f"{long_stem}.jpg"
    photo.write_bytes(b"FAKE")
    # Google truncates to 46 chars
    sidecar = tmp_path / f"{'a' * 46}.jpg.json"
    sidecar.write_text("{}")
    assert find_json_sidecar(photo) == sidecar


def test_find_json_sidecar_edited(tmp_path):
    photo = tmp_path / "photo-edited.jpg"
    photo.write_bytes(b"FAKE")
    sidecar = tmp_path / "photo.jpg.json"
    sidecar.write_text("{}")
    assert find_json_sidecar(photo) == sidecar


def test_find_json_sidecar_not_found(tmp_path):
    photo = tmp_path / "orphan.jpg"
    photo.write_bytes(b"FAKE")
    assert find_json_sidecar(photo) is None


# ---------------------------------------------------------------------------
# sha256
# ---------------------------------------------------------------------------

def test_sha256(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00" * 1024)
    h = sha256(f)
    assert len(h) == 64  # hex SHA-256
    # Same content → same hash
    assert h == sha256(f)

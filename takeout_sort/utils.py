"""
Cross-platform utility helpers.

Covers:
- OS / filesystem detection
- Disk-space queries and pre-flight checks
- Hard-link / symlink / copy fallback chain
- SHA-256 file hashing (streamed, large-file safe)
- Google Takeout JSON-to-photo filename matching
- Safe file-move with rollback
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# OS / filesystem detection
# ---------------------------------------------------------------------------

class OSFamily(Enum):
    MACOS = auto()
    WINDOWS = auto()
    LINUX = auto()
    UNKNOWN = auto()


def detect_os() -> OSFamily:
    s = platform.system()
    if s == "Darwin":
        return OSFamily.MACOS
    if s == "Windows":
        return OSFamily.WINDOWS
    if s == "Linux":
        return OSFamily.LINUX
    return OSFamily.UNKNOWN


def supports_hard_links(src: Path, dst_dir: Path) -> bool:
    """
    Return True if src and dst_dir are on the same filesystem (hard links will work).
    On Windows hard links work but only within the same volume and require specific
    privileges; we perform a quick probe instead of relying on stat.
    """
    try:
        src_dev = src.stat().st_dev
        dst_dev = dst_dir.stat().st_dev if dst_dir.exists() else dst_dir.parent.stat().st_dev
        return src_dev == dst_dev
    except OSError:
        return False


def supports_symlinks() -> bool:
    """
    Windows requires Developer Mode or elevated privileges for symlinks.
    Probe at runtime.
    """
    if sys.platform != "win32":
        return True
    try:
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "a.txt"
            lnk = Path(td) / "b.txt"
            src.write_text("x")
            os.symlink(src, lnk)
            return True
    except (OSError, NotImplementedError):
        return False


# ---------------------------------------------------------------------------
# Disk-space helpers
# ---------------------------------------------------------------------------

@dataclass
class SpaceInfo:
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1_073_741_824

    @property
    def total_gb(self) -> float:
        return self.total_bytes / 1_073_741_824


def get_space(path: Path) -> SpaceInfo:
    """Return disk space information for the filesystem containing *path*."""
    usage = shutil.disk_usage(str(path) if path.exists() else str(path.parent))
    return SpaceInfo(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


def check_space(
    required_bytes: int,
    path: Path,
    label: str = "operation",
) -> tuple[bool, SpaceInfo]:
    """
    Check whether *path*'s filesystem has at least *required_bytes* free.

    Returns ``(ok, space_info)``.
    """
    info = get_space(path)
    # Keep a 512 MB safety buffer
    ok = info.free_bytes >= required_bytes + 512 * 1024 * 1024
    return ok, info


def human_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}"
        n = int(n / 1024.0)
    return f"{n:,.1f} PB"


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Return the hex SHA-256 digest of a file (streamed in 1 MiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Google Takeout JSON sidecar filename matching
# ---------------------------------------------------------------------------

# Photo file extensions that Takeout exports.
PHOTO_EXTENSIONS = frozenset(
    ".jpg .jpeg .png .gif .bmp .webp .tiff .tif "
    ".heic .heif .avif .raw .cr2 .nef .arw .dng".split()
)
VIDEO_EXTENSIONS = frozenset(
    ".mp4 .mov .avi .mkv .3gp .m4v .wmv .flv .webm".split()
)
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Maximum base-filename length Google uses when naming .json sidecars.
_JSON_STEM_MAX = 46


def find_json_sidecar(media_path: Path) -> Path | None:
    """
    Return the Path of the Google Takeout JSON sidecar for *media_path*, or None.

    Google names the sidecar ``<stem><ext>.json``, but truncates ``<stem>`` to
    46 characters when the full stem is longer.  The truncation happens *before*
    any ``(N)`` duplicate suffix.

    Examples
    --------
    photo.jpg                    → photo.jpg.json
    a_very_long_...name.jpg      → a_very_long_...nam.jpg.json  (46-char stem)
    IMG_1234(1).jpg              → IMG_1234(1).jpg.json
    photo-edited.jpg             → photo-edited.jpg.json
    """
    parent = media_path.parent
    name = media_path.name      # e.g. "photo.jpg"
    stem = media_path.stem      # e.g. "photo"
    ext = media_path.suffix     # e.g. ".jpg"

    candidates = [
        parent / f"{name}.json",  # exact match first
    ]

    # Truncated stem variant
    if len(stem) > _JSON_STEM_MAX:
        trunc = stem[:_JSON_STEM_MAX]
        candidates.append(parent / f"{trunc}{ext}.json")

    # Some edited files: sidecar is named after the original (strip -edited / _edited)
    for pattern in (r"-[Ee]dited$", r"_[Ee]dited$"):
        base_stem = re.sub(pattern, "", stem)
        if base_stem != stem:
            candidates.append(parent / f"{base_stem}{ext}.json")
            if len(base_stem) > _JSON_STEM_MAX:
                trunc = base_stem[:_JSON_STEM_MAX]
                candidates.append(parent / f"{trunc}{ext}.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


# ---------------------------------------------------------------------------
# Safe file operations with hard-link / symlink / copy fallback
# ---------------------------------------------------------------------------

class LinkStrategy(Enum):
    HARD = "hard"
    SYMLINK = "symlink"
    COPY = "copy"


def choose_link_strategy(src: Path, dst_dir: Path) -> LinkStrategy:
    """Choose the best available strategy for creating an album entry."""
    if supports_hard_links(src, dst_dir):
        return LinkStrategy.HARD
    if supports_symlinks():
        return LinkStrategy.SYMLINK
    return LinkStrategy.COPY


def create_link(
    src: Path,
    dst: Path,
    strategy: LinkStrategy,
    overwrite: bool = False,
) -> None:
    """
    Create *dst* pointing at / copying *src* using the given strategy.

    Raises FileExistsError if *dst* exists and *overwrite* is False.
    """
    if dst.exists() or dst.is_symlink():
        if not overwrite:
            raise FileExistsError(f"{dst} already exists")
        dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)

    if strategy == LinkStrategy.HARD:
        os.link(src, dst)
    elif strategy == LinkStrategy.SYMLINK:
        dst.symlink_to(src.resolve())
    else:
        shutil.copy2(src, dst)


def safe_move(src: Path, dst: Path) -> None:
    """
    Move *src* to *dst*, creating parent directories as needed.

    If *src* and *dst* are on different filesystems shutil.move will copy then
    delete; on same filesystem it's a rename (atomic, no extra space used).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


# ---------------------------------------------------------------------------
# Path sanitisation (for album folder names from Google)
# ---------------------------------------------------------------------------

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[ .]+$")


def sanitise_folder_name(name: str, max_length: int = 200) -> str:
    """
    Make *name* safe to use as a folder name on all target platforms.

    - Replaces illegal characters with underscores.
    - Strips leading/trailing dots and spaces (Windows limitation).
    - Truncates to *max_length* characters.
    """
    safe = _ILLEGAL_CHARS.sub("_", name)
    safe = _TRAILING.sub("", safe).strip()
    return safe[:max_length] or "Unnamed"


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def walk_files(root: Path, extensions: frozenset[str] | None = None):
    """Yield all files under *root*, optionally filtered by extension."""
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            p = Path(dirpath) / fname
            if extensions is None or p.suffix.lower() in extensions:
                yield p


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def is_json_sidecar(path: Path) -> bool:
    """
    Return True if *path* looks like a Google Takeout JSON sidecar.

    Sidecar names end in ``.<ext>.json`` where ``<ext>`` is a media extension.
    """
    name = path.name.lower()
    if not name.endswith(".json"):
        return False
    # strip the trailing .json and check what's left
    remainder = name[:-5]
    for ext in MEDIA_EXTENSIONS:
        if remainder.endswith(ext):
            return True
    return False

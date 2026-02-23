"""
Metadata parsing, XMP sidecar generation, and EXIF embedding.

Pipeline for each photo
-----------------------
1. Parse Google's JSON sidecar → structured PhotoMeta dataclass.
2. Write an XMP sidecar file (``<stem>.xmp``) next to the organised photo.
3. Embed date + GPS into the photo's EXIF (JPEG/TIFF only) via piexif,
   leaving the pixel data unchanged.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

# piexif and Pillow are optional at module-level so the rest of the tool
# remains importable even without them (tests, indexer, etc.).
try:
    import piexif
    from PIL import Image

    _PIEXIF_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIEXIF_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GeoPoint:
    latitude: float
    longitude: float
    altitude: float | None = None


@dataclass
class PhotoMeta:
    title: str = ""
    description: str = ""
    taken_ts: int | None = None       # Unix seconds, preferred source for date
    creation_ts: int | None = None    # Unix seconds
    geo: GeoPoint | None = None
    people: list[str] = field(default_factory=list)
    google_url: str = ""
    is_edited: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def taken_dt(self) -> datetime | None:
        if self.taken_ts is not None:
            return datetime.fromtimestamp(self.taken_ts, tz=timezone.utc)
        return None

    @property
    def best_ts(self) -> int | None:
        """photoTakenTime is more accurate; fall back to creationTime."""
        return self.taken_ts if self.taken_ts is not None else self.creation_ts


# ---------------------------------------------------------------------------
# JSON sidecar parsing
# ---------------------------------------------------------------------------

def _parse_ts(node: dict | None) -> int | None:
    if not node:
        return None
    try:
        return int(node["timestamp"])
    except (KeyError, ValueError, TypeError):
        return None


def _parse_geo(node: dict | None) -> GeoPoint | None:
    if not node:
        return None
    try:
        lat = float(node.get("latitude", 0))
        lon = float(node.get("longitude", 0))
        alt = node.get("altitude")
        if lat == 0.0 and lon == 0.0:
            return None
        return GeoPoint(latitude=lat, longitude=lon, altitude=float(alt) if alt else None)
    except (ValueError, TypeError):
        return None


def parse_google_json(json_path: Path) -> PhotoMeta:
    """
    Parse a Google Takeout JSON sidecar and return a PhotoMeta.

    Tolerates missing fields gracefully.
    """
    try:
        raw: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return PhotoMeta()

    taken_ts = _parse_ts(raw.get("photoTakenTime"))
    creation_ts = _parse_ts(raw.get("creationTime"))

    # Prefer geoData (user-corrected) over geoDataExif (device-reported)
    geo = _parse_geo(raw.get("geoData")) or _parse_geo(raw.get("geoDataExif"))

    people = [p["name"] for p in raw.get("people", []) if "name" in p]

    title = raw.get("title", "")
    description = raw.get("description", "")
    google_url = raw.get("url", "")

    return PhotoMeta(
        title=title,
        description=description,
        taken_ts=taken_ts,
        creation_ts=creation_ts,
        geo=geo,
        people=people,
        google_url=google_url,
        raw=raw,
    )


def parse_album_json(json_path: Path) -> dict[str, Any]:
    """Parse an album-level metadata.json."""
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw


# ---------------------------------------------------------------------------
# XMP sidecar generation
# ---------------------------------------------------------------------------

_XMP_TEMPLATE = """\
<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description
        rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/"
        xmlns:exif="http://ns.adobe.com/exif/1.0/"
        xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
        xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
        xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
    >{datetime_block}{title_block}{description_block}{geo_block}{people_block}{url_block}
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""


def _datetime_block(dt: datetime | None) -> str:
    if dt is None:
        return ""
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    return f"\n        xmp:CreateDate=\"{iso}\"\n        exif:DateTimeOriginal=\"{iso}\""


def _geo_block(geo: GeoPoint | None) -> str:
    if geo is None:
        return ""
    lat_ref = "N" if geo.latitude >= 0 else "S"
    lon_ref = "E" if geo.longitude >= 0 else "W"
    lines = [
        f'\n        exif:GPSLatitude="{abs(geo.latitude):.7f}N"' if lat_ref == "N"
        else f'\n        exif:GPSLatitude="{abs(geo.latitude):.7f}S"',
        f'\n        exif:GPSLongitude="{abs(geo.longitude):.7f}E"' if lon_ref == "E"
        else f'\n        exif:GPSLongitude="{abs(geo.longitude):.7f}W"',
    ]
    if geo.altitude is not None:
        lines.append(f'\n        exif:GPSAltitude="{geo.altitude:.1f}/1"')
        lines.append('\n        exif:GPSAltitudeRef="0"')
    return "".join(lines)


def _text_block(tag: str, value: str) -> str:
    if not value:
        return ""
    return f"\n      <{tag}>{xml_escape(value)}</{tag}>"


def _people_block(people: list[str]) -> str:
    if not people:
        return ""
    items = "".join(f"\n          <rdf:li>{xml_escape(p)}</rdf:li>" for p in people)
    return (
        "\n      <Iptc4xmpCore:SubjectCode>"
        f"\n        <rdf:Bag>{items}\n        </rdf:Bag>"
        "\n      </Iptc4xmpCore:SubjectCode>"
    )


def write_xmp_sidecar(photo_path: Path, meta: PhotoMeta) -> Path:
    """
    Write a ``.xmp`` sidecar file next to *photo_path* and return its path.

    The sidecar uses the same stem as the photo: ``photo.jpg`` → ``photo.xmp``.
    """
    xmp_path = photo_path.with_suffix(".xmp")

    dt_block = _datetime_block(meta.taken_dt)
    title_b = ""
    desc_b = ""
    if meta.title:
        title_b = (
            "\n      <dc:title>\n        <rdf:Alt>"
            f"\n          <rdf:li xml:lang='x-default'>{xml_escape(meta.title)}</rdf:li>"
            "\n        </rdf:Alt>\n      </dc:title>"
        )
    if meta.description:
        desc_b = (
            "\n      <dc:description>\n        <rdf:Alt>"
            f"\n          <rdf:li xml:lang='x-default'>{xml_escape(meta.description)}</rdf:li>"
            "\n        </rdf:Alt>\n      </dc:description>"
        )

    url_b = ""
    if meta.google_url:
        url_b = (
            "\n      <dc:source>"
            f"{xml_escape(meta.google_url)}"
            "</dc:source>"
        )

    xmp_content = _XMP_TEMPLATE.format(
        datetime_block=dt_block,
        title_block=title_b,
        description_block=desc_b,
        geo_block=_geo_block(meta.geo),
        people_block=_people_block(meta.people),
        url_block=url_b,
    )

    xmp_path.write_text(xmp_content, encoding="utf-8")
    return xmp_path


# ---------------------------------------------------------------------------
# EXIF embedding
# ---------------------------------------------------------------------------

def _deg_to_rational(value: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Convert a decimal degree to an EXIF rational tuple (degrees, minutes, seconds)."""
    abs_val = abs(value)
    deg = int(abs_val)
    min_ = int((abs_val - deg) * 60)
    sec = round(((abs_val - deg) * 60 - min_) * 60 * 1000)
    return (deg, 1), (min_, 1), (sec, 1000)


def embed_exif(photo_path: Path, meta: PhotoMeta) -> bool:
    """
    Embed date/GPS from *meta* into the EXIF of a JPEG or TIFF file.

    Returns True on success, False if the file format is not supported or
    piexif/Pillow are not installed.  Does NOT modify the pixel data.
    """
    if not _PIEXIF_AVAILABLE:
        return False

    suffix = photo_path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".tiff", ".tif"}:
        return False  # HEIC/PNG/etc. not supported via piexif

    try:
        exif_dict = piexif.load(str(photo_path))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    changed = False

    # --- DateTime ---
    dt = meta.taken_dt
    if dt is not None:
        dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = dt_str
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeDigitized] = dt_str
        exif_dict.setdefault("0th", {})[piexif.ImageIFD.DateTime] = dt_str
        changed = True

    # --- GPS ---
    geo = meta.geo
    if geo is not None:
        gps_ifd: dict = {}
        gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = b"N" if geo.latitude >= 0 else b"S"
        gps_ifd[piexif.GPSIFD.GPSLatitude] = _deg_to_rational(geo.latitude)
        gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = b"E" if geo.longitude >= 0 else b"W"
        gps_ifd[piexif.GPSIFD.GPSLongitude] = _deg_to_rational(geo.longitude)
        if geo.altitude is not None:
            gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = b"\x00"
            gps_ifd[piexif.GPSIFD.GPSAltitude] = (
                int(abs(geo.altitude) * 100), 100
            )
        exif_dict["GPS"] = gps_ifd
        changed = True

    if not changed:
        return False

    try:
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(photo_path))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Convenience: apply all metadata to an organised photo
# ---------------------------------------------------------------------------

def apply_metadata(photo_path: Path, meta: PhotoMeta) -> None:
    """Write XMP sidecar and attempt EXIF embedding for *photo_path*."""
    write_xmp_sidecar(photo_path, meta)
    embed_exif(photo_path, meta)

# takeout-sort — Specification

> Implementation notes, quirks, and design rationale live in [`CLAUDE.md`](CLAUDE.md).
> This document specifies *what the system does and must do*, not how it is built.

---

## 1. Purpose

`takeout-sort` takes a Google Photos library exported via Google Takeout and
produces a clean, metadata-rich local library organised by date, with album
membership preserved as hard links, symlinks, or copies.

### What it does not do

- It does **not** delete anything from Google Photos.  That is a separate,
  explicit manual step (`scripts/delete_from_google_photos.py`).  See
  §8 (Safety Constraints).
- It does **not** modify the original Takeout ZIPs or the extracted source
  files beyond moving them into the output tree.
- It does **not** require a Google API key or OAuth token.

---

## 2. Prerequisites

1. The user has requested a Google Takeout export of Google Photos and received
   download links via email.
2. Either Playwright + Chromium is installed (for automated download), or the
   ZIPs have been downloaded manually.
3. Python 3.10+ is installed.  Optional dependencies: `playwright`, `piexif`,
   `Pillow` (EXIF embedding only).

---

## 3. Output directory structure

```
<destination>/
├── .takeout-sort/
│   └── index.db          — SQLite state database (see §6)
├── downloads/            — raw downloaded ZIPs (may be deleted per --zip-cleanup)
├── raw/                  — extracted Takeout folder tree
├── Library/
│   ├── 2023/
│   │   ├── 03/
│   │   │   └── 15/
│   │   │       ├── IMG_1234.jpg
│   │   │       └── IMG_1234.xmp
│   │   └── …
│   └── No Date/          — photos with no recoverable timestamp
└── Albums/
    └── Summer Holiday/
        ├── IMG_5678.jpg  — hard link / symlink / copy of Library/ entry
        └── IMG_5678.xmp  — copy of the XMP sidecar (cheap; always readable)
```

### Folder depth inside Library/

Controlled by `--depth`:

| Value  | Path pattern             |
|--------|--------------------------|
| `day`  | `Library/YYYY/MM/DD/`    |
| `month`| `Library/YYYY/MM/`       |
| `year` | `Library/YYYY/`          |

Default: `day`.

### --no-albums-in-library mode

When `--no-albums-in-library` is set:
- Photos that belong to at least one album are placed **only** in `Albums/`
  (first album is the primary location; additional albums receive links/copies).
- `Library/` holds only photos not in any album.
- Photos with no album always go to `Library/` regardless of this flag.

---

## 4. Pipeline phases

### Phase 1 — Download

Automate clicking every "Download" link on the Google Takeout downloads page
and save each ZIP to `<destination>/downloads/`.

Three authentication modes:

| Mode      | Mechanism                                                                 |
|-----------|---------------------------------------------------------------------------|
| `browser` | Open a visible Chromium window; user logs in; automation takes over.      |
| `attach`  | Attach to a running Chrome started with `--remote-debugging-port=<port>`. |
| `js`      | Print a JS snippet for the user to paste into the browser console.        |

Each downloaded file is recorded in the `downloads` table with status
`downloaded`.  Phase 1 is idempotent: a ZIP already present on disk is not
re-downloaded.

After each ZIP lands in `downloads/`, it is extracted to `raw/` (unless
`--zip-cleanup=keep`).  A disk-space pre-flight check runs before each
extraction; if space is tight the user is offered interactive options (see §7).

### Phase 2 — Index

Walk the source tree (extracted directories and/or unextracted ZIPs) and
record every media file in the `photos` table.

**Two-pass design:**

1. **Discover** — fast walk; insert one row per file with `status='discovered'`
   and `content_hash=NULL`.
2. **Hash / dedup** — compute SHA-256 for every on-disk file.  If two files
   share a hash, the second is marked `status='skipped'` and its album
   memberships are transferred to the canonical row.  Canonical row gets
   `status='indexed'`.

Files inside unextracted ZIPs are indexed without extraction (stream the ZIP
central directory) and kept as `status='discovered'` until organise time,
when they are extracted on-demand into a `.staging/` directory.

**Critical:** `compute_hashes()` must complete before `organise()` runs.  The
album-membership transfer during dedup is what makes `--no-albums-in-library`
work correctly.  See [`CLAUDE.md §Google Takeout format`](CLAUDE.md).

### Phase 3 — Organise

Process every photo with `status IN ('indexed', 'discovered')`:

1. Resolve source path (extract from ZIP if needed).
2. Compute destination path (`Library/YYYY/MM/DD/` or album dir).
3. `safe_move()` source → destination.  On failure: set `status='error'`,
   skip.
4. Immediately write `status='organised'` and `final_path` to DB.
5. Best-effort: write XMP sidecar + embed EXIF (see §5).
6. Best-effort: create album links/copies.

Steps 5 and 6 are explicitly best-effort.  A failure in either does **not**
roll back the move or change the status.  The photo is already at its
destination; metadata can be reconstructed.

Organise is idempotent: rows already `status='organised'` or `status='skipped'`
are not touched.

---

## 5. Metadata

### XMP sidecar

A `.xmp` sidecar is written next to every organised photo using the same stem:
`photo.jpg` → `photo.xmp`.

Fields written:

| XMP field                        | Source                                  |
|----------------------------------|-----------------------------------------|
| `xmp:CreateDate`                 | `photoTakenTime` (preferred) or `creationTime` |
| `exif:DateTimeOriginal`          | Same                                    |
| `exif:GPSLatitude/Longitude`     | `geoData` (preferred) or `geoDataExif` |
| `exif:GPSAltitude`               | Same                                    |
| `dc:title`                       | JSON `title`                            |
| `dc:description`                 | JSON `description`                      |
| `Iptc4xmpCore:SubjectCode`       | JSON `people[].name`                    |
| `dc:source`                      | Google Photos URL (`--no-google-metadata` omits this) |

`--no-google-metadata` omits only `dc:source`.  All other fields are always
written.  The URL is always stored in the SQLite DB regardless of this flag.

### EXIF embedding

Date and GPS are also written directly into the photo file's EXIF (JPEG/TIFF
only) via `piexif`.  This is a best-effort no-op for HEIC, PNG, MOV, and any
format `piexif` does not support.  Pixel data is never modified.

### Metadata precedence

`photoTakenTime` > `creationTime` > no date (→ `Library/No Date/`).
`geoData` (user-corrected in Google Photos) > `geoDataExif` (device-reported).

### Sidecar filename rules (Google Takeout quirks)

See [`CLAUDE.md §JSON sidecar naming`](CLAUDE.md) for the full rules.
Summary: stems are truncated to 46 characters; edited photos strip `-edited` /
`_edited` suffixes to find the sidecar.

---

## 6. Database schema

Location: `<destination>/.takeout-sort/index.db`
Engine: SQLite with WAL journal mode and foreign keys enabled.

### `photos` table (one row per physical file)

| Column            | Type    | Notes                                               |
|-------------------|---------|-----------------------------------------------------|
| `id`              | INTEGER | Primary key                                         |
| `source_zip`      | TEXT    | NULL if already extracted                           |
| `source_path`     | TEXT    | **UNIQUE** — path inside ZIP or on disk             |
| `content_hash`    | TEXT    | SHA-256; NULL until `compute_hashes()` runs         |
| `original_filename` | TEXT  |                                                     |
| `extension`       | TEXT    |                                                     |
| `taken_ts`        | INTEGER | Unix seconds; NULL if unknown                       |
| `creation_ts`     | INTEGER | Unix seconds; NULL if unknown                       |
| `latitude`        | REAL    |                                                     |
| `longitude`       | REAL    |                                                     |
| `altitude`        | REAL    |                                                     |
| `title`           | TEXT    |                                                     |
| `description`     | TEXT    |                                                     |
| `people`          | TEXT    | JSON-encoded `["Name", …]`                          |
| `google_url`      | TEXT    | Always stored; only omitted from XMP if `--no-google-metadata` |
| `is_edited`       | INTEGER | Boolean                                             |
| `raw_json`        | TEXT    | Full original Google JSON sidecar                   |
| `final_path`      | TEXT    | Absolute path after organise                        |
| `status`          | TEXT    | `discovered` → `indexed` → `organised`; or `skipped` / `error` |

### Status lifecycle

```
discovered  — file seen, no hash yet
    ↓ compute_hashes()
indexed     — hashed, unique, ready to organise
skipped     — duplicate of an indexed file (albums transferred to canonical)
    ↓ organise()
organised   — moved to final_path; XMP + EXIF written
error       — move failed; source may no longer exist
```

### `albums` table

One row per Google Photos album found in the export.  `name` is UNIQUE.

### `photo_albums` table

Many-to-many join.  `(photo_id, album_id)` is the primary key.
Inserts use `INSERT OR IGNORE` (idempotent).

### `downloads` table

Tracks each Takeout ZIP URL through `pending → downloading → downloaded →
extracting → extracted → error`.

---

## 7. CLI reference

```
takeout-sort [OPTIONS] COMMAND [ARGS]
```

### Commands

#### `run <destination>`
Download + index + organise in one shot.  `<destination>` is created if it
does not exist.

#### `download <destination>`
Phase 1 only.  Downloads ZIPs to `<destination>/downloads/` and extracts them
to `<destination>/raw/`.  Prints the next-step command on completion.

#### `organize <source> [destination]`
Index + organise a pre-downloaded (or pre-extracted) export.  `<destination>`
defaults to `<source>` if omitted.

#### `status <destination>`
Print a Rich table of photo counts by status and remaining free disk space.

### Shared flags

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `takeout.google.com/…` | Takeout download page URL |
| `--auth browser\|attach\|js` | `browser` | Authentication mode for download |
| `--cdp-port N` | `9222` | CDP port for `--auth=attach` |
| `--depth day\|month\|year` | `day` | Date subfolder depth in `Library/` |
| `--albums-in-library / --no-albums-in-library` | albums included | Album photo placement |
| `--zip-cleanup immediate\|after\|keep` | `immediate` | When to delete source ZIPs |
| `--headless` | off | Run Playwright in headless mode |
| `--google-metadata / --no-google-metadata` | included | Include `dc:source` URL in XMP |
| `--db <path>` | `<dest>/.takeout-sort/index.db` | Custom database path |
| `--version` | — | Print version and exit |

### `--no-google-metadata` confirmation

Because omitting the Google URL from sidecars is irreversible once the
database is deleted, this flag triggers an interactive three-choice prompt:

1. Continue — strip the URL from sidecars (keep the flag).
2. Include — override the flag for this run.
3. Abort.

Non-interactive contexts (piped stdin) exit with status 1.  There is no
`--yes` / `--force` bypass.

### Low disk space handling

When `check_space_for_zip()` detects insufficient space before extracting a
ZIP, the user is offered:

1. Switch to `--zip-cleanup=immediate` (delete processed ZIPs as we go).
2. Skip this ZIP and continue.
3. Abort and resume later with `takeout-sort organize`.

---

## 8. Safety constraints

### Cloud deletion

`scripts/delete_from_google_photos.py` is a **standalone manual tool**.  No
`takeout-sort` CLI command calls it as a side effect.

Safeguards that must never be weakened:
- The user must type exactly `"yes"` to proceed; any other input aborts.
- EOF / non-interactive input (CI/pipes) aborts with a clear message.
- `--dry-run` enforced by `continue` before any browser action — no code path
  bypasses it.

**Future agents and contributors: do not add `--force`, `--yes`, or any other
bypass to this script.**  Do not call it from any automated pipeline.  If you
want to preview what would be deleted, always run with `--dry-run` first and
show the output to the user.

### Local file safety

- Moves use `os.rename` (atomic on POSIX, same filesystem) or
  `shutil.move` (copy+delete, cross-filesystem).  There is no silent overwrite;
  `_unique_dest()` appends `_2`, `_3`, … suffixes to avoid collisions.
- ZIP deletion is the only irreversible local operation, and it is gated
  behind the explicit `--zip-cleanup` flag.
- The DB status is written to `'organised'` immediately after a successful
  move, before any subsequent operations.  Post-move failures (sidecar, EXIF,
  album links) are best-effort and do not affect the photo's location or
  status record.

---

## 9. Resumability

Every phase is fully resumable:

- **Download:** already-downloaded ZIPs are detected by filename; re-running
  does not re-download.
- **Index:** `source_path` has a UNIQUE constraint; re-indexing the same file
  is a no-op (`INSERT OR IGNORE`).
- **Organise:** only rows with `status IN ('indexed', 'discovered')` are
  processed; `organised` and `skipped` rows are untouched.

If the process crashes mid-organise, re-running `takeout-sort organize` (or
`run`) picks up from where it left off.

---

## 10. Cross-platform behaviour

| Capability      | Detection method                                  | Fallback     |
|-----------------|---------------------------------------------------|--------------|
| Hard links      | `st_dev` equality (same filesystem)               | Symlink      |
| Symlinks        | Runtime probe (`os.symlink` in temp dir)          | Copy         |
| EXIF embedding  | `piexif` import + `.jpg`/`.tiff` extension check  | XMP only     |

The tool runs on macOS, Linux, and Windows.  On Windows, hard links require
same-volume placement; symlinks require Developer Mode or administrator
privileges.

---

## 11. Extension and media type support

**Photos:** `.jpg .jpeg .png .gif .bmp .webp .tiff .tif .heic .heif .avif
.raw .cr2 .nef .arw .dng`

**Videos:** `.mp4 .mov .avi .mkv .3gp .m4v .wmv .flv .webm`

EXIF embedding (`piexif`) applies only to `.jpg .jpeg .tiff .tif`.
All other types receive XMP sidecars only.

---

## 12. References

- [`CLAUDE.md`](CLAUDE.md) — implementation notes, non-obvious design decisions,
  common pitfalls, and agent knowledge log.  Read this before modifying any
  module.
- [`pyproject.toml`](pyproject.toml) — package metadata, dependencies, entry
  points.
- [`scripts/delete_from_google_photos.py`](scripts/delete_from_google_photos.py)
  — standalone post-migration cleanup tool.  See §8.

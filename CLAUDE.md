# CLAUDE.md — Agent notes for google-takeout-sort

Persistent notes for Claude Code agents working on this repository.
Update this file whenever you discover something non-obvious.

---

## Project overview

`takeout-sort` is a Python CLI tool that organises a Google Photos library
exported via Google Takeout into a clean `Library/` + `Albums/` structure,
preserving all metadata in XMP sidecar files and EXIF.

**Entry point:** `takeout_sort/cli.py` → `main()` (click group)
**Package installed as:** `takeout-sort` (see `pyproject.toml`)
**Test runner:** `pytest` (65 → 91 tests as of this writing)
**Python:** 3.10+

---

## Google Takeout format — critical quirks

### 1. Same photo appears twice: year-folder AND album folder

The most important fact about Takeout exports:

```
Takeout/Google Photos/
  Photos from 2023/
    beach.jpg          ← copy 1 (library dump)
    beach.jpg.json
  Summer Holiday/
    beach.jpg          ← copy 2 (album copy, identical bytes)
    beach.jpg.json
    metadata.json      ← album-level metadata
```

This means the indexer will create **two rows** for `beach.jpg` with different
`source_path` values. The `compute_hashes()` function detects this, marks one
as `status='skipped'`, and — critically — **transfers the album membership
from the skipped row to the canonical row**.  Without this transfer,
`--no-albums-in-library` would never work because the year-folder copy has
no album membership in the DB.

**`compute_hashes()` MUST run before `organise()`.**  The test fixture in
`test_organizer.py` explicitly calls `compute_hashes()` before marking any
rows as `indexed`.

### 2. JSON sidecar naming: 46-character truncation

Google truncates the base filename (stem) to **46 characters** when naming
the `.json` sidecar.  `utils.find_json_sidecar()` and `indexer._json_candidates_in_zip()`
both implement this.

```
VeryLongFilenameHere...AAAA.jpg  →  sidecar: VeryLongFilenameHere...A.jpg.json
                                                          └─ 46 chars ─┘
```

The constant `_JSON_STEM_MAX = 46` lives in `utils.py`.

### 3. Edited photos

Photos edited in Google Photos are exported as `photo-edited.jpg` alongside
the original `photo.jpg`.  The edited file's sidecar is named after the
**original** filename.  `find_json_sidecar()` strips `-edited` / `_edited`
suffixes to find the sidecar.

### 4. `metadata.json` distinguishes album folders from year-folders

Year folders (`Photos from YYYY`) do **not** have a `metadata.json`.
Album folders do.  The regex `_YEAR_FOLDER_RE` in `indexer.py` also matches
year-folder names to avoid treating them as albums.

### 5. Google Photos API no longer supports deletion (2024)

The delete script (`scripts/delete_from_google_photos.py`) uses Playwright
to automate the web UI instead.  The keyboard shortcut `Shift+3` ('#') moves
a photo to trash when viewing it on `photos.google.com`.

---

## Database design decisions

### `source_path` has a UNIQUE constraint

Without it, `INSERT OR IGNORE` silently inserts duplicates and the idempotency
tests fail.  The constraint is on `source_path` (not content hash) so two
copies of the same photo at different paths are correctly recorded as separate
rows before deduplication.

### `content_hash` is NULL until `compute_hashes()` runs

Photos start with `status='discovered'` and `content_hash=NULL`.  After
`compute_hashes()`:
- Unique files → `status='indexed'`, `content_hash=<sha256>`
- Duplicates  → `status='skipped'`, `content_hash=<sha256>` (album links transferred)

The organiser only processes `status IN ('indexed', 'discovered')`.

### `photo_albums` uses `INSERT OR IGNORE`

Album-membership links are safe to insert multiple times (idempotent).

---

## SQLite / Python transaction gotcha

Python's `sqlite3` module opens an **implicit transaction** after any DML
statement (INSERT, UPDATE, DELETE) unless `isolation_level=None`.  Calling
`conn.execute("BEGIN")` when an implicit transaction is already open raises:

```
sqlite3.OperationalError: cannot start a transaction within a transaction
```

**Fix in tests:** call `db.commit()` after upserts before doing anything
that needs a fresh transaction.  The `transaction()` context manager in
`db.py` uses explicit `BEGIN` so it should only be used at the top level,
not nested inside implicit transactions.

---

## `--no-google-metadata` flag

- Only affects **XMP sidecar files** — the `dc:source` (Google Photos URL)
  field is omitted.
- The URL is **always stored in the SQLite database** regardless of this flag.
- `scripts/delete_from_google_photos.py` reads from the DB, not XMP, so it
  still works as long as the database exists.
- The CLI prompts for confirmation (with three choices) when this flag is
  passed, because losing the URL from the sidecar is irreversible once the
  database is deleted.

The flag is threaded as:
```
cli.py (--no-google-metadata)
  → _confirm_no_google_metadata()   ← interactive prompt
  → _phase_organise(include_google_metadata=...)
    → organise(include_google_metadata=...)
      → _organise_photo(include_google_metadata=...)
        → apply_metadata(include_google_metadata=...)
          → write_xmp_sidecar(include_google_metadata=...)
```

---

## Hard links, symlinks, and copies

Album entries are created via `utils.create_link()` using a fallback chain:

1. **Hard link** (`os.link`) — zero extra space; requires same filesystem.
2. **Symlink** (`os.symlink`) — tiny overhead; requires symlink support
   (Windows needs Developer Mode or elevation).
3. **Copy** (`shutil.copy2`) — fallback; uses full file space.

`utils.choose_link_strategy()` probes `st_dev` equality to decide.  Never
assume hard links will work across volumes.

---

## Testing patterns

### Organizer tests require `compute_hashes()` in the fixture

```python
index_directory(source, conn)
compute_hashes(conn)               # must be called before organise()
conn.execute("UPDATE photos SET status = 'indexed' WHERE status = 'discovered'")
conn.commit()
```

The last SQL line promotes any photos that `compute_hashes` couldn't hash
(e.g., photos that only exist in ZIPs) so the organiser picks them up.

### Synthetic JPEG bytes

Minimal fake JPEG for tests: `b"\xff\xd8\xff" + b"\x00" * N`.  This is
enough for file-type detection but not for real EXIF embedding (piexif will
fail on it, which is fine since we test that path separately).

### SQLite fixture pattern

```python
@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / ".takeout-sort" / "index.db")
    yield conn
    conn.close()
```

Call `db.commit()` between operations in tests when you need to close
implicit transactions before calling helpers that use explicit `BEGIN`.

---

## File layout

```
takeout_sort/
  __init__.py      version string
  cli.py           click commands + interactive prompts
  db.py            SQLite schema, upsert helpers, iterators
  downloader.py    Playwright automation + ZIP extraction
  indexer.py       two-pass scanner (discover → hash/dedup)
  metadata.py      JSON parsing, XMP writing, EXIF embedding
  organizer.py     file mover + album linker
  utils.py         OS detection, disk space, file ops, sidecar matching

scripts/
  delete_from_google_photos.py   standalone Playwright deletion tool

tests/
  test_db.py
  test_indexer.py
  test_metadata.py
  test_organizer.py
  test_utils.py
```

---

## Common pitfalls to avoid

1. **Don't skip `compute_hashes()`** before testing organiser behaviour
   involving album membership — the year-folder/album-folder dedup won't
   happen and `--no-albums-in-library` will appear broken.

2. **Don't hardcode file sizes** in tests — use `b"\x00" * N` as content
   and compare by hash or filename, not size.

3. **Don't call `BEGIN` manually** inside a `pytest` fixture that has already
   run DML — call `conn.commit()` first.

4. **`piexif` only supports JPEG/TIFF** — `embed_exif()` returns `False`
   silently for HEIC, PNG, MOV, etc.  This is expected; XMP covers all types.

5. **`upsert_photo` returns -1** when the row already exists (INSERT OR IGNORE
   was suppressed); callers should handle this sentinel value.

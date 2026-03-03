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

---

## General rules and transferable patterns

These lessons came out of building this project but apply broadly to any
software agent, automated pipeline, or CLI tool.  They are written to be
useful to future agents regardless of project or language.

---

### 1. `INSERT OR IGNORE` is only idempotent when a UNIQUE constraint exists

If you want "insert if not already there" semantics, the database must have
a UNIQUE (or PRIMARY KEY) constraint on the column(s) that define uniqueness.
Without it, `INSERT OR IGNORE` inserts every time and creates silent duplicates.

**Always pair `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING` with an explicit
UNIQUE constraint.**  This applies to PostgreSQL, SQLite, and MySQL alike.

---

### 2. Python's `sqlite3` opens implicit transactions — and they bite you

Python's `sqlite3` module automatically begins a transaction after any DML
(INSERT, UPDATE, DELETE) unless `isolation_level=None`.  If you then call
`conn.execute("BEGIN")` you get:

```
sqlite3.OperationalError: cannot start a transaction within a transaction
```

**Rule:** After any DML, call `conn.commit()` or `conn.rollback()` before
issuing another explicit `BEGIN`.  In tests, always commit between setup
steps and the code under test.  In production code, prefer a context-manager
(`with transaction(conn):`) that handles `BEGIN`/`COMMIT`/`ROLLBACK`
automatically so callers never need to think about this.

---

### 3. Design for resumability from day one with a status column

Any pipeline that processes large datasets (files, API records, database rows)
should track each item's state in a persistent store from the start.  A simple
status column (`discovered → indexed → organised`) gives you:

- Free resumability: re-run and skip already-processed items.
- Observability: query the DB to see exactly where things stand.
- Safety: if the process crashes mid-run, nothing is lost.

**Don't rely on filesystem state alone** (e.g., "does the output file exist?").
The DB is the source of truth.  Write `final_path` back to the DB so you can
verify the file at any time.

---

### 4. Destructive or irreversible operations require confirmation

Any action the user cannot easily undo (deleting cloud data, stripping
metadata from files, force-pushing, wiping a cache) deserves:

1. A clear, plain-English warning of exactly what will happen.
2. An explicit confirmation step (typed `yes`, or a numbered menu choice).
3. A safe exit if the terminal is non-interactive (CI/pipes).
4. A `--dry-run` flag where practical.

For non-interactive contexts, either fail loudly (`sys.exit(1)`) or require
a `--yes` / `--force` flag that the caller must pass deliberately.  Silent
no-ops are the worst outcome: the user thinks something happened but it didn't.

---

### 5. Thread flags all the way down — don't use globals

When a feature flag or option affects behaviour deep in a call stack, pass it
explicitly through every layer rather than storing it in a global or module-
level variable.  This makes the code testable (each layer can be tested with
either value independently), grep-able (one search finds every affected site),
and safe for concurrent use.

```
cli → organise(include_google_metadata=...)
    → _organise_photo(include_google_metadata=...)
      → apply_metadata(include_google_metadata=...)
        → write_xmp_sidecar(include_google_metadata=...)
```

If passing the flag 5 levels deep feels painful, it is a sign the function
hierarchy is too deep — consider a config/options dataclass instead of a
growing parameter list.

---

### 6. Detect capabilities at runtime; fall back gracefully

Hard links, symlinks, and EXIF embedding all depend on OS and filesystem
capabilities.  Probe them once at startup (check `st_dev`, try-except a test
link) and store the result.  Then apply the best available strategy silently.

```
hard link → symlink → copy   (file linking)
EXIF embed → XMP sidecar only   (metadata)
```

Never fail hard when a fallback exists.  Log the fallback at INFO level so
power users can see what happened, but don't surface it as an error.

---

### 7. Pre-flight checks before long, space-consuming operations

Before kicking off anything that will write gigabytes of data:
1. Estimate the required space.
2. Check available space on the target volume.
3. If it might be tight, warn the user and offer a choice — not a crash mid-way.

This is especially important for ZIP extraction (the extracted tree can be
2–3× the ZIP size).  A disk-full mid-extraction leaves partial data and is
much harder to recover from than a clean pre-flight rejection.

---

### 8. Two-pass pipelines avoid holding everything in memory

For large datasets:

- **Pass 1 (discover):** Walk the source and record items in the DB.
  No heavy computation.  Fast.  Can be interrupted and resumed.
- **Pass 2 (process):** Read from the DB in batches, do the heavy work
  (hashing, moving, API calls), update status.

This decouples discovery from processing, lets you restart either pass
independently, and avoids loading the entire dataset into RAM.  Batch
size should be tunable (default 200–500 rows) to balance memory vs. round-trips.

---

### 9. Write the sentinel / error-return value into the function contract

When a function can legally return "nothing was done" (e.g., `INSERT OR IGNORE`
was suppressed), choose a sentinel value and document it:

```python
def upsert_photo(...) -> int:
    """Returns the row id, or -1 if the row already existed."""
```

Avoid returning `None` for "error" and a valid ID for "success" — callers
must explicitly check for `None` and it is too easy to treat it as falsy-but-valid.
A named constant (`ALREADY_EXISTS = -1`) is even better.

---

### 10. Make progress visible; make it optional

Long-running operations should accept a `progress_cb` callback rather than
printing directly.  This keeps library code free of I/O side-effects and lets
callers (CLI, GUI, tests) decide how to display progress.

```python
def compute_hashes(conn, progress_cb=None, batch_size=200):
    ...
    if progress_cb:
        progress_cb(filename, photo_id)
```

Tests can inject a collecting lambda; the CLI can wire up a Rich progress bar;
headless runs get nothing.  Never print directly from library functions.

---

### 11. Encode domain knowledge as named constants, not bare literals

Magic numbers and magic strings scatter domain knowledge across the codebase.
Collect them at the top of the relevant module:

```python
_JSON_STEM_MAX = 46   # Google Takeout truncates sidecar names at 46 chars
_YEAR_FOLDER_RE = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)
```

When a bug is caused by a domain rule (e.g., "why 46?") the answer is one
grep away instead of buried inside a conditional.

---

### 12. `shutil.copy2` + unlink is not atomic — prefer `os.rename` where possible

`safe_move()` uses `os.rename()` when source and destination are on the same
filesystem (atomic on POSIX).  Cross-filesystem moves must fall back to
copy-then-delete.  If a crash happens between copy and delete, you have a
duplicate — which is safe to clean up but messy.

**Rule:** Always try `rename` first.  Only copy-then-delete when `rename`
raises `OSError` (cross-device).  Log the fallback.

---

### 13. Sidecar / companion file lookup should be tolerant of edge cases

Real-world data is messier than the spec.  A sidecar-finding function should
handle, in order:

1. Exact match (`photo.jpg.json`)
2. Truncated stem (long filenames)
3. Suffix variants (`-edited`, `(1)`, etc.)
4. Case-insensitive filesystem differences

Return `None` cleanly — never raise — when no sidecar is found.  The caller
decides whether a missing sidecar is an error or a normal case.

---

### 14. Document the "why" not just the "what" in persistent agent notes

When writing notes for future agents (like this file), the most valuable thing
is the reasoning behind non-obvious decisions — not what the code does (the
code shows that), but *why* it does it that way.

Good: "source_path has a UNIQUE constraint because without it INSERT OR IGNORE
never ignores anything and idempotency tests fail."

Less useful: "source_path is TEXT NOT NULL UNIQUE."

Future agents (and humans) will reach for the simpler approach and need to know
why it won't work before they spend time on it.

---

### 15. Playwright / browser automation: authenticate once, reuse the session

Browser automation for authenticated services (Google, etc.) is fragile if
you try to log in programmatically.  Instead:

- **Launch mode:** Open a real browser window, let the user log in manually,
  then take over the session via CDP or storage-state serialisation.
- **Attach mode:** Attach to an already-running Chrome/Edge via `--remote-debugging-port`.
- **Cookie/storage export:** Serialize `context.storage_state()` to a file
  and re-use it in subsequent headless runs.

Avoid storing credentials in code or config files.  Let the human do the login
once; automate everything after authentication.

---

### 16. End-to-end tests catch interaction bugs that unit tests miss

Unit tests verify individual functions.  End-to-end tests verify that the
pieces work together.  The most valuable bug caught during this project
(`--no-albums-in-library` silently not working) was only caught by a test
that ran the full pipeline: index → hash → organise → assert file location.

No unit test on `compute_hashes()` alone would have revealed that the
album membership transfer was missing, because no single unit knew it was
responsible for that cross-function contract.

**Rule:** For any feature that spans multiple modules, write at least one
test that exercises the full path from input to final observable output.
These tests are slower but catch the bugs that matter most to users.

---

### 17. `sqlite3.Row` enables named column access — always set it

By default, Python's `sqlite3` returns rows as plain tuples.  Setting:

```python
conn.row_factory = sqlite3.Row
```

lets you access columns by name (`row["status"]`, `row["source_path"]`)
instead of by index (`row[2]`).  This makes code far more readable and
robust against schema changes that add/reorder columns.  Set it immediately
after `connect()` and never use index-based row access.

---

### 18. `pyproject.toml` build-backend path is easy to get wrong

The correct `setuptools` build backend identifier is:

```toml
[build-system]
build-backend = "setuptools.build_meta"
```

A common mistake is `"setuptools.backends.legacy:build"` (which does not
exist) or `"setuptools:build_meta"` (wrong separator).  If `pip install -e .`
fails with "Cannot import … build backend", check this line first.

---

### 19. Use `pytest`'s `tmp_path` fixture — never `tempfile` directly in tests

`pytest` provides `tmp_path` (a `pathlib.Path` to a fresh temp directory,
unique per test, automatically cleaned up) as a built-in fixture.  Using it
gives you:

- Automatic cleanup even on test failure.
- `pathlib.Path` API (not raw strings).
- A consistent pattern every pytest user recognises.

Prefer `tmp_path / "subdir" / "file.txt"` over `tempfile.mkdtemp()` in any
pytest-based test suite.

---

### 20. When a function can silently no-op, make the no-op observable

`INSERT OR IGNORE` silently does nothing when the row already exists.
`os.link` silently does nothing if the link already exists (on some platforms).
`shutil.copy2` silently overwrites.

In all these cases, the caller should be able to detect what happened — via
a return value, a log message, or an exception.  Silent no-ops cause subtle
bugs: the developer assumes the operation succeeded, but the data was never
written (or was overwritten).

For upserts: return the existing row's id (or a sentinel like `-1`) so the
caller knows whether an insert or an ignore occurred.  Log at DEBUG level
which branch was taken.  This costs almost nothing and saves hours of
debugging.

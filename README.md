# takeout-sort

**A fast, space-efficient CLI tool for sorting a Google Photos library exported via Google Takeout.**

Google Takeout exports your photo library as dozens (or hundreds) of ZIP files, each containing a
chaotic mix of year-folders, album-folders, and per-photo JSON sidecar files. `takeout-sort` turns
that mess into a clean, well-organised folder structure — preserving all metadata, albums, and
timestamps — while using as little extra disk space as possible.

---

## Features

- **All-in-one or step-by-step** — download ZIPs automatically, index them, and organise, or run
  each phase independently
- **Automated downloading** — opens a browser window and clicks every "Download" button on the
  Takeout page for you (no more pressing 100 buttons)
- **Space-efficient** — files are *moved*, not copied; album entries use hard links (zero extra
  bytes); ZIPs are deleted as each one is extracted
- **Full metadata preservation** — writes an XMP sidecar (`.xmp`) next to every photo and embeds
  date + GPS into the photo's EXIF; works natively in Finder Quick Look, Lightroom, Windows
  Explorer, Digikam, and most photo managers
- **Album support** — every Google Photos album becomes a real folder; optionally exclude album
  photos from the main Library (a popular "clean Library" workflow)
- **Resumable** — progress is tracked in a SQLite database; interrupted runs pick up where they
  left off
- **Duplicate detection** — SHA-256 deduplication ensures the same photo is stored only once
- **Cross-platform** — macOS, Linux, and Windows (hard links fall back to symlinks or copies
  automatically)
- **Configurable folder depth** — `YYYY/MM/DD/` (default), `YYYY/MM/`, or `YYYY/`

---

## Output structure

```
<destination>/
├── .takeout-sort/
│   └── index.db          ← SQLite database (progress + metadata)
├── Library/
│   ├── 2023/
│   │   ├── 01/
│   │   │   └── 01/
│   │   │       ├── IMG_1234.jpg
│   │   │       └── IMG_1234.xmp   ← XMP sidecar (date, GPS, tags…)
│   │   └── …
│   └── No Date/           ← photos with no timestamp metadata
└── Albums/
    └── Vacation Summer/
        ├── IMG_5678.jpg   ← hard link to Library copy (zero extra space)
        └── IMG_5678.xmp
```

---

## Requirements

- **Python 3.10+**
- [Playwright](https://playwright.dev/python/) (only needed for the download phase)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/rachael/google-takeout-sort.git
cd google-takeout-sort

# 2. Install (a virtual environment is recommended)
pip install .

# 3. Install Playwright's bundled Chromium (only needed for automatic downloading)
pip install playwright
playwright install chromium
```

---

## Quick start

### Option A — You haven't downloaded your Takeout ZIPs yet

```bash
takeout-sort run ~/Photos
```

This opens a browser window. Sign in to Google, navigate to
[takeout.google.com/settings/takeout/downloads](https://takeout.google.com/settings/takeout/downloads),
and the script takes over: it clicks every Download button, extracts each ZIP, and organises
everything into `~/Photos/Library/` and `~/Photos/Albums/`.

### Option B — You've already downloaded the ZIPs

```bash
# If the ZIPs are in ~/Downloads/takeout/:
takeout-sort organize ~/Downloads/takeout ~/Photos

# If you've already extracted the ZIPs into a folder:
takeout-sort organize ~/Downloads/raw ~/Photos
```

---

## Commands

### `takeout-sort run <destination>`

All-in-one: download → extract → index → organise.

```
takeout-sort run ~/Photos [OPTIONS]
```

### `takeout-sort download <destination>`

Phase 1 only — automate downloading all Takeout ZIPs into `<destination>/downloads/`.
Run `takeout-sort organize` afterwards.

```
takeout-sort download ~/Photos [OPTIONS]
```

### `takeout-sort organize <source> [destination]`

Index and organise a pre-downloaded or pre-extracted Takeout export.

- `<source>` — directory containing ZIP files or extracted Takeout folders
- `[destination]` — where `Library/` and `Albums/` will be created (defaults to `<source>`)

```
takeout-sort organize ~/Downloads/takeout ~/Photos [OPTIONS]
```

### `takeout-sort status <destination>`

Show a summary of the current indexing / organisation progress.

```
takeout-sort status ~/Photos
```

---

## Options reference

| Flag | Default | Description |
|---|---|---|
| `--url URL` | Takeout download page | URL of the Google Takeout download page |
| `--auth browser\|attach\|js` | `browser` | How to handle Google sign-in (see below) |
| `--cdp-port PORT` | `9222` | Chrome DevTools port for `--auth=attach` |
| `--depth day\|month\|year` | `day` | Date-based subfolder depth inside `Library/` |
| `--albums-in-library` | ✓ (on) | Include album photos in `Library/` as well |
| `--no-albums-in-library` | — | Exclude album photos from `Library/`; they live only in `Albums/` |
| `--zip-cleanup immediate\|after\|keep` | `immediate` | When to delete original ZIPs (see below) |
| `--headless` | off | Run Playwright in headless mode |
| `--google-metadata` | ✓ (on) | Include Google-specific fields in XMP sidecars (e.g. the Google Photos source URL) |
| `--no-google-metadata` | — | Omit Google-specific fields from XMP sidecars |
| `--db PATH` | `<dest>/.takeout-sort/index.db` | Custom SQLite database path |

---

## Auth modes (`--auth`)

| Mode | How it works |
|---|---|
| `browser` **(default)** | Opens a visible Chromium window. Sign in to Google, navigate to the Takeout downloads page, and the script automates the rest. |
| `attach` | Attaches to a Chrome/Chromium instance already running with `--remote-debugging-port=9222`. Start Chrome manually: `google-chrome --remote-debugging-port=9222`, log in, navigate to the Takeout page, then run the script. |
| `js` | Prints a JavaScript snippet to paste into your browser's DevTools console. No Playwright dependency for this mode. The script then waits for you to move the ZIPs into `<destination>/downloads/`. |

---

## ZIP cleanup options (`--zip-cleanup`)

| Option | Behaviour | Best for |
|---|---|---|
| `immediate` **(default)** | Delete each ZIP immediately after extraction | Drives near capacity |
| `after` | Keep ZIPs during organisation; delete them all when done | Medium drives |
| `keep` | Never delete ZIPs | Unlimited space / backup purposes |

If free disk space is insufficient before extracting a ZIP, the script pauses, reports how much
space is needed vs available, and offers interactive options (switch to immediate deletion, skip
this ZIP, or abort and resume later).

---

## Album handling

### `--albums-in-library` (default)

Every photo appears in `Library/` sorted by date. Photos that are also in albums appear *again*
in `Albums/<album-name>/` as hard links (no extra space used).

```
Library/2023/01/01/beach.jpg      ← the file
Albums/Vacation/beach.jpg         ← hard link (same inode, zero extra bytes)
```

### `--no-albums-in-library`

Photos that belong to at least one album go **directly into** `Albums/` only.
`Library/` contains only photos that are not in any album.

```
Library/2023/01/01/solo.jpg       ← not in any album
Albums/Vacation/beach.jpg         ← primary location for album photos
```

This mirrors the Google Photos behaviour where a photo "lives" in an album once it's been sorted
into one.

---

## Metadata

Every organised photo gets an XMP sidecar file (`photo.xmp`) written alongside it.
The sidecar contains:

- **Date taken** (`xmp:CreateDate`, `exif:DateTimeOriginal`)
- **GPS coordinates** (`exif:GPSLatitude`, `exif:GPSLongitude`, `exif:GPSAltitude`)
- **Title** (`dc:title`)
- **Description / caption** (`dc:description`)
- **People / faces** (`Iptc4xmpCore:SubjectCode`)
- **Google Photos URL** (`dc:source`)

For JPEG and TIFF files, the date and GPS are also embedded directly into the file's EXIF so that
apps that don't read XMP (e.g. basic camera rolls) show the correct date.

XMP sidecars are read natively by:
- macOS Finder / Quick Look / Preview
- Adobe Lightroom and Bridge
- Windows Explorer (via Windows Photo Viewer and Photos app)
- digiKam, darktable, RawTherapee
- Most modern photo management software

---

## Large libraries

`takeout-sort` is designed to handle very large exports:

- **Streaming index** — ZIPs are indexed without full extraction; only the small JSON sidecar
  files are loaded into memory. The SQLite database records the ZIP path and member path for each
  photo so it can be extracted on-demand during the organise phase.
- **Move, don't copy** — photos are moved (renamed) into `Library/`, not duplicated.
- **Hard links for albums** — no extra bytes for album membership.
- **ZIPs deleted immediately** — with `--zip-cleanup immediate` (the default), each ZIP is deleted
  as soon as its contents are extracted, keeping free space available for the next download.
- **Resumable** — if the process is interrupted (power failure, Ctrl-C, etc.), re-run the same
  command. The SQLite database tracks which photos have already been organised.

---

## Step-by-step walkthrough for a large library

```bash
# 1. Start the all-in-one command; a browser window will open.
takeout-sort run ~/Photos

# 2. In the browser: sign in → navigate to the Takeout downloads page.
#    The script detects the page and begins clicking Download buttons.

# 3. Each ZIP is downloaded to ~/Photos/downloads/, extracted to ~/Photos/raw/,
#    and the ZIP is deleted immediately (--zip-cleanup immediate).

# 4. Once all ZIPs are extracted, the indexer builds ~/Photos/.takeout-sort/index.db.

# 5. The organiser moves photos into ~/Photos/Library/ and creates album links.

# 6. Done! Check the output:
takeout-sort status ~/Photos
```

---

## Resuming an interrupted run

```bash
# Simply re-run the same command.
# Already-organised photos are skipped; the run continues from where it stopped.
takeout-sort run ~/Photos

# Or, if you only need to re-run the organise phase:
takeout-sort organize ~/Photos/raw ~/Photos
```

---

## Troubleshooting

**"No download links found on this page."**
- Make sure you're on `https://takeout.google.com/settings/takeout/downloads` (not the export
  creation page).
- Your export must be *ready* — Google sends an email when it's available.
- Try `--auth=js` and paste the snippet into your browser's console.

**The browser window closed before all downloads finished.**
- The ZIPs that were downloaded are still in `<destination>/downloads/`.
- Run `takeout-sort organize <destination>` to continue with what's already there.

**Photos appear in "No Date" folder.**
- These photos had no `photoTakenTime` in their JSON sidecar and no EXIF date.
- You can manually move them after the sort.

**Hard links don't work / I see copies in Albums/.**
- Hard links require the source and destination to be on the same filesystem.
  If `Library/` and `Albums/` are on different drives, `takeout-sort` automatically falls back to
  symlinks (or copies on Windows without symlink permissions). This is expected.

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check takeout_sort tests
```

---

## Utility scripts

### `scripts/delete_from_google_photos.py`

After you have verified that your local library is complete, this script
deletes the corresponding photos from Google Photos (cloud) using the URLs
recorded in the takeout-sort database.

> **Google's API no longer supports deletion.**  The script automates the
> Google Photos web interface via Playwright instead.

```bash
# Always do a dry run first
python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db --dry-run

# Delete everything (prompts for confirmation)
python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db

# Delete up to 50 photos, 2 s apart, using an existing Chrome session
python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db \
    --limit 50 --delay 2 --auth attach
```

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | List photos that would be deleted without actually deleting |
| `--auth browser\|attach` | `browser` | Auth method (same as main CLI) |
| `--cdp-port PORT` | `9222` | DevTools port for `--auth=attach` |
| `--delay SECS` | `1.5` | Seconds between deletions (avoid rate-limiting) |
| `--limit N` | unlimited | Stop after N deletions |
| `--headless` | off | Run browser headlessly |

Only photos that had a `google_url` in their Takeout JSON sidecar can be
targeted.  Photos without a recorded URL are reported but skipped.

---

## Roadmap

- [ ] Google Drive export support
- [ ] GUI wrapper
- [ ] Progress resume across machines (exportable DB)
- [ ] Batch HEIC → JPEG conversion (optional)

---

## Contributing

Pull requests are welcome. Please open an issue first to discuss significant changes.

---

## License

MIT © Contributors

"""
takeout-sort CLI entry point.

Commands
--------
takeout-sort run        <destination>  — download + index + organise (all-in-one)
takeout-sort download   <destination>  — phase 1: automate Takeout downloads
takeout-sort organize   <source>       — phase 2+3: index + organise (pre-downloaded)
takeout-sort status     <destination>  — show progress from the DB

Global flags
------------
--url       URL of the Takeout download page (default: takeout.google.com/…)
--auth      browser | attach | js  (download auth mode)
--cdp-port  Chrome DevTools port for 'attach' mode (default 9222)
--depth     day | month | year (Library date subfolder depth, default: day)
--no-albums-in-library
            Exclude album photos from Library/; they live only in Albums/
--zip-cleanup  immediate | after | keep
            When to delete original ZIPs
--headless  Run Playwright in headless mode (no visible window)
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .db import open_db, count_by_status
from .downloader import (
    check_space_for_zip,
    download_takeout,
    extract_zip,
    print_js_snippet,
)
from .indexer import compute_hashes, index_directory, index_zip
from .organizer import FolderDepth, organise, print_summary
from .utils import (
    detect_os,
    get_space,
    human_bytes,
    OSFamily,
    walk_files,
)

console = Console()

_TAKEOUT_DOWNLOAD_URL = "https://takeout.google.com/settings/takeout/downloads"

# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

_url_option = click.option(
    "--url",
    default=_TAKEOUT_DOWNLOAD_URL,
    show_default=True,
    help="URL of the Google Takeout download page.",
)
_auth_option = click.option(
    "--auth",
    type=click.Choice(["browser", "attach", "js"], case_sensitive=False),
    default="browser",
    show_default=True,
    help=(
        "How to authenticate for downloading.\n\n"
        "  browser — open a visible browser window (default)\n"
        "  attach  — attach to Chrome started with --remote-debugging-port=9222\n"
        "  js      — print a JS snippet to paste into the browser console"
    ),
)
_cdp_port_option = click.option(
    "--cdp-port", default=9222, show_default=True,
    help="Chrome DevTools Protocol port (used with --auth=attach).",
)
_depth_option = click.option(
    "--depth",
    type=click.Choice(["day", "month", "year"], case_sensitive=False),
    default="day",
    show_default=True,
    help="Date-based subfolder depth inside Library/.",
)
_albums_option = click.option(
    "--albums-in-library/--no-albums-in-library",
    default=True,
    help=(
        "Include album photos in Library/ as well (default). "
        "Use --no-albums-in-library to keep Library/ album-free."
    ),
)
_zip_cleanup_option = click.option(
    "--zip-cleanup",
    type=click.Choice(["immediate", "after", "keep"], case_sensitive=False),
    default="immediate",
    show_default=True,
    help=(
        "When to delete original ZIPs.\n\n"
        "  immediate — delete each ZIP right after extraction (saves most space)\n"
        "  after     — delete all ZIPs once organisation is complete\n"
        "  keep      — never delete ZIPs"
    ),
)
_headless_option = click.option(
    "--headless", is_flag=True, default=False,
    help="Run browser in headless mode (no visible window). Requires --auth=browser.",
)
_google_metadata_option = click.option(
    "--google-metadata/--no-google-metadata",
    default=True,
    help=(
        "Include Google-specific fields in XMP sidecars (default). "
        "Use --no-google-metadata to omit them (e.g. the Google Photos source URL)."
    ),
)
_db_option = click.option(
    "--db", "db_path",
    default=None, type=click.Path(),
    help="Custom path for the SQLite index database.",
)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(__version__, prog_name="takeout-sort")
def main():
    """
    \b
    takeout-sort — Sort a Google Photos Takeout export into a clean library.

    \b
    Quick start (all-in-one):
      takeout-sort run ~/Photos

    \b
    Step by step:
      takeout-sort download ~/Photos
      takeout-sort organize ~/Photos/raw ~/Photos
    """


# ---------------------------------------------------------------------------
# 'run' command — all phases
# ---------------------------------------------------------------------------

@main.command()
@click.argument("destination", type=click.Path(file_okay=False, writable=True))
@_url_option
@_auth_option
@_cdp_port_option
@_depth_option
@_albums_option
@_zip_cleanup_option
@_headless_option
@_google_metadata_option
@_db_option
def run(
    destination,
    url,
    auth,
    cdp_port,
    depth,
    albums_in_library,
    zip_cleanup,
    headless,
    google_metadata,
    db_path,
):
    """
    Download, index, and organise a Google Takeout export (all-in-one).

    DESTINATION is the directory that will hold the downloads/ folder and the
    final Library/ + Albums/ output.  It will be created if it doesn't exist.
    """
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)

    db = _open_db(dest, db_path)

    _print_banner(dest)

    # Phase 1 — download
    _phase_download(dest, url=url, auth=auth, cdp_port=cdp_port,
                    zip_cleanup=zip_cleanup, headless=headless, conn=db)

    # Phase 2 — index
    raw_dir = dest / "raw"
    if raw_dir.exists():
        _phase_index(raw_dir, db)
    else:
        console.print("[yellow]No raw/ directory found; skipping index phase.[/yellow]")

    # Phase 3 — organise
    _phase_organise(dest, db, depth=depth, albums_in_library=albums_in_library,
                    include_google_metadata=google_metadata, zip_cleanup=zip_cleanup)

    _print_summary(db, dest)


# ---------------------------------------------------------------------------
# 'download' command — phase 1 only
# ---------------------------------------------------------------------------

@main.command()
@click.argument("destination", type=click.Path(file_okay=False, writable=True))
@_url_option
@_auth_option
@_cdp_port_option
@_zip_cleanup_option
@_headless_option
@_db_option
def download(destination, url, auth, cdp_port, zip_cleanup, headless, db_path):
    """
    Automate downloading all Takeout ZIPs into DESTINATION/downloads/.

    After downloading, use 'takeout-sort organize' to sort the files.
    """
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    db = _open_db(dest, db_path)

    _print_banner(dest)
    _phase_download(dest, url=url, auth=auth, cdp_port=cdp_port,
                    zip_cleanup=zip_cleanup, headless=headless, conn=db)

    console.print("\n[green]Download phase complete.[/green]")
    console.print(
        f"Next step:  [bold]takeout-sort organize {destination}[/bold]"
    )


# ---------------------------------------------------------------------------
# 'organize' command — index + organise a pre-downloaded (or pre-extracted) source
# ---------------------------------------------------------------------------

@main.command()
@click.argument("source", type=click.Path(exists=True, file_okay=False))
@click.argument("destination", type=click.Path(file_okay=False, writable=True), required=False)
@_depth_option
@_albums_option
@_zip_cleanup_option
@_google_metadata_option
@_db_option
def organize(source, destination, depth, albums_in_library, zip_cleanup, google_metadata, db_path):
    """
    Index and organise an already-downloaded Takeout export.

    SOURCE is the directory containing extracted Takeout folders or ZIP files.
    DESTINATION (optional) is where Library/ and Albums/ will be created.
    If omitted, DESTINATION defaults to SOURCE.

    \b
    Examples:
      # Exported ZIPs already in ~/Downloads/takeout/:
      takeout-sort organize ~/Downloads/takeout ~/Photos

      # Already extracted into ~/Downloads/raw/:
      takeout-sort organize ~/Downloads/raw ~/Photos
    """
    src = Path(source)
    dest = Path(destination) if destination else src
    dest.mkdir(parents=True, exist_ok=True)

    db = _open_db(dest, db_path)
    _print_banner(dest)

    _phase_index(src, db, zip_cleanup=zip_cleanup)
    _phase_organise(dest, db, depth=depth, albums_in_library=albums_in_library,
                    include_google_metadata=google_metadata, zip_cleanup=zip_cleanup)

    _print_summary(db, dest)


# ---------------------------------------------------------------------------
# 'status' command
# ---------------------------------------------------------------------------

@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
@_db_option
def status(destination, db_path):
    """Show the current indexing / organisation progress for a DESTINATION."""
    dest = Path(destination)
    db = _open_db(dest, db_path)

    stats = count_by_status(db)
    total = sum(stats.values())

    table = Table(title="takeout-sort status", show_header=True, header_style="bold cyan")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    for status_name, count in sorted(stats.items()):
        table.add_row(status_name, str(count))
    table.add_row("[bold]Total[/bold]", f"[bold]{total}[/bold]")

    console.print(table)

    space = get_space(dest)
    console.print(
        f"\nDisk: [cyan]{human_bytes(space.free_bytes)}[/cyan] free of "
        f"{human_bytes(space.total_bytes)} on {dest}"
    )


# ---------------------------------------------------------------------------
# Internal phase helpers
# ---------------------------------------------------------------------------

def _open_db(dest: Path, db_path_override: str | None):
    if db_path_override:
        p = Path(db_path_override)
    else:
        p = dest / ".takeout-sort" / "index.db"
    return open_db(p)


def _print_banner(dest: Path) -> None:
    os_family = detect_os()
    space = get_space(dest)
    console.print(
        f"\n[bold cyan]takeout-sort[/bold cyan] v{__version__}  "
        f"  OS: {os_family.name}  "
        f"  Free: {human_bytes(space.free_bytes)}\n"
        f"  Destination: [bold]{dest}[/bold]\n"
    )


def _phase_download(
    dest: Path,
    *,
    url: str,
    auth: str,
    cdp_port: int,
    zip_cleanup: str,
    headless: bool,
    conn,
) -> None:
    console.rule("[bold]Phase 1 — Download[/bold]")

    if auth == "js":
        print_js_snippet()
        console.print(
            "[yellow]Move the downloaded ZIPs into:[/yellow]\n"
            f"  [bold]{dest / 'downloads'}[/bold]\n"
            "Then run:\n"
            f"  [bold]takeout-sort organize {dest}[/bold]"
        )
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Downloading…", total=None)

        def progress_cb(label: str, current: int, total: int):
            progress.update(task, description=f"[cyan]{label}[/cyan]", total=total, completed=current)

        downloaded = download_takeout(
            dest,
            url=url,
            auth_mode=auth,
            cdp_port=cdp_port,
            conn=conn,
            progress_cb=progress_cb,
            headless=headless,
        )

    if not downloaded:
        console.print("[yellow]No files downloaded.[/yellow]")
        return

    console.print(f"\n[green]Downloaded {len(downloaded)} file(s).[/green]")

    # Extract ZIPs
    _extract_all_zips(dest / "downloads", dest, zip_cleanup=zip_cleanup)


def _extract_all_zips(
    downloads_dir: Path,
    dest: Path,
    zip_cleanup: str = "immediate",
) -> None:
    zips = sorted(downloads_dir.glob("*.zip"))
    if not zips:
        return

    console.rule("[bold]Extracting ZIPs[/bold]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting…", total=len(zips))

        for zip_path in zips:
            # Space check
            ok, msg = check_space_for_zip(zip_path, dest)
            if not ok:
                _handle_low_space(msg, zip_path, dest, zip_cleanup)

            def cb(name, _zp=zip_path):
                pass  # per-member progress is too noisy

            delete = (zip_cleanup == "immediate")
            extract_zip(zip_path, dest, delete_after=delete, progress_cb=cb)
            progress.update(task, advance=1, description=f"[cyan]{zip_path.name}[/cyan]")

    console.print(f"[green]Extraction complete.[/green]")


def _handle_low_space(
    msg: str,
    zip_path: Path,
    dest: Path,
    current_cleanup: str,
) -> None:
    """Interactively offer space-saving options when disk is tight."""
    console.print(f"\n[bold red]⚠  Low disk space[/bold red]: {msg}\n")

    space = get_space(dest)
    zip_size = zip_path.stat().st_size

    options = []
    if current_cleanup != "immediate":
        options.append(("1", "Switch to immediate ZIP deletion (recommended)", "immediate"))
    options.append(("2", "Skip this ZIP and continue", "skip"))
    options.append(("3", "Abort and resume later with 'takeout-sort organize'", "abort"))

    console.print("Options:")
    for key, label, _ in options:
        console.print(f"  [{key}] {label}")

    while True:
        choice = click.prompt("Choose an option", default="1")
        for key, _, action in options:
            if choice == key:
                if action == "skip":
                    return
                if action == "abort":
                    console.print("[yellow]Aborting. Run 'takeout-sort organize' to continue.[/yellow]")
                    sys.exit(0)
                if action == "immediate":
                    # Delete already-extracted ZIPs to free space
                    downloads_dir = dest / "downloads"
                    for done_zip in sorted(downloads_dir.glob("*.zip")):
                        if done_zip != zip_path:
                            try:
                                done_zip.unlink()
                                console.print(f"  Deleted {done_zip.name}")
                            except OSError:
                                pass
                    return
        console.print("[red]Invalid choice.[/red]")


def _phase_index(
    source: Path,
    conn,
    zip_cleanup: str = "immediate",
) -> None:
    console.rule("[bold]Phase 2 — Index[/bold]")

    zips = sorted(source.rglob("*.zip"))
    dirs = [
        d for d in source.rglob("*")
        if d.is_dir() and d.name not in {"__MACOSX"}
    ]

    total_items = len(zips) + 1  # +1 for dir pass

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Index ZIPs (stream; no extraction)
        if zips:
            zip_task = progress.add_task("Indexing ZIPs…", total=len(zips))
            for zip_path in zips:
                def cb(name, _zp=zip_path):
                    progress.update(zip_task, description=f"[cyan]{_zp.name} › {name}[/cyan]")

                index_zip(zip_path, conn, progress_cb=cb)
                progress.update(zip_task, advance=1)

        # Index pre-extracted directories
        dir_task = progress.add_task("Indexing directories…", total=None)

        def dir_cb(name):
            progress.update(dir_task, description=f"[cyan]{name}[/cyan]")

        n = index_directory(source, conn, progress_cb=dir_cb)
        progress.update(dir_task, description=f"[green]Indexed {n} files[/green]", total=1, completed=1)

        # Hash pass (deduplication) — only for on-disk photos
        hash_task = progress.add_task("Deduplicating…", total=None)

        def hash_cb(name, photo_id):
            progress.update(hash_task, description=f"[cyan]{name}[/cyan]")

        compute_hashes(conn, progress_cb=hash_cb)
        progress.update(hash_task, description="[green]Deduplication done[/green]", total=1, completed=1)

    stats = count_by_status(conn)
    console.print(
        f"[green]Index complete:[/green] "
        f"{stats.get('indexed', 0)} photos ready, "
        f"{stats.get('skipped', 0)} duplicates skipped."
    )


def _phase_organise(
    dest: Path,
    conn,
    depth: str = "day",
    albums_in_library: bool = True,
    include_google_metadata: bool = True,
    zip_cleanup: str = "immediate",
) -> None:
    console.rule("[bold]Phase 3 — Organise[/bold]")

    folder_depth = FolderDepth(depth)

    total_row = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE status IN ('indexed', 'discovered')"
    ).fetchone()
    total = total_row[0] if total_row else 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Organising…", total=total)

        def prog_cb(name: str, current: int, total_: int):
            progress.update(task, description=f"[cyan]{name}[/cyan]", completed=current, total=total_)

        organise(
            conn,
            dest,
            depth=folder_depth,
            albums_in_library=albums_in_library,
            include_google_metadata=include_google_metadata,
            progress_cb=prog_cb,
        )

    console.print("[green]Organisation complete.[/green]")

    # Clean up ZIPs after organisation if requested
    if zip_cleanup == "after":
        downloads_dir = dest / "downloads"
        for z in downloads_dir.glob("*.zip"):
            try:
                z.unlink()
            except OSError:
                pass


def _print_summary(conn, dest: Path) -> None:
    console.rule("[bold]Summary[/bold]")
    stats = print_summary(conn, dest)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for k, v in sorted(stats.items()):
        table.add_row(k, str(v))
    console.print(table)

    library = dest / "Library"
    albums = dest / "Albums"
    if library.exists():
        console.print(f"\n[bold]Library:[/bold] {library}")
    if albums.exists():
        console.print(f"[bold]Albums:[/bold]  {albums}")
    console.print()

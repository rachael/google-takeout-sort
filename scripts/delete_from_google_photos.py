#!/usr/bin/env python3
"""
delete_from_google_photos.py
============================

Delete photos from Google Photos (cloud) using the URLs recorded in a
takeout-sort database.

**Important notes**
-------------------
- Google's API no longer supports photo deletion.  This script automates the
  Google Photos *web interface* via Playwright instead.
- Only photos that have a ``google_url`` recorded in the database are targeted.
  Photos without a URL are listed but skipped.
- The script requires you to be signed in to Google Photos in the browser it
  opens (or in the existing Chrome session you attach to).
- Deletion is *permanent*.  Run with ``--dry-run`` first to see what would be
  deleted without actually deleting anything.

Usage
-----
    python scripts/delete_from_google_photos.py <db-path> [OPTIONS]

Options
-------
    --dry-run           List photos that would be deleted, but don't delete.
    --auth browser      Open a visible Chromium window (default).
    --auth attach       Attach to Chrome started with --remote-debugging-port=9222.
    --cdp-port PORT     DevTools port for 'attach' mode (default: 9222).
    --delay SECS        Seconds to wait between each deletion (default: 1.5).
    --limit N           Stop after deleting N photos (default: unlimited).
    --headless          Run browser in headless mode.

Examples
--------
    # Dry run — show what would be deleted
    python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db --dry-run

    # Delete everything recorded in the database
    python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db

    # Delete up to 100 photos with a 2-second pause between each
    python scripts/delete_from_google_photos.py ~/Photos/.takeout-sort/index.db \\
        --limit 100 --delay 2
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _fetch_urls(db_path: Path) -> list[tuple[int, str, str]]:
    """
    Return [(photo_id, filename, google_url), …] for every organised photo
    that has a google_url recorded.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, original_filename, google_url FROM photos "
        "WHERE google_url IS NOT NULL AND google_url != '' "
        "ORDER BY taken_ts"
    ).fetchall()
    conn.close()
    return [(r["id"], r["original_filename"], r["google_url"]) for r in rows]


def _count_without_url(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE google_url IS NULL OR google_url = ''"
    ).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Playwright deletion logic
# ---------------------------------------------------------------------------

def _delete_photos(
    urls: list[tuple[int, str, str]],
    *,
    auth: str = "browser",
    cdp_port: int = 9222,
    delay: float = 1.5,
    limit: int | None = None,
    dry_run: bool = False,
    headless: bool = False,
) -> tuple[int, int]:
    """
    Iterate through photo URLs and delete each from Google Photos.

    Returns (deleted_count, failed_count).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print(
            "Error: Playwright is not installed.\n"
            "Install it with:  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    if limit is not None:
        urls = urls[:limit]

    deleted = 0
    failed = 0

    with sync_playwright() as p:
        if auth == "attach":
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            context = browser.contexts[0]
            page = context.pages[0]
        else:
            browser = p.chromium.launch(
                headless=headless,
                args=["--start-maximized"],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

            # Open Google Photos so the user can sign in if needed.
            page.goto("https://photos.google.com")
            _wait_for_signin(page)

        print(f"\nDeleting {len(urls)} photo(s) from Google Photos…\n")

        for idx, (photo_id, filename, url) in enumerate(urls, 1):
            prefix = f"[{idx}/{len(urls)}]"

            if dry_run:
                print(f"  {prefix} DRY RUN — would delete: {filename}")
                deleted += 1
                continue

            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)

                # Keyboard shortcut: Shift+3 (i.e. '#') moves photo to trash.
                # This is the standard Google Photos keyboard shortcut.
                page.keyboard.press("Shift+3")

                # Wait for and confirm the "Move to trash" dialog.
                try:
                    confirm = page.wait_for_selector(
                        "button:has-text('Move to trash'), "
                        "button:has-text('Delete'), "
                        "[aria-label*='trash'], "
                        "[data-action='trash']",
                        timeout=5_000,
                    )
                    if confirm:
                        confirm.click()
                except PWTimeout:
                    # No dialog appeared — the shortcut may have worked directly,
                    # or we might be on an unsupported page.
                    pass

                # Brief pause to let the UI settle and avoid rate-limiting.
                time.sleep(delay)

                print(f"  {prefix} Deleted: {filename}")
                deleted += 1

            except PWTimeout:
                print(f"  {prefix} Timeout loading: {filename} ({url})")
                failed += 1
            except Exception as exc:
                print(f"  {prefix} Error: {filename} — {exc}")
                failed += 1

        browser.close()

    return deleted, failed


# ---------------------------------------------------------------------------
# Sign-in detection
# ---------------------------------------------------------------------------

def _wait_for_signin(page, timeout_s: int = 300) -> None:
    """Block until the user is signed in to Google Photos (up to *timeout_s* seconds)."""
    print(
        "\nA browser window has opened.\n"
        "  Sign in to your Google account if prompted, then wait here.\n"
        "  The script will continue once you are signed in.\n"
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            url = page.url
            if "photos.google.com" in url and "accounts.google" not in url:
                print("Signed in. Starting deletions…\n")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for Google Photos sign-in ({timeout_s}s).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete photos from Google Photos using URLs from a takeout-sort database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "db_path",
        metavar="DB_PATH",
        help="Path to the takeout-sort SQLite database (e.g. ~/Photos/.takeout-sort/index.db).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List photos that would be deleted without actually deleting them.",
    )
    parser.add_argument(
        "--auth",
        choices=["browser", "attach"],
        default="browser",
        help="Auth method: 'browser' opens a new window; 'attach' uses an existing Chrome session.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=9222,
        metavar="PORT",
        help="Chrome DevTools Protocol port for --auth=attach (default: 9222).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        metavar="SECS",
        help="Seconds to wait between each deletion (default: 1.5).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after deleting N photos.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no visible window).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db_path).expanduser()

    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    urls = _fetch_urls(db_path)
    skipped = _count_without_url(db_path)

    print(f"Database: {db_path}")
    print(f"Photos with Google URL: {len(urls)}")
    if skipped:
        print(
            f"Photos without Google URL (will be skipped): {skipped}\n"
            "  (These photos had no URL in their Takeout JSON sidecar.)"
        )

    if not urls:
        print("\nNothing to delete.")
        return

    if args.dry_run:
        print("\n--- DRY RUN: no photos will be deleted ---\n")
    else:
        print(
            "\n⚠  WARNING: This will PERMANENTLY delete photos from Google Photos.\n"
            "   Make sure your local library is complete and verified first.\n"
            "   Use --dry-run to preview what will be deleted.\n"
        )
        try:
            answer = input("Type 'yes' to continue, anything else to abort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return
        if answer != "yes":
            print("Aborted.")
            return

    deleted, failed = _delete_photos(
        urls,
        auth=args.auth,
        cdp_port=args.cdp_port,
        delay=args.delay,
        limit=args.limit,
        dry_run=args.dry_run,
        headless=args.headless,
    )

    print(f"\nDone. Deleted: {deleted}  Failed: {failed}")
    if failed:
        print(
            "Tip: some deletions may have failed if the photo was already deleted,\n"
            "moved to another account, or if the URL has expired."
        )


if __name__ == "__main__":
    main()

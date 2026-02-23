"""
Download automation for Google Takeout exports.

The user has already requested a Takeout export and received an email with a
link to the download page (takeout.google.com/settings/takeout/downloads).
This module opens that page in a Playwright-controlled browser and clicks
every "Download" button, waiting for each file to land in the destination
downloads/ directory.

Auth strategies
---------------
browser (default)
    Opens a visible Chromium window. The script pauses on the Google login
    page and waits for the user to sign in and navigate to the Takeout
    download page. Once there, automation takes over.

attach
    Attaches to an already-running Chrome/Chromium instance that was started
    with ``--remote-debugging-port=9222``.  The user must start Chrome
    manually with that flag and navigate to the Takeout page beforehand.

js
    Prints a JavaScript snippet the user can paste into the browser console.
    No Playwright is used; the script then polls the downloads/ directory.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

# Playwright is an optional runtime dependency; we import lazily so the rest
# of the tool is importable without it.
def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print(
            "[takeout-sort] Playwright is not installed.\n"
            "Install it with:  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# JS-snippet strategy (no Playwright required for this part)
# ---------------------------------------------------------------------------

JS_SNIPPET = r"""
// Paste this into the browser console on the Google Takeout download page.
// It will click every "Download" button with a short delay between each.
(async () => {
  // Selector that matches the download buttons on takeout.google.com
  const selectors = [
    'a[href*="storage.googleapis.com"]',  // direct storage links
    'button[aria-label*="Download"]',
    'a[aria-label*="Download"]',
    '.jfk-button[data-action="download"]',
    '[data-download-url]',
  ];

  let buttons = [];
  for (const sel of selectors) {
    const found = Array.from(document.querySelectorAll(sel));
    buttons = buttons.concat(found);
  }

  // De-duplicate
  buttons = [...new Set(buttons)];
  console.log(`Found ${buttons.length} download button(s).`);

  for (let i = 0; i < buttons.length; i++) {
    const btn = buttons[i];
    console.log(`Clicking ${i + 1}/${buttons.length}: ${btn.href || btn.textContent.trim()}`);
    btn.click();
    // Wait 1.5 s between clicks to avoid triggering rate limits.
    await new Promise(r => setTimeout(r, 1500));
  }
  console.log('Done — all download buttons clicked.');
})();
"""


def print_js_snippet() -> None:
    print("\n" + "=" * 60)
    print("Paste the following JavaScript into your browser console")
    print("on the Google Takeout download page:")
    print("=" * 60)
    print(JS_SNIPPET)
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Playwright-based download
# ---------------------------------------------------------------------------

_TAKEOUT_DOWNLOAD_URL = "https://takeout.google.com/settings/takeout/downloads"

# CSS / attribute selectors to find download links/buttons on the Takeout page.
_DOWNLOAD_SELECTORS = [
    "a[href*='storage.googleapis.com']",
    "a[href*='takeout-export']",
    "[data-download-url]",
    "a.downloadButton",
    "div[role='button'][aria-label*='Download']",
    "button[aria-label*='Download']",
]


def download_takeout(
    destination: Path,
    *,
    url: str = _TAKEOUT_DOWNLOAD_URL,
    auth_mode: str = "browser",   # "browser" | "attach" | "js"
    cdp_port: int = 9222,
    conn: sqlite3.Connection | None = None,
    progress_cb: Callable[[str, int, int], None] | None = None,
    headless: bool = False,
) -> list[Path]:
    """
    Automate downloading all Takeout ZIPs to ``destination/downloads/``.

    Returns the list of downloaded ZIP paths.
    """
    downloads_dir = destination / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if auth_mode == "js":
        print_js_snippet()
        print(
            "After clicking all buttons, the ZIPs will land in your browser's\n"
            "default Downloads folder. Move them to:\n"
            f"  {downloads_dir}\n"
            "then run:\n"
            "  takeout-sort organize <destination>\n"
        )
        return []

    sync_playwright = _require_playwright()

    with sync_playwright() as p:
        if auth_mode == "attach":
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            context = browser.contexts[0]
            page = context.pages[0]
        else:
            # "browser" mode: open a visible window
            browser = p.chromium.launch(
                headless=headless,
                downloads_path=str(downloads_dir),
                args=["--start-maximized"],
            )
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto(url)

            # Pause until user has logged in and the download page is loaded.
            _wait_for_takeout_page(page)

        # Collect all download links.
        links = _collect_download_links(page)

        if not links:
            print(
                "\n[takeout-sort] No download links found on this page.\n"
                "Make sure you are on https://takeout.google.com/settings/takeout/downloads\n"
                "and that your export is ready.\n"
            )
            browser.close()
            return []

        print(f"\n[takeout-sort] Found {len(links)} file(s) to download.\n")

        downloaded: list[Path] = []
        for i, (href, label) in enumerate(links, 1):
            if progress_cb:
                progress_cb(label or href, i, len(links))

            local_path = _trigger_download(page, context, href, downloads_dir, label)
            if local_path:
                downloaded.append(local_path)
                if conn is not None:
                    _record_download(conn, href, local_path)

        browser.close()

    return downloaded


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _wait_for_takeout_page(page) -> None:
    """
    Block until the user is on the Takeout downloads page.
    Polls every 2 seconds for up to 10 minutes.
    """
    print(
        "\n[takeout-sort] A browser window has opened.\n"
        "  1. Sign in to your Google account if prompted.\n"
        "  2. Navigate to the Takeout download page if not already there:\n"
        "       https://takeout.google.com/settings/takeout/downloads\n"
        "  3. Wait here — the script will detect the page and continue automatically.\n"
    )
    deadline = time.time() + 600  # 10 minutes
    while time.time() < deadline:
        try:
            url = page.url
            if "takeout.google.com" in url and "download" in url.lower():
                # Extra wait for JS-heavy page to finish rendering
                page.wait_for_load_state("networkidle", timeout=15_000)
                print("[takeout-sort] Takeout page detected. Starting downloads...\n")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("Timed out waiting for the Takeout download page (10 min).")


def _collect_download_links(page) -> list[tuple[str, str]]:
    """
    Return a list of (href, label) tuples for all downloadable ZIPs on the page.
    """
    links: list[tuple[str, str]] = []

    # Try each selector
    for sel in _DOWNLOAD_SELECTORS:
        try:
            elements = page.query_selector_all(sel)
            for el in elements:
                href = el.get_attribute("href") or el.get_attribute("data-download-url") or ""
                label = el.text_content().strip() or href.split("/")[-1].split("?")[0]
                if href and href not in [h for h, _ in links]:
                    links.append((href, label))
        except Exception:
            continue

    # Fallback: scrape all <a> tags containing 'takeout' or 'storage.googleapis.com'
    if not links:
        try:
            all_links = page.eval_on_selector_all(
                "a",
                """els => els
                    .filter(e => e.href && (
                        e.href.includes('storage.googleapis.com') ||
                        e.href.includes('takeout')
                    ))
                    .map(e => [e.href, e.textContent.trim()])
                """,
            )
            links = [(href, label) for href, label in all_links if href]
        except Exception:
            pass

    return links


def _trigger_download(
    page,
    context,
    href: str,
    downloads_dir: Path,
    label: str,
) -> Path | None:
    """
    Navigate to *href*, wait for the download to complete, and save it to
    *downloads_dir*.  Returns the saved Path or None on failure.
    """
    try:
        with page.expect_download(timeout=0) as dl_info:
            # Some links open in a new tab, some trigger directly.
            page.goto(href)

        download = dl_info.value
        suggested = download.suggested_filename or label or f"takeout_{int(time.time())}.zip"

        # Strip query strings from suggested name
        safe_name = suggested.split("?")[0].strip() or "takeout.zip"
        dest = downloads_dir / safe_name

        # Handle name collision
        if dest.exists():
            stem = dest.stem
            ext = dest.suffix
            n = 2
            while dest.exists():
                dest = downloads_dir / f"{stem}_{n}{ext}"
                n += 1

        download.save_as(str(dest))
        print(f"  ✓  Downloaded: {dest.name}")
        return dest

    except Exception as e:
        print(f"  ✗  Failed to download {label!r}: {e}")
        return None


def _record_download(conn: sqlite3.Connection, url: str, local_path: Path) -> None:
    try:
        size = local_path.stat().st_size
    except OSError:
        size = None
    conn.execute(
        "INSERT OR REPLACE INTO downloads (url, filename, local_path, status, finished_at) "
        "VALUES (?, ?, ?, 'downloaded', datetime('now'))",
        (url, local_path.name, str(local_path)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------

def extract_zip(
    zip_path: Path,
    destination: Path,
    *,
    delete_after: bool = True,
    progress_cb: Callable[[str], None] | None = None,
) -> Path:
    """
    Extract a Takeout ZIP to *destination* and optionally delete the ZIP.

    Returns the directory it was extracted to.
    """
    import zipfile

    extract_dir = destination / "raw"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        for member in members:
            if progress_cb:
                progress_cb(member.filename)
            zf.extract(member, path=extract_dir)

    if delete_after:
        try:
            zip_path.unlink()
        except OSError:
            pass

    return extract_dir


def check_space_for_zip(
    zip_path: Path,
    destination: Path,
) -> tuple[bool, str]:
    """
    Estimate whether there is enough free space to extract *zip_path*.

    Returns (ok, message).  We assume the extracted size is ~3× the ZIP size
    as a conservative upper bound.
    """
    from .utils import check_space, human_bytes, get_space

    try:
        zip_size = zip_path.stat().st_size
    except OSError:
        return True, ""  # Can't check — proceed

    # Worst-case: raw photos compressed near 1:1, but the ZIP file itself is on
    # the same volume, so we need space for the extracted files only.
    needed = zip_size  # extraction is ~1:1 for already-compressed media
    ok, info = check_space(needed, destination)
    if not ok:
        msg = (
            f"Low disk space: need ~{human_bytes(needed)} free, "
            f"only {human_bytes(info.free_bytes)} available on {destination}."
        )
        return False, msg
    return True, ""

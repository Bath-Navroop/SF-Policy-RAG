"""Download SFPD Department General Orders (DGOs) and write data/manifest.csv.

Run from the repo root:
    uv run python ingest/download.py          # the 10 starter orders
    uv run python ingest/download.py --all    # every order on the listing page

How it works:
1. Read the listing page and turn it into a list of orders.
2. Download each order's page into data/raw/, politely and with caching.
3. Write data/manifest.csv: one row per saved page (what it is, where it came from, when).
"""

import argparse
import csv
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

LISTING_URL = "https://www.sanfranciscopolice.org/your-sfpd/policies/general-orders"

# Tells the site who is making the requests (polite scraping practice).
USER_AGENT = "SF-Policy-RAG/0.1 (student project; github.com/Bath-Navroop/SF-Policy-RAG)"

# The order number inside link text like: DGO5.01 "Use of Force..."  or  DGO 10.11 Body Worn Cameras
ORDER_NUMBER_RE = re.compile(r"DGO\s*(\d+\.\d+)")

# The dates that follow each link, like: (Revised 9/4/24)(Effective 10/19/24)
REVISED_RE = re.compile(r"Revised\s+(\d{1,2}/\d{1,2}/\d{2,4})")
EFFECTIVE_RE = re.compile(r"Effective\s+(\d{1,2}/\d{1,2}/\d{2,4})")

# Where downloaded pages are saved. Relative path, so run the script from the repo root.
RAW_DIR = Path("data/raw")

# The record of what we downloaded. Committed to git (unlike data/raw/).
MANIFEST_PATH = Path("data/manifest.csv")
MANIFEST_FIELDS = ["doc_id", "title", "source_url", "revised_date", "effective_date", "downloaded_at"]

# Seconds to wait before each request, so we don't hammer the city's server.
DELAY_SECONDS = 1.5

# The 10 orders to start with (the examples in PLAN.md, plus 1.08 and 2.01).
STARTER_IDS = [
    "DGO-1.08",   # Community Policing
    "DGO-2.01",   # General Rules of Conduct
    "DGO-2.04",   # Complaints Against Officers
    "DGO-5.01",   # Use of Force
    "DGO-5.05",   # Emergency Response and Pursuit Driving
    "DGO-5.21",   # Crisis Intervention Team
    "DGO-6.09",   # Domestic Violence
    "DGO-6.10",   # Missing Persons
    "DGO-8.03",   # Crowd Control
    "DGO-10.11",  # Body Worn Cameras
]


def fetch(url: str, client: httpx.Client) -> str:
    """Download one page and return its HTML."""
    response = client.get(url)
    response.raise_for_status()  # Fail loudly on 404/500 instead of carrying on with an error page.
    return response.text


def to_iso_date(us_date: str) -> str:
    """Convert '9/4/24' (or '9/4/2024') to '2024-09-04'."""
    year = us_date.split("/")[-1]
    fmt = "%m/%d/%Y" if len(year) == 4 else "%m/%d/%y"
    return datetime.strptime(us_date, fmt).date().isoformat()


def version_date(order: dict) -> str:
    """The date a version took effect: its effective date if listed, otherwise its revised date.

    Dates are ISO strings ('2024-10-19'), which sort correctly as plain text, so we
    can compare them with >= without converting back to date objects.
    """
    return order["effective_date"] or order["revised_date"]


def text_after_link(link: Tag, max_chars: int = 200) -> str:
    """Return the page text that comes after a link, up to the next order link.

    The dates follow each link on the page, e.g.
        DGO5.01 "Use of Force..." (Revised 9/4/24)(Effective 10/19/24)
    but they can sit in a different HTML element from the link (the link may be
    wrapped in <strong>, or the dates may be in the next table cell). So instead
    of only looking at the link's siblings, walk forward through the page in
    reading order (next_elements) and collect text until the next order link.
    """
    parts: list[str] = []
    total = 0
    for element in link.next_elements:
        if isinstance(element, Tag):
            # Stop at the next order's link, so we don't grab its dates.
            if element.name == "a" and "/general-orders/" in element.get("href", ""):
                break
            continue
        # next_elements also visits the link's own text (the title); skip it.
        if any(parent is link for parent in element.parents):
            continue
        parts.append(str(element))
        total += len(element)
        if total >= max_chars:  # Safety limit so the last link doesn't read the whole footer.
            break
    return " ".join(parts)


def get_order_links(html: str) -> list[dict]:
    """Find every general order on the listing page.

    Returns one dict per order with doc_id, title, source_url, revised_date, effective_date.
    """
    soup = BeautifulSoup(html, "html.parser")
    orders: dict[str, dict] = {}  # Keyed by doc_id so a link that appears twice is only kept once.

    for link in soup.find_all("a", href=True):
        if "/general-orders/" not in link["href"]:
            continue

        link_text = link.get_text(" ", strip=True)
        match = ORDER_NUMBER_RE.search(link_text)
        if not match:
            continue  # A link to the section (e.g. in the menu), not to a specific order.

        # Build the ID from the number in the text, not the URL (the URL for 1.08 ends in "1-08-0").
        doc_id = f"DGO-{match.group(1)}"
        title = link_text[match.end():].strip().strip('"“”').strip()
        dates = text_after_link(link)
        revised = REVISED_RE.search(dates)
        effective = EFFECTIVE_RE.search(dates)

        order = {
            "doc_id": doc_id,
            "title": title,
            "source_url": urljoin(LISTING_URL, link["href"]),  # Turns a relative link into a full URL.
            "revised_date": to_iso_date(revised.group(1)) if revised else "",
            "effective_date": to_iso_date(effective.group(1)) if effective else "",
        }

        # Some orders are listed twice: an old version and a newer one (e.g. 5.08 at
        # /5-08 from 1996 and at /5-08-0 from 2026). Keep only the newest version.
        existing = orders.get(doc_id)
        if existing:
            if version_date(existing) >= version_date(order):
                continue  # The one we already have is the same or newer.
            print(
                f"  Note: {doc_id} is listed twice; using the {version_date(order)} version, "
                f"not the {version_date(existing) or 'undated'} one."
            )
        orders[doc_id] = order

    return list(orders.values())


def raw_path(doc_id: str) -> Path:
    """Where an order's page is saved, e.g. data/raw/DGO-5.01.html."""
    return RAW_DIR / f"{doc_id}.html"


def download_order(order: dict, client: httpx.Client) -> bool:
    """Save one order's page to data/raw/.

    Returns True if it downloaded the page, False if a saved copy already existed.
    We keep the raw HTML exactly as downloaded, so we can re-run parsing as often
    as we like without downloading again.
    """
    path = raw_path(order["doc_id"])
    if path.exists():
        return False  # Cached: never download the same page twice.

    time.sleep(DELAY_SECONDS)  # Be polite: pause before every request.
    html = fetch(order["source_url"], client)
    path.write_text(html, encoding="utf-8")
    return True


def downloaded_at(doc_id: str) -> str:
    """When an order's page was saved, from the file's modification time, in UTC.

    Using the file's own timestamp means a page downloaded last week and skipped
    today still shows last week's date, which is when we actually got it.
    """
    mtime = raw_path(doc_id).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


def write_manifest(orders: list[dict]) -> int:
    """Write data/manifest.csv with one row per order that has a saved page.

    Returns the number of rows written.
    """
    rows = [
        {**order, "downloaded_at": downloaded_at(order["doc_id"])}  # Copy the dict and add one field.
        for order in orders
        if raw_path(order["doc_id"]).exists()
    ]
    # newline="" lets the csv module control line endings itself (avoids blank lines on some systems).
    with MANIFEST_PATH.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download SFPD General Orders.")
    parser.add_argument(
        "--all", action="store_true", help="download every order, not just the 10 starter orders"
    )
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)  # Create data/raw/ if it doesn't exist yet.

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        all_orders = get_order_links(fetch(LISTING_URL, client))
        print(f"Found {len(all_orders)} general orders on the listing page.")

        orders = all_orders  # The ones to download this run.
        if not args.all:
            by_id = {order["doc_id"]: order for order in all_orders}
            missing = [doc_id for doc_id in STARTER_IDS if doc_id not in by_id]
            if missing:
                print(f"Warning: not on the listing page: {missing}")
            orders = [by_id[doc_id] for doc_id in STARTER_IDS if doc_id in by_id]

        failed = []
        for order in orders:
            doc_id = order["doc_id"]
            try:
                downloaded = download_order(order, client)
            except httpx.HTTPError as error:
                # One broken page shouldn't stop the whole run; note it and move on.
                print(f"  {doc_id:<10} FAILED: {error}")
                failed.append(doc_id)
                continue
            size_kb = raw_path(doc_id).stat().st_size / 1024
            status = "downloaded" if downloaded else "already saved, skipped"
            print(f"  {doc_id:<10} {size_kb:5.0f} KB  {status}")

    print(f"Done: {len(orders) - len(failed)} saved in {RAW_DIR}/, {len(failed)} failed.")
    if failed:
        print(f"Failed: {failed} (run again to retry just these)")

    # Built from ALL orders, so the manifest lists every saved page, not just this run's.
    count = write_manifest(all_orders)
    print(f"Wrote {MANIFEST_PATH} with {count} rows.")


if __name__ == "__main__":
    main()

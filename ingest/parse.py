"""Turn saved DGO pages (data/raw/*.html) into clean, structured text (data/parsed/*.json).

Run from the repo root:
    uv run python -m ingest.parse                      # the 10 starter orders
    uv run python -m ingest.parse --all                # every downloaded order
    uv run python -m ingest.parse DGO-10.11 --print    # one order, printed so you can read it

What the saved pages look like (checked 2026-09-27 across all 113):
- The policy text is always in <div id="policy-content"> ... <div class="field--name-body">.
- Section headings are <h3> (or <h2> in some orders), either new-style
  "10.11.05 ACTIVATION OF BODY WORN CAMERAS" or old-style "III. PROCEDURES".
- Sub-items are nested <ol> lists. Their letters/numbers (A., 1., a.) are NOT in the
  text; the browser draws them from CSS. So we rebuild the labels ourselves, because
  the documents refer to them ("see 10.11.05 C") and citations need them.

Output per order, e.g. data/parsed/DGO-10.11.json:
    {"doc_id": ..., "title": ..., "source_url": ..., "revised_date": ..., "effective_date": ...,
     "sections": [{"number": "10.11.05", "heading": "10.11.05 ACTIVATION OF ...",
                   "text": "A. Members must ...\\n  1. A response to ..."}]}
"""

import argparse
import csv
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ingest.download import MANIFEST_PATH, RAW_DIR, STARTER_IDS

PARSED_DIR = Path("data/parsed")

HEADING_TAGS = ("h2", "h3")

# Finds the number at the start of a heading. Real headings are messy, so it allows:
#   "10.11.05 PURPOSE", "6.14.03LEGAL STANDARDS" (no space), "III. PROCEDURES",
#   "II.PROCEDURES" (no space) and "Ill. PROCEDURES" (lowercase L typo for III, in DGO 8.07).
# Roman numerals must end with "." so a title word like "IV" alone isn't mistaken for one.
HEADING_NUMBER_RE = re.compile(r"^(\d+\.\d+\.\d+|[IVXLCl]+\.)\s*(.*)$")

# Label style for each nesting depth when a list has no class telling us.
# Matches the usual legal outline: A. -> 1. -> a. -> (1) -> (a)
DEFAULT_STYLES = ["A", "1", "a", "(1)", "(a)"]


# ---------- Small text helpers ----------


def clean_inline(text: str) -> str:
    """Collapse all whitespace (spaces, newlines, non-breaking spaces) into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def clean_block(text: str) -> str:
    """Like clean_inline, but keep line breaks (from <br>) and drop empty lines."""
    lines = (clean_inline(line) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


# ---------- Rebuilding list labels ----------


def list_style(ol: Tag, depth: int) -> str:
    """Decide how an <ol> is numbered: 'A', '1', 'a', '(1)' or '(a)'.

    The site marks some lists with a CSS class ("alpha" or "num"); otherwise we
    fall back to the usual outline style for that depth.
    """
    classes = ol.get("class") or []
    if "num" in classes:
        return "1"
    if "alpha" in classes:
        return "A" if depth == 0 else "a"
    return DEFAULT_STYLES[min(depth, len(DEFAULT_STYLES) - 1)]


def make_label(n: int, style: str) -> str:
    """Turn item number n (1, 2, 3...) into a label like 'C.', '3.', 'c.', '(3)'."""
    letter_ok = 1 <= n <= 26
    if style == "A" and letter_ok:
        return f"{chr(ord('A') + n - 1)}."
    if style == "a" and letter_ok:
        return f"{chr(ord('a') + n - 1)}."
    if style == "(1)":
        return f"({n})"
    if style == "(a)" and letter_ok:
        return f"({chr(ord('a') + n - 1)})"
    return f"{n}."  # "1" style, or a fallback if a list somehow has more than 26 items.


def render_list(lst: Tag, depth: int, lines: list[str]) -> None:
    """Add one line per list item to `lines`, indented by depth, with rebuilt labels.

    Calls itself for nested lists (recursion), going one level deeper each time.
    """
    ordered = lst.name == "ol"
    style = list_style(lst, depth) if ordered else ""
    start = int(lst.get("start", 1)) if ordered else 1

    for index, item in enumerate(lst.find_all("li", recursive=False)):
        label = make_label(start + index, style) if ordered else "-"

        # The item's own text, without the text of any list nested inside it.
        own_parts = [
            child.get_text(" ") if isinstance(child, Tag) else str(child)
            for child in item.children
            if not (isinstance(child, Tag) and child.name in ("ol", "ul"))
        ]
        text = clean_inline(" ".join(own_parts))
        if text:
            lines.append(f"{'  ' * depth}{label} {text}")

        for nested in item.find_all(["ol", "ul"], recursive=False):
            render_list(nested, depth + 1, lines)


def cell_text(cell: Tag) -> str:
    """Text of one table cell. Bullet lists inside a cell become 'item; item; item'."""
    items = cell.find_all("li")
    if items:
        return "; ".join(clean_inline(item.get_text(" ")) for item in items)
    return clean_inline(cell.get_text(" "))


def render_table(table: Tag, lines: list[str]) -> None:
    """One line per table row, each value labelled with its column header.

    e.g. "Subject's Actions: Compliance | Description: ... | Possible Force Option: ..."
    Labelling every row makes each line understandable on its own, which matters
    later when a chunk might contain only part of a table.
    """
    rows = table.find_all("tr")
    if not rows:
        return
    headers: list[str] = []
    if rows[0].find("th"):  # All tables in the DGOs have a header row, but check anyway.
        headers = [cell_text(cell) for cell in rows[0].find_all(["th", "td"])]
        rows = rows[1:]

    for row in rows:
        cells = [cell_text(cell) for cell in row.find_all(["th", "td"])]
        if not any(cells):
            continue
        if headers and len(headers) == len(cells):
            parts = [f"{header}: {value}" for header, value in zip(headers, cells) if value]
        else:
            parts = [value for value in cells if value]
        lines.append(" | ".join(parts))


# ---------- Parsing one page ----------


def find_body(soup: BeautifulSoup) -> Tag:
    """Return the <div> holding the policy text (skipping the 'request a PDF' notice)."""
    for div in soup.select("#policy-content div.field--name-body"):
        if not div.find_parent(id="policy-disclaimer"):
            return div
    raise ValueError("policy text not found (page layout may have changed)")


def iter_blocks(container: Tag):
    """Yield the top-level pieces of the policy text (paragraphs, headings, lists...) in order.

    Normally these are just the body's direct children. But DGO 6.16 wraps several
    headings and lists inside one <p>, so we step inside any element that contains
    a heading. `yield from` hands back everything the inner call yields.
    """
    for element in container.children:
        if (
            isinstance(element, Tag)
            and element.name not in HEADING_TAGS
            and element.find(HEADING_TAGS)
        ):
            yield from iter_blocks(element)
        else:
            yield element


def heading_number(heading_text: str) -> str:
    """'10.11.05 PURPOSE' -> '10.11.05', 'III. PROCEDURES' -> 'III', 'Ill. X' -> 'III'."""
    match = HEADING_NUMBER_RE.match(heading_text)
    if not match:
        return ""
    number = match.group(1).rstrip(".")
    if not number[0].isdigit():
        number = number.replace("l", "I")  # Fix the "Ill" typo.
    return number


def parse_sections(html: str) -> list[dict]:
    """Split a page's policy text into sections, one per heading."""
    soup = BeautifulSoup(html, "html.parser")
    body = find_body(soup)

    for br in body.find_all("br"):
        br.replace_with("\n")  # Keep <br> line breaks as real newlines.

    sections: list[dict] = []
    current = {"number": "", "heading": "", "lines": []}  # Text before the first heading, if any.

    for element in iter_blocks(body):
        if isinstance(element, NavigableString):
            if element.strip():
                current["lines"].append(clean_inline(element))
            continue

        if element.name in HEADING_TAGS:
            sections.append(current)
            heading_text = clean_inline(element.get_text(" "))
            current = {"number": heading_number(heading_text), "heading": heading_text, "lines": []}
        elif element.name in ("ol", "ul"):
            render_list(element, 0, current["lines"])
        elif element.name == "table":
            render_table(element, current["lines"])
        elif element.name == "hr":
            continue
        else:  # <p>, <div> and anything else: keep its text.
            text = clean_block(element.get_text(" "))
            if text:
                current["lines"].append(text)

    sections.append(current)

    # Turn the collected lines into one text block per section; drop empty sections.
    return [
        {"number": s["number"], "heading": s["heading"], "text": "\n".join(s["lines"])}
        for s in sections
        if s["lines"] or s["heading"]
    ]


# ---------- Running it ----------


def load_manifest() -> dict[str, dict]:
    """Read data/manifest.csv into {doc_id: row}."""
    with MANIFEST_PATH.open(newline="", encoding="utf-8") as file:
        return {row["doc_id"]: row for row in csv.DictReader(file)}


def parse_order(meta: dict) -> dict:
    """Parse one saved page and combine it with its manifest info."""
    html = (RAW_DIR / f"{meta['doc_id']}.html").read_text(encoding="utf-8")
    return {
        "doc_id": meta["doc_id"],
        "title": meta["title"],
        "source_url": meta["source_url"],
        "revised_date": meta["revised_date"],
        "effective_date": meta["effective_date"],
        "sections": parse_sections(html),
    }


def print_order(doc: dict) -> None:
    """Print a parsed order in a readable form, for checking by eye."""
    print(
        f"# {doc['doc_id']} {doc['title']} (effective {doc['effective_date'] or '?'}, "
        f"revised {doc['revised_date'] or '?'})\n"
    )
    for section in doc["sections"]:
        print(f"## {section['heading'] or '(text before the first heading)'}\n")
        # A blank line after every line, so a Markdown preview shows each item on its own
        # line (Markdown joins lines that aren't separated by a blank line).
        print("\n\n".join(section["text"].split("\n")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse saved DGO pages into structured text.")
    parser.add_argument(
        "doc_ids",
        nargs="*",
        help="orders to parse, e.g. DGO-10.11 (default: the 10 starter orders)",
    )
    parser.add_argument("--all", action="store_true", help="parse every order in the manifest")
    parser.add_argument("--print", action="store_true", help="print the parsed text to read by eye")
    args = parser.parse_args()

    manifest = load_manifest()
    doc_ids = list(manifest) if args.all else (args.doc_ids or STARTER_IDS)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for doc_id in doc_ids:
        if doc_id not in manifest:
            print(f"  {doc_id:<10} not in the manifest; run ingest/download.py first")
            continue
        doc = parse_order(manifest[doc_id])
        out_path = PARSED_DIR / f"{doc_id}.json"
        out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1

        words = sum(len(s["text"].split()) for s in doc["sections"])
        numbered = sum(1 for s in doc["sections"] if s["number"])
        warning = "  <-- check this one" if numbered == 0 or words < 100 else ""
        print(
            f"  {doc_id:<10} {len(doc['sections']):3} sections ({numbered} numbered) "
            f"{words:6} words{warning}"
        )

        if args.print:
            print()
            print_order(doc)

    print(f"Done: wrote {written} files to {PARSED_DIR}/")


if __name__ == "__main__":
    main()

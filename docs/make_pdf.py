"""Convert docs/showcase.html to docs/showcase.pdf in one command.

Two-second setup:
    pip install playwright
    playwright install chromium

Then:
    python docs/make_pdf.py

The showcase is a portrait product brochure, so it's rendered at A4 portrait.
The actual call-brief deck (rendered by app/services.py:render_deck) uses
A4 landscape — see api/index.py:briefing_pdf for that pipeline.

Why Playwright (not weasyprint): Playwright renders the page in real Chromium,
so the Google Fonts (Poppins / Lato / DM Mono), gradients, and grid layouts
come out identical to how they look in your browser. Weasyprint is lighter
but has rough edges with modern CSS.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows consoles default to cp1252 which can't print Unicode arrows / check
# marks. Force UTF-8 on stdout/stderr so the script's own progress messages
# don't crash. (The PDF rendering itself is unaffected; it runs in Chromium.)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

DOCS = Path(__file__).resolve().parent
SRC = DOCS / "showcase.html"
OUT = DOCS / "showcase.pdf"


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run:\n  pip install playwright\n  playwright install chromium", file=sys.stderr)
        return 1

    if not SRC.exists():
        print(f"Source not found: {SRC}", file=sys.stderr)
        return 1

    url = SRC.as_uri()
    print(f"-> Rendering {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        page.pdf(
            path=str(OUT),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            prefer_css_page_size=False,
        )
        browser.close()

    print(f"OK Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

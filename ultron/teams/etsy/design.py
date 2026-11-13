"""Designer agent: writes a structured spec; the renderer turns it into a clean PDF and listing image."""
from __future__ import annotations

import json
import re

from ...render import make_listing_image, render_pdf
from . import mocks

SYSTEM = """You design printable PDF products sold on Etsy. You output a layout spec, not prose.
The product must be genuinely useful: real fields a buyer needs, sensible column names, clear headings.
Layout rules:
- 2 to 6 pages. Page 1 can be a short "how to use" page; the rest are the fill-in pages.
- Block types:
  {"type": "text", "text": "..."}                                   short guidance, max 60 words
  {"type": "lines", "label": "...", "count": 4-20}                  ruled writing lines
  {"type": "checklist", "title": "...", "items": ["..."], "blank": 0-10}
  {"type": "table", "title": "...", "columns": ["..."], "widths": [relative numbers], "rows": 5-25}
- Tables: 2 to 7 columns, short column names (1-3 words).
- One accent color as #RRGGBB, calm and printable (avoid neon)."""

PROMPT = """{lessons}

Niche: "{keyword}"
Product type: {product_type}
Buyer and rationale: {rationale}
{variation}{notes}
Return:
{{"title": "product name, max 6 words", "subtitle": "one sentence benefit",
"accent_color": "#RRGGBB", "pages": [{{"heading": "...", "intro": "optional one sentence", "blocks": [...]}}]}}"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


class Designer:
    def __init__(self, team):
        self.team = team

    def run(self, niche, notes: str | None = None) -> int:
        db = self.team.db
        previous = db.all("SELECT title FROM assets WHERE niche_id = ?", niche["id"])
        variation = ""
        if previous:
            why = "This niche is selling. " if niche["status"] == "scale" else ""
            variation = (f"{why}Make a clearly distinct version (different angle, layout, style or audience) "
                         f"from the existing versions: {', '.join(r['title'] for r in previous)}.")
        spec = self.team.ask(
            "designer", SYSTEM,
            PROMPT.format(lessons=self.team.lessons_block(), keyword=niche["keyword"],
                          product_type=niche["product_type"], rationale=niche["rationale"] or "",
                          variation=variation,
                          notes=f"\nOwner's directions (follow them): {notes}" if notes else ""),
            tier="fast", max_tokens=3000,
            mock=lambda: mocks.design(niche["keyword"], niche["product_type"], seed=niche["id"] + len(previous)),
        )
        if not spec.get("pages"):
            raise ValueError("designer returned a spec with no pages")

        folder = self.team.ctx.out / "assets" / "etsy" / f"{niche['id']:04d}-{slug(spec.get('title') or niche['keyword'])}"
        if previous:
            folder = folder.with_name(folder.name + f"-v{len(previous) + 1}")
        pdf = folder / f"{slug(spec.get('title') or niche['keyword'])}.pdf"
        pages = render_pdf(spec, pdf)
        preview = make_listing_image(pdf, spec, pages, folder / "listing-image.png")
        (folder / "spec.json").write_text(json.dumps(spec, indent=2))

        asset_id = db.insert("assets", team="etsy", niche_id=niche["id"], kind=niche["product_type"],
                             title=spec.get("title", niche["keyword"]), file_path=str(pdf),
                             preview_path=str(preview), spec_json=json.dumps(spec))
        if niche["status"] == "candidate":
            db.update("niches", niche["id"], status="designed")
        return asset_id

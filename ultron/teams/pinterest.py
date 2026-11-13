"""Pinterest distribution agent: turns live Etsy listings into pins that send traffic back to them."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone

from ..guard import BudgetExceeded
from ..integrations.pinterest_api import PinterestAPI
from ..render import make_pin_image
from .base import Team

SYSTEM = """You write Pinterest pins that drive clicks to an Etsy printable listing.
Pinterest is a search engine: write for the phrases people type, not for hype.
- Title: max 100 characters, starts with the main search phrase.
- Description: 2-3 natural sentences, max 450 characters, include 3-5 relevant search phrases naturally,
  end with a soft call to action. No hashtags, no emojis, no fake urgency.
- Alt text: max 300 characters, describe what the image shows for someone who can't see it.
- Headline: max 7 words, printed on the pin image."""

PROMPT = """{lessons}

Listing title: {title}
Niche search phrase: {keyword}
Price: ${price:.2f}
{previous}
Return: {{"title": "...", "description": "...", "alt_text": "...", "headline": "..."}}"""


class PinterestTeam(Team):
    name = "pinterest"
    role = "support"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.api = PinterestAPI(ctx)

    def register(self) -> None:
        self.ctx.approvals.register("pinterest.create_pin", self.publish)

    def _targets(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.cfg["repin_after_days"])).isoformat()
        return self.db.all(
            """SELECT l.*, a.file_path, a.spec_json, n.keyword FROM listings l
               JOIN assets a ON a.id = l.asset_id JOIN niches n ON n.id = a.niche_id
               WHERE l.platform = 'etsy' AND l.status = 'active' AND l.url IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM pins p WHERE p.listing_id = l.id
                               AND (p.ts > ? OR p.status = 'queued'))
               ORDER BY l.id LIMIT ?""", cutoff, self.cfg["pins_per_cycle"])

    def run_cycle(self) -> dict:
        summary = {"queued_pins": 0}
        targets = self._targets()
        if not targets:
            summary["note"] = "no active listings with a URL need a pin"
            return summary
        try:
            for listing in targets:
                previous = [r["title"] for r in self.db.all("SELECT title FROM pins WHERE listing_id = ?", listing["id"])]
                copy = self.ask(
                    "pin_writer", SYSTEM,
                    PROMPT.format(lessons=self.lessons_block(), title=listing["title"], keyword=listing["keyword"],
                                  price=listing["price_usd"] or 0,
                                  previous=f"Earlier pins (write a different angle): {previous}" if previous else ""),
                    mock=lambda: {"title": f"{listing['keyword'].title()} | Printable PDF"[:100],
                                  "description": f"Keep track of everything with this {listing['keyword']}. "
                                                 "Print it at home and start using it today. Tap to see all pages.",
                                  "alt_text": f"Preview of a printable {listing['keyword']} PDF page with a table to fill in.",
                                  "headline": listing["keyword"].replace(" printable", "").title()},
                )
                spec = json.loads(listing["spec_json"])
                pin_id = self.db.insert("pins", listing_id=listing["id"], title=str(copy.get("title", ""))[:100],
                                        status="queued")
                image = make_pin_image(self.ctx.path(listing["file_path"]), str(copy.get("headline") or spec.get("title", "")),
                                       spec.get("accent_color"),
                                       self.ctx.out / "assets" / "pinterest" / f"pin-{pin_id:04d}.png")
                self.db.update("pins", pin_id, image_path=str(image))
                payload = {"pin_id": pin_id, "listing_id": listing["id"], "link": listing["url"],
                           "title": str(copy.get("title", ""))[:100],
                           "description": str(copy.get("description", ""))[:500],
                           "alt_text": str(copy.get("alt_text", ""))[:500], "image_path": str(image),
                           "_on_reject": ["pins", pin_id]}
                self.ctx.approvals.queue("pinterest", "pinterest.create_pin",
                                         f"Pin \"{payload['title']}\" -> {listing['url']}  |  image: {image}", payload)
                summary["queued_pins"] += 1
        except BudgetExceeded as err:
            summary["stopped"] = str(err)
        return summary

    def publish(self, payload: dict) -> dict:
        if self.api.can_post:
            pin = self.api.create_pin(title=payload["title"], description=payload["description"],
                                      link=payload["link"], alt_text=payload["alt_text"],
                                      image=self.ctx.path(payload["image_path"]))
            self.db.update("pins", payload["pin_id"], external_id=str(pin.get("id")), status="posted")
            return {"message": f"Pin {pin.get('id')} posted", "pin_id": pin.get("id")}

        folder = self.ctx.out / "packets" / "pinterest" / f"pin-{payload['pin_id']:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.ctx.path(payload["image_path"]), folder)
        (folder / "pin.md").write_text(
            f"# {payload['title']}\n\nLink: {payload['link']}\n\n## Description\n\n{payload['description']}\n\n"
            f"## Alt text\n\n{payload['alt_text']}\n")
        self.db.update("pins", payload["pin_id"], status="manual_pending")
        return {"message": f"Manual pin packet written to {folder} (no Pinterest token)", "packet": str(folder)}

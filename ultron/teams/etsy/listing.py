"""Lister agent: writes SEO copy, enforces Etsy's AI disclosure, queues the listing for your approval.
Executor: publishes through the Etsy API, or builds a manual upload packet when no OAuth token exists."""
from __future__ import annotations

import json
import re
import shutil

from . import mocks

SYSTEM = """You write Etsy listings for printable PDF products. Optimize for Etsy search without keyword stuffing.
- Title: max 140 characters, lead with the exact phrase buyers search, then 2-3 useful descriptors.
- Tags: exactly 13, each max 20 characters, lowercase, multi-word phrases buyers actually type, no repeats.
- Description: plain text, first sentence restates the search phrase naturally; then WHAT YOU GET, HOW IT WORKS.
  Do not mention AI (a required disclosure is appended automatically). No emojis, no claims you can't back up.
- Price: a number in USD between 2.99 and 9.99."""

PROMPT = """{lessons}

Product: {title}
Subtitle: {subtitle}
Niche search phrase: {keyword}
Pages: {page_list}
Return: {{"title": "...", "tags": ["13 tags"], "description": "...", "price_usd": 4.99}}"""

DISCLOSURE = ("\n\nHOW THIS WAS MADE\nI create these printables with the help of AI tools, and I review every design "
              "myself before it is listed.\n\nThis is a digital download. No physical item will be shipped. "
              "For personal use only.")


def clean_tags(tags) -> list[str]:
    out = []
    for tag in tags or []:
        tag = re.sub(r"[^a-z0-9 '\-]", "", str(tag).lower()).strip()
        tag = re.sub(r"\s+", " ", tag)
        if len(tag) > 20:   # Etsy's limit; cut at a word boundary, never mid-word
            tag = tag[:21].rsplit(" ", 1)[0] if " " in tag[:21] else ""
        if tag and tag not in out:
            out.append(tag)
    return out[:13]


def clean_title(title: str) -> str:
    title = re.sub(r"[%^~$]", "", str(title))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:140].rstrip(" ,-|")


def _try(fn):
    """Best-effort lookup: None if Etsy refuses it, so publishing can still go ahead."""
    try:
        return fn()
    except Exception:
        return None


class Lister:
    def __init__(self, team):
        self.team = team

    def run(self, asset_id: int) -> int:
        db, cfg = self.team.db, self.team.cfg
        asset = db.one("SELECT * FROM assets WHERE id = ?", asset_id)
        niche = db.one("SELECT * FROM niches WHERE id = ?", asset["niche_id"])
        spec = json.loads(asset["spec_json"])
        copy = self.team.ask(
            "lister", SYSTEM,
            PROMPT.format(lessons=self.team.lessons_block(), title=spec.get("title"), subtitle=spec.get("subtitle", ""),
                          keyword=niche["keyword"], page_list=", ".join(p.get("heading", "") for p in spec["pages"])),
            tier="fast",
            mock=lambda: mocks.listing(niche["keyword"], cfg["default_price_usd"]),
        )
        title = clean_title(copy.get("title") or spec.get("title"))
        tags = clean_tags(copy.get("tags"))
        try:
            price = min(max(float(copy.get("price_usd") or cfg["default_price_usd"]), 1.0), 50.0)
        except (TypeError, ValueError):
            price = cfg["default_price_usd"]
        description = str(copy.get("description", "")).strip() + DISCLOSURE

        listing_id = db.insert("listings", team="etsy", asset_id=asset_id, platform="etsy", title=title,
                               price_usd=price, status="queued")
        payload = {"listing_id": listing_id, "asset_id": asset_id, "title": title, "tags": tags,
                   "price_usd": price, "description": description,
                   "file_path": asset["file_path"], "preview_path": asset["preview_path"],
                   "_on_reject": ["listings", listing_id]}
        self.team.ctx.approvals.queue(
            "etsy", "etsy.publish_listing",
            f"List \"{title}\" at ${price:.2f}  |  preview: {asset['preview_path']}", payload)
        return listing_id

    # --- executor (runs only after approval) -------------------------------------

    def publish(self, payload: dict) -> dict:
        ctx, db, cfg, api = self.team.ctx, self.team.db, self.team.cfg, self.team.api
        listing_id = payload["listing_id"]
        if api.can_write and cfg.get("taxonomy_id"):
            asset = db.one("SELECT kind FROM assets WHERE id = ?", payload.get("asset_id"))
            taxonomy = (cfg.get("taxonomy_by_type") or {}).get(asset["kind"] if asset else None) or cfg["taxonomy_id"]
            # Resumable: reuse a draft from an earlier, half-finished attempt instead of making a duplicate.
            row = db.one("SELECT external_id, url FROM listings WHERE id = ?", listing_id)
            etsy_id = (row["external_id"] if row else None) or _try(lambda: api.find_draft(payload["title"]))
            if etsy_id:
                url = (row["url"] if row and row["url"] else None) or f"https://www.etsy.com/listing/{etsy_id}"
                api.update_listing(etsy_id, title=payload["title"], description=payload["description"],
                                   price=round(float(payload["price_usd"]), 2), tags=payload["tags"])
            else:
                created = api.create_draft_listing(title=payload["title"], description=payload["description"],
                                                   price=payload["price_usd"], tags=payload["tags"],
                                                   taxonomy_id=int(taxonomy))
                etsy_id = str(created["listing_id"])
                url = created.get("url") or f"https://www.etsy.com/listing/{etsy_id}"
            db.update("listings", listing_id, external_id=etsy_id, url=url)   # saved before uploads
            if not _try(lambda: api.listing_images(etsy_id)):
                api.upload_image(etsy_id, ctx.path(payload["preview_path"]))
            if not _try(lambda: api.listing_files(etsy_id)):
                api.upload_file(etsy_id, ctx.path(payload["file_path"]))
            status = "draft"
            if cfg.get("publish_state") == "active":
                api.update_listing(etsy_id, state="active")
                db.insert("expenses", team="etsy", kind="listing_fee", amount_usd=cfg["listing_fee_usd"],
                          note=f"listing {etsy_id}")
                status = "active"
            db.update("listings", listing_id, status=status)
            return {"message": f"Etsy {status} listing {etsy_id} created", "etsy_listing_id": etsy_id}

        # Manual mode: everything you need to upload by hand, in one folder.
        folder = ctx.out / "packets" / "etsy" / f"listing-{listing_id:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(ctx.path(payload["file_path"]), folder)
        shutil.copy(ctx.path(payload["preview_path"]), folder)
        (folder / "listing.md").write_text(
            f"# {payload['title']}\n\nPrice: ${payload['price_usd']:.2f}\n\n"
            f"Tags (13): {', '.join(payload['tags'])}\n\n"
            "Etsy settings: Digital download · Who made it: I did · What is it: A finished product · "
            "When was it made: Made to order · Creativity: Designed by a seller (disclose AI use)\n\n"
            f"## Description\n\n{payload['description']}\n\n---\n"
            f"After it's live, run:\n  python -m ultron link {listing_id} --url <etsy listing url>\n")
        db.update("listings", listing_id, status="manual_pending")
        why = "no Etsy OAuth token" if not api.can_write else "taxonomy_id not set in config.yaml"
        return {"message": f"Manual packet written to {folder} ({why})", "packet": str(folder)}

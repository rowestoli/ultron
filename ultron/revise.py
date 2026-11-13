"""Owner edits before approval.

Two ways in:
  apply_changes() - direct edits (title, price, tags, description) from the HQ form or the CLI
  revise()        - a plain-English request ("make it $2.99", "darker green, add a notes page")
                    that the AI turns into field changes and, if needed, a redrawn printable or pin image
"""
from __future__ import annotations

import json
import re

from .render import make_listing_image, make_pin_image, render_pdf
from .teams.etsy.design import SYSTEM as DESIGN_SYSTEM
from .teams.etsy.listing import DISCLOSURE, clean_tags, clean_title

EDITABLE = {"pending", "approved"}

LISTING_SYSTEM = """You help the owner revise an Etsy printable listing before it is published.
Do exactly what the owner asks and nothing else. Keep everything they didn't mention unchanged.
Etsy rules: title max 140 characters; exactly 13 tags, each max 20 characters, lowercase; no trademarks or brand names;
the description is plain text and must not mention AI (a disclosure is added automatically).
If the owner wants the printable itself changed (colors, pages, fields, columns, layout, wording on the pages),
put a precise instruction for the designer in "redesign"; otherwise set it to null."""

LISTING_PROMPT = """Current listing:
title: {title}
price_usd: {price}
tags: {tags}
description:
{description}

The printable: "{spec_title}", accent color {accent}, pages: {pages}

Conversation so far:
{history}

Owner's new request: {instruction}

Return: {{"reply": "1-2 sentences telling the owner what you changed (or why you couldn't)",
"changes": {{"title": "...", "price_usd": 3.99, "tags": ["..."], "description": "..."}},
"redesign": null}}
Only include keys in "changes" that should change."""

REDESIGN_PROMPT = """Here is the current layout spec of the printable:
{spec}

Apply this change requested by the owner: {instruction}
Keep everything else the same. Return the complete updated spec in the same JSON format."""

PIN_SYSTEM = """You help the owner revise a Pinterest pin before it is posted. Do exactly what they ask, nothing else.
Limits: title max 100 characters; description max 450 characters, no hashtags or emojis; alt text max 300 characters;
headline (the text printed on the pin image) max 7 words."""

PIN_PROMPT = """Current pin:
title: {title}
description: {description}
alt_text: {alt_text}
headline on image: {headline}

Conversation so far:
{history}

Owner's new request: {instruction}

Return: {{"reply": "1-2 sentences telling the owner what you changed",
"changes": {{"title": "...", "description": "...", "alt_text": "...", "headline": "..."}}}}
Only include keys in "changes" that should change."""

COLORS = {"red": "#B5523B", "green": "#4F7A3A", "dark green": "#2F5A3A", "blue": "#2E5E8C", "navy": "#24365E",
          "purple": "#7A4E9E", "pink": "#C0507A", "orange": "#C8742A", "yellow": "#B8962A", "teal": "#2F6F62",
          "gray": "#5A6070", "grey": "#5A6070", "black": "#2B2B2B", "brown": "#7A5230"}


class EditError(ValueError):
    pass


def _load(ctx, approval_id: int):
    row = ctx.db.one("SELECT * FROM approvals WHERE id = ?", approval_id)
    if row is None:
        raise EditError(f"No item #{approval_id}")
    if row["status"] not in EDITABLE:
        raise EditError(f"#{approval_id} is already {row['status']} and can't be edited")
    return row, json.loads(row["payload_json"])


def body_of(description: str) -> str:
    """Description without the auto-added disclosure (what the owner and the AI actually edit)."""
    return (description or "").replace(DISCLOSURE, "").rstrip()


def _asset_for(ctx, payload: dict):
    if payload.get("asset_id"):
        return ctx.db.one("SELECT * FROM assets WHERE id = ?", payload["asset_id"])
    listing = ctx.db.one("SELECT asset_id FROM listings WHERE id = ?", payload.get("listing_id"))
    return ctx.db.one("SELECT * FROM assets WHERE id = ?", listing["asset_id"]) if listing else None


def apply_changes(ctx, approval_id: int, changes: dict, source: str = "owner") -> dict:
    """Validate and save field edits. Returns the fields that changed."""
    row, p = _load(ctx, approval_id)
    applied = {}
    if row["action"] == "etsy.publish_listing":
        if changes.get("title"):
            title = clean_title(changes["title"])
            if title != p["title"]:
                p["title"] = applied["title"] = title
        if changes.get("price_usd") not in (None, ""):
            try:
                price = round(float(str(changes["price_usd"]).replace("$", "")), 2)
            except ValueError:
                raise EditError("Price must be a number")
            if not 0.20 <= price <= 500:
                raise EditError("Price must be between $0.20 and $500")
            old = float(p.get("price_usd") or 0)
            if abs(old - price) >= 0.005:
                p["price_usd"] = applied["price_usd"] = price
                asset = _asset_for(ctx, p)
                kind = asset["kind"] if asset else "printable"
                ctx.db.add_lesson("etsy", f"Owner changed the price of a {kind} (\"{p['title'][:60]}\") "
                                          f"from ${old:.2f} to ${price:.2f}.", source="owner")
        if changes.get("tags") is not None:
            tags = changes["tags"].split(",") if isinstance(changes["tags"], str) else changes["tags"]
            tags = clean_tags(tags)
            if not tags:
                raise EditError("Add at least one tag")
            if tags != p.get("tags"):
                p["tags"] = applied["tags"] = tags
        if changes.get("description"):
            desc = body_of(str(changes["description"]).strip()) + DISCLOSURE
            if desc != p.get("description"):
                p["description"] = desc
                applied["description"] = True
        if p.get("listing_id"):
            ctx.db.update("listings", p["listing_id"], title=p["title"], price_usd=p["price_usd"])
        summary = f"List \"{p['title']}\" at ${p['price_usd']:.2f}  |  preview: {p['preview_path']}"
    elif row["action"] == "pinterest.create_pin":
        for key, limit in (("title", 100), ("description", 500), ("alt_text", 500)):
            if changes.get(key) and str(changes[key]).strip()[:limit] != p.get(key):
                p[key] = applied[key] = str(changes[key]).strip()[:limit]
        if changes.get("headline") and changes["headline"] != p.get("headline"):
            listing = ctx.db.one("SELECT l.*, a.file_path, a.spec_json FROM listings l JOIN assets a ON a.id = l.asset_id "
                                 "WHERE l.id = ?", p["listing_id"])
            spec = json.loads(listing["spec_json"]) if listing else {}
            make_pin_image(ctx.path(listing["file_path"]), str(changes["headline"])[:60], spec.get("accent_color"),
                           ctx.path(p["image_path"]))
            p["headline"] = applied["headline"] = str(changes["headline"])[:60]
        if p.get("pin_id"):
            ctx.db.update("pins", p["pin_id"], title=p["title"])
        summary = f"Pin \"{p['title']}\" -> {p['link']}  |  image: {p['image_path']}"
    else:
        raise EditError("This item can't be edited")
    ctx.db.update("approvals", approval_id, payload_json=json.dumps(p), summary=summary)
    return applied


def _history(ctx, approval_id: int) -> str:
    rows = ctx.db.all("SELECT role, text FROM revisions WHERE approval_id = ? ORDER BY id DESC LIMIT 6", approval_id)
    return "\n".join(f"{'Owner' if r['role'] == 'owner' else 'You'}: {r['text']}" for r in reversed(rows)) or "(none)"


def _mock_listing(instruction: str, spec: dict) -> dict:
    text = instruction.lower()
    changes, did, redesign = {}, [], None
    m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)|(\d+\.\d{2})", instruction)
    if m and re.search(r"pric|\$|cheap|lower|raise|cost|\d\.\d\d", text):
        changes["price_usd"] = float(m.group(1) or m.group(2))
        did.append(f"set the price to ${changes['price_usd']:.2f}")
    color = next((c for c in sorted(COLORS, key=len, reverse=True) if c in text), None)
    if color:
        redesign = f"Change the accent color to {color}."
        did.append(f"switched the accent color to {color}")
    reply = ("Mock mode: I " + " and ".join(did) + ".") if did else \
        "Mock mode can only change prices and colors. Switch llm.provider to a real model for anything else."
    return {"reply": reply, "changes": changes, "redesign": redesign}


def revise(ctx, approval_id: int, instruction: str) -> dict:
    instruction = instruction.strip()
    if not instruction:
        raise EditError("Type what you'd like changed")
    row, p = _load(ctx, approval_id)
    ctx.db.insert("revisions", approval_id=approval_id, role="owner", text=instruction)
    history = _history(ctx, approval_id)
    redesigned = False

    if row["action"] == "etsy.publish_listing":
        asset = _asset_for(ctx, p)
        spec = json.loads(asset["spec_json"]) if asset else {}
        result = ctx.llm.json(
            LISTING_SYSTEM,
            LISTING_PROMPT.format(title=p["title"], price=p["price_usd"], tags=", ".join(p.get("tags", [])),
                                  description=body_of(p.get("description", "")), spec_title=spec.get("title", ""),
                                  accent=spec.get("accent_color", ""), history=history, instruction=instruction,
                                  pages="; ".join(pg.get("heading", "") for pg in spec.get("pages", []))),
            team="etsy", agent="editor", tier="smart", mock=lambda: _mock_listing(instruction, spec))
        applied = apply_changes(ctx, approval_id, result.get("changes") or {}, source="ai")
        if result.get("redesign") and asset:
            color = next((c for c in sorted(COLORS, key=len, reverse=True) if c in instruction.lower()), None)
            new_spec = ctx.llm.json(
                DESIGN_SYSTEM, REDESIGN_PROMPT.format(spec=json.dumps(spec), instruction=result["redesign"]),
                team="etsy", agent="designer", tier="fast", max_tokens=3000,
                mock=lambda: {**spec, "accent_color": COLORS.get(color, spec.get("accent_color"))})
            if new_spec.get("pages"):
                pdf = ctx.path(asset["file_path"])
                pages = render_pdf(new_spec, pdf)
                make_listing_image(pdf, new_spec, pages, ctx.path(asset["preview_path"]))
                ctx.db.update("assets", asset["id"], spec_json=json.dumps(new_spec), title=new_spec.get("title", asset["title"]))
                redesigned = True
    elif row["action"] == "pinterest.create_pin":
        result = ctx.llm.json(
            PIN_SYSTEM,
            PIN_PROMPT.format(title=p["title"], description=p["description"], alt_text=p.get("alt_text", ""),
                              headline=p.get("headline", p["title"]), history=history, instruction=instruction),
            team="pinterest", agent="pin_writer", tier="fast",
            mock=lambda: {"reply": "Mock mode can't rewrite pins. Switch llm.provider to a real model.", "changes": {}})
        applied = apply_changes(ctx, approval_id, result.get("changes") or {}, source="ai")
    else:
        raise EditError("This item can't be edited")

    reply = str(result.get("reply") or "Done.").strip()
    if redesigned:
        reply += " The printable and its listing image were redrawn."
    ctx.db.insert("revisions", approval_id=approval_id, role="ai", text=reply)
    return {"reply": reply, "applied": applied, "redesigned": redesigned}

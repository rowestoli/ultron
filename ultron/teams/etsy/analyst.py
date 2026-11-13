"""Analyst agent: pulls listing stats and sales, then decides scale / keep / kill and records lessons."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ...db import now
from . import mocks

SYSTEM = """You are the analyst on an Etsy printables team. You judge listings on real data only.
- Listings younger than 14 days, or with fewer than 50 views, get "keep" unless something is clearly broken.
- "scale" = it sells or converts favorites well; the team will make variations of that niche.
- "kill" = 30+ days, meaningful views, no sales; the team stops investing in that niche.
- Lessons must be general, actionable, and grounded in the numbers (e.g. which product types or price points
  convert). Max 3. Do not restate a lesson that is already in the playbook."""

PROMPT = """{lessons}

Listings (age in days, views, favorites, sales, revenue):
{table}

Return: {{"verdicts": [{{"listing_id": 1, "verdict": "scale|keep|kill", "reason": "..."}}], "lessons": ["..."]}}"""


class Analyst:
    def __init__(self, team):
        self.team = team

    # --- data collection ----------------------------------------------------------

    def refresh(self) -> int:
        """Snapshot stats for every live listing. Returns how many listings were updated."""
        db, api = self.team.db, self.team.api
        if not api.can_read:
            return 0
        live = db.all("SELECT * FROM listings WHERE platform = 'etsy' AND external_id IS NOT NULL "
                      "AND status IN ('draft', 'active')")
        if not live:
            return 0

        sales: dict[str, list] = {}
        if api.can_write:
            since = int(time.time()) - 86400 * int(self.team.ctx.cfg["ultron"]["lookback_days"]) * 3
            for receipt in api.receipts(min_created=since):
                for tx in receipt.get("transactions", []):
                    price = tx.get("price") or {}
                    amount = (price.get("amount") or 0) / (price.get("divisor") or 100) * (tx.get("quantity") or 1)
                    sales.setdefault(str(tx.get("listing_id")), []).append(
                        (f"etsy-{receipt.get('receipt_id')}-{tx.get('transaction_id')}", amount, tx.get("quantity") or 1))

        updated = 0
        for row in live:
            try:
                info = api.get_listing(row["external_id"])
            except Exception as err:
                print(f"  [analyst] could not read Etsy listing {row['external_id']}: {err}")
                continue
            state = info.get("state")
            if state == "active" and row["status"] != "active":
                db.update("listings", row["id"], status="active", url=info.get("url") or row["url"])
            tx = sales.get(row["external_id"], [])
            for ref, amount, _ in tx:
                if not db.one("SELECT 1 FROM revenue WHERE ref = ?", ref):
                    db.insert("revenue", team="etsy", source="etsy_sale", amount_usd=amount,
                              note=row["title"], ref=ref)
            db.insert("metrics", listing_id=row["id"], views=info.get("views") or 0,
                      favorites=info.get("num_favorers") or 0, sales=sum(q for _, _, q in tx),
                      revenue_usd=round(sum(a for _, a, _ in tx), 2), source="etsy_api")
            updated += 1
        return updated

    # --- judgment -----------------------------------------------------------------

    def review(self) -> dict | None:
        db = self.team.db
        last = db.get_kv("etsy.last_review", "1970-01-01")
        if not db.one("SELECT 1 FROM metrics WHERE ts > ?", last):
            return None   # nothing new since the last review; don't spend tokens

        rows, ids = [], []
        for listing in db.all("SELECT l.*, n.keyword, n.id AS niche_id FROM listings l "
                              "JOIN assets a ON a.id = l.asset_id JOIN niches n ON n.id = a.niche_id "
                              "WHERE l.platform = 'etsy' AND l.status IN ('active', 'draft')"):
            m = db.latest_metrics(listing["id"])
            if not m:
                continue
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(listing["ts"])).days
            rows.append(f"#{listing['id']} \"{listing['title'][:70]}\" niche='{listing['keyword']}' "
                        f"${listing['price_usd']:.2f} | age {age}d | views {m['views']} | fav {m['favorites']} | "
                        f"sales {m['sales']} | ${m['revenue_usd']:.2f}")
            ids.append(listing["id"])
        if not rows:
            return None

        result = self.team.ask("analyst", SYSTEM,
                               PROMPT.format(lessons=self.team.lessons_block(), table="\n".join(rows)),
                               tier="smart", mock=lambda: mocks.review(ids))
        applied = {"scale": 0, "keep": 0, "kill": 0}
        for v in result.get("verdicts", []):
            verdict = v.get("verdict")
            if verdict not in applied or v.get("listing_id") not in ids:
                continue
            applied[verdict] += 1
            if verdict in ("scale", "kill"):
                niche = db.one("SELECT a.niche_id FROM listings l JOIN assets a ON a.id = l.asset_id WHERE l.id = ?",
                               v["listing_id"])
                db.update("niches", niche["niche_id"], status="scale" if verdict == "scale" else "killed")
        for lesson in (result.get("lessons") or [])[:3]:
            db.add_lesson("etsy", str(lesson).strip())
        db.set_kv("etsy.last_review", now())
        return {"verdicts": applied, "lessons": result.get("lessons", [])}

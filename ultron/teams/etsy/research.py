"""Researcher agent: proposes specific niches, then checks them against live Etsy search data."""
from __future__ import annotations

import math

from . import mocks

BLOCKED_TERMS = ["disney", "marvel", "pokemon", "harry potter", "taylor swift", "star wars", "nintendo",
                 "barbie", "lego", "nfl", "nba", "mlb", "nhl", "ncaa", "sanrio", "hello kitty", "bluey",
                 "recipe", "pattern", "crochet", "knitting", "sewing"]

SYSTEM = """You are the market researcher on an Etsy digital-products team that sells printable PDFs.
Find specific niches a buyer would search for by name. Specific beats generic:
"dog medication tracker printable" beats "planner".
Rules:
- Only these product types: {types}.
- No trademarks, brand names, celebrities, sports leagues or licensed characters.
- No recipes, sewing/crochet/knitting patterns, or instructional guides (Etsy bans AI-made functional guides).
- No medical, legal or financial advice content; trackers and logs are fine.
- Prefer evergreen, practical needs over trends."""

PROMPT = """{lessons}

Niches already explored (do not repeat or rephrase these):
{existing}
{focus}
Propose {n} new niches. Return:
{{"niches": [{{"keyword": "the exact search phrase a buyer types", "product_type": "one of the allowed types",
"buyer": "who buys it", "rationale": "one sentence on why it sells",
"est_demand": 1-5, "est_competition": 1-5}}]}}"""


class Researcher:
    def __init__(self, team):
        self.team = team

    def run(self, n: int, focus: str | None = None) -> list[int]:
        db, cfg = self.team.db, self.team.cfg
        existing = [r["keyword"] for r in db.all("SELECT keyword FROM niches WHERE team = 'etsy'")]
        data = self.team.ask(
            "researcher",
            SYSTEM.format(types=", ".join(cfg["product_types"])),
            PROMPT.format(lessons=self.team.lessons_block(), existing="\n".join(existing[-60:]) or "(none)", n=n,
                          focus=f"\nThe owner asked for this batch: {focus}\n" if focus else ""),
            tier="smart",
            mock=lambda: mocks.niches(n, set(existing), seed=len(existing)),
        )
        ids = []
        for niche in (data.get("niches") or [])[:n]:
            keyword = str(niche.get("keyword", "")).strip().lower()
            if not keyword or keyword in existing or any(t in keyword for t in BLOCKED_TERMS):
                continue
            count, avg_fav, verified = self._market_check(keyword)
            if verified:
                score = round(math.log1p(avg_fav) / math.log10(count + 10) * 10, 2)
            else:
                score = float(niche.get("est_demand", 3)) * 2 - float(niche.get("est_competition", 3))
            ids.append(db.insert(
                "niches", team="etsy", keyword=keyword, product_type=niche.get("product_type", "printable"),
                rationale=f"{niche.get('buyer', '')}: {niche.get('rationale', '')}".strip(": "),
                competing_listings=count, avg_favorites=avg_fav, score=score, verified=int(verified),
            ))
            existing.append(keyword)
        return ids

    def _market_check(self, keyword: str) -> tuple[int | None, float | None, bool]:
        """Etsy search: how many listings compete, and how much do the top ones get favorited."""
        api = self.team.api
        if not api.can_read:
            return None, None, False
        try:
            data = api.search_active(keyword, limit=25)
        except Exception as err:
            print(f"  [researcher] Etsy search failed for '{keyword}': {err}")
            return None, None, False
        results = data.get("results") or []
        favs = [r.get("num_favorers") or 0 for r in results]
        return int(data.get("count") or 0), (sum(favs) / len(favs) if favs else 0.0), True

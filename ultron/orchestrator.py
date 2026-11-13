"""Ultron: refreshes metrics, splits the budget by ROI (with exploration), runs teams, writes a strategy memo."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .context import Context
from .guard import BudgetExceeded
from .teams import REGISTRY

MEMO_SYSTEM = """You are Ultron, the orchestrator of a small set of AI agent teams that try to earn real revenue
(Etsy printables, plus a Pinterest team that drives traffic to them). You are a careful capital allocator, not a hype machine.
Write a short memo for the human owner: what happened this cycle, what the numbers say, what to watch next.
Be concrete and honest about uncertainty and small sample sizes.
Optionally add at most 2 cross-team lessons, only if the data supports them."""

MEMO_PROMPT = """Team performance over the last {days} days:
{stats}

This cycle's budget allocation (USD of LLM spend): {alloc}
This cycle's results: {results}
Approvals waiting for the owner: {pending}

Return: {{"memo": "3-6 sentences", "lessons": ["optional, max 2"]}}"""


class Ultron:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.teams = {name: cls(ctx) for name, cls in REGISTRY.items()
                      if ctx.cfg["teams"].get(name, {}).get("enabled")}
        for team in self.teams.values():
            team.register()

    # --- accounting ---------------------------------------------------------------

    def stats(self, team: str) -> dict:
        db = self.ctx.db
        days = self.ctx.cfg["ultron"]["lookback_days"]
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        revenue = db.scalar("SELECT SUM(amount_usd) FROM revenue WHERE team = ? AND ts >= ?", team, since, default=0.0)
        llm = db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE team = ? AND ts >= ?", team, since, default=0.0)
        other = db.scalar("SELECT SUM(amount_usd) FROM expenses WHERE team = ? AND ts >= ?", team, since, default=0.0)
        cost = llm + other
        return {"revenue": round(revenue, 2), "llm_cost": round(llm, 4), "other_cost": round(other, 2),
                "profit": round(revenue - cost, 2) or 0.0, "revenue_per_dollar": round(revenue / cost, 2) if cost else 0.0}

    def allocate(self, pool: float) -> dict[str, float]:
        u = self.ctx.cfg["ultron"]
        alloc: dict[str, float] = {}
        if u.get("write_memo"):
            alloc["ultron"] = round(min(0.05, pool * 0.1), 4)
        rest = pool - alloc.get("ultron", 0.0)

        support = [n for n, t in self.teams.items() if t.role == "support"]
        product = [n for n, t in self.teams.items() if t.role == "product"]
        if support:
            share = rest * u["support_share"] / len(support)
            for name in support:
                alloc[name] = round(share, 4)
            rest -= share * len(support)
        if product:
            explore = rest * u["explore_share"] / len(product)
            weights = {n: max(self.stats(n)["revenue_per_dollar"], 0.0) for n in product}
            total = sum(weights.values())
            exploit = rest * (1 - u["explore_share"])
            for name in product:
                w = weights[name] / total if total else 1 / len(product)
                alloc[name] = round(explore + exploit * w, 4)
        return alloc

    # --- the loop -----------------------------------------------------------------

    def run_cycle(self, only: str | None = None) -> dict:
        ctx = self.ctx
        ctx.guard.assert_running()
        for name, team in self.teams.items():
            try:
                team.refresh_metrics()
            except Exception as err:
                print(f"  [{name}] metrics refresh failed: {err}")

        pool = min(ctx.cfg["budget"]["cycle_llm_usd"], ctx.guard.headroom())
        if pool <= 0.001:
            return {"allocation": {}, "results": {}, "memo": "Budget cap reached; no work this cycle."}
        alloc = self.allocate(pool)
        ctx.guard.start_cycle(alloc)

        results = {}
        for name, team in self.teams.items():
            if only and name != only:
                continue
            print(f"→ {name}: budget ${alloc.get(name, 0):.3f}")
            results[name] = team.run_cycle()

        memo = None
        if ctx.cfg["ultron"].get("write_memo") and not only:
            memo = self._memo(alloc, results)
        ctx.db.insert("cycles", allocation_json=json.dumps(alloc), results_json=json.dumps(results, default=str),
                      memo=memo)
        return {"allocation": alloc, "results": results, "memo": memo}

    def _memo(self, alloc: dict, results: dict) -> str | None:
        stats = "\n".join(f"- {n} ({t.role}): {json.dumps(self.stats(n))}" for n, t in self.teams.items())
        pending = len(self.ctx.approvals.pending())
        try:
            data = self.ctx.llm.json(
                MEMO_SYSTEM,
                MEMO_PROMPT.format(days=self.ctx.cfg["ultron"]["lookback_days"], stats=stats,
                                   alloc=json.dumps(alloc), results=json.dumps(results, default=str), pending=pending),
                team="ultron", agent="memo", tier="smart",
                mock=lambda: {"memo": f"Cycle complete. {pending} actions are waiting for your approval. "
                                      "No sales data yet, so budget stays mostly on exploration.", "lessons": []},
            )
        except BudgetExceeded as err:
            return f"(memo skipped: {err})"
        for lesson in (data.get("lessons") or [])[:2]:
            self.ctx.db.add_lesson("ultron", str(lesson).strip(), source="ultron")
        return data.get("memo")

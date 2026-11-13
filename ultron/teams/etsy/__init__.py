"""Etsy digital-products team: researcher -> designer -> lister (approval) -> analyst."""
from __future__ import annotations

from ...guard import BudgetExceeded
from ...integrations.etsy_api import EtsyAPI
from ..base import Team
from .analyst import Analyst
from .design import Designer
from .listing import Lister
from .research import Researcher

MAX_VERSIONS_PER_NICHE = 3


class EtsyTeam(Team):
    name = "etsy"
    role = "product"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.api = EtsyAPI(ctx)
        self.researcher = Researcher(self)
        self.designer = Designer(self)
        self.lister = Lister(self)
        self.analyst = Analyst(self)

    def register(self) -> None:
        self.ctx.approvals.register("etsy.publish_listing", self.lister.publish)

    def refresh_metrics(self) -> None:
        self.analyst.refresh()

    def _next_niches(self, k: int):
        """Scaling winners first (one variation each), then the best untried candidates."""
        scale = self.db.all(
            "SELECT n.* FROM niches n WHERE n.team = 'etsy' AND n.status = 'scale' "
            "AND (SELECT COUNT(*) FROM assets a WHERE a.niche_id = n.id) < ? ORDER BY n.score DESC", MAX_VERSIONS_PER_NICHE)
        fresh = self.db.all(
            "SELECT * FROM niches WHERE team = 'etsy' AND status = 'candidate' ORDER BY score DESC, id")
        return (list(scale) + list(fresh))[:k]

    def run_cycle(self) -> dict:
        summary = {"reviewed": None, "niches_found": 0, "designed": 0, "queued_listings": 0}
        try:
            summary["reviewed"] = self.analyst.review()
            backlog = self.db.scalar("SELECT COUNT(*) FROM niches WHERE team = 'etsy' AND status = 'candidate'")
            if backlog < self.cfg["products_per_cycle"] * 2:
                summary["niches_found"] = len(self.researcher.run(self.cfg["niches_per_cycle"]))
            for niche in self._next_niches(self.cfg["products_per_cycle"]):
                asset_id = self.designer.run(niche)
                summary["designed"] += 1
                self.lister.run(asset_id)
                summary["queued_listings"] += 1
        except BudgetExceeded as err:
            summary["stopped"] = str(err)
        return summary

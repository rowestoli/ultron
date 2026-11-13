"""Every team plugs into Ultron through this interface."""
from __future__ import annotations

from typing import Any

from ..context import Context


class Team:
    name = ""
    role = "product"   # product teams earn revenue; support teams (distribution) help them

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.team_cfg(self.name)
        self.db = ctx.db

    def register(self) -> None:
        """Register executors for the actions this team queues for approval."""

    def refresh_metrics(self) -> None:
        """Pull fresh performance data before Ultron allocates budget."""

    def run_cycle(self) -> dict:
        raise NotImplementedError

    def ask(self, agent: str, system: str, prompt: str, *, tier: str = "fast",
            mock: Any = None, max_tokens: int = 2000) -> Any:
        return self.ctx.llm.json(system, prompt, team=self.name, agent=agent, tier=tier,
                                 mock=mock, max_tokens=max_tokens)

    def lessons_block(self) -> str:
        lessons = self.db.lessons(self.name)
        if not lessons:
            return "Playbook: no lessons recorded yet."
        return "Playbook lessons learned so far (follow them):\n" + "\n".join(f"- {l}" for l in lessons)

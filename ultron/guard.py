"""Spending caps and the kill switch. Every LLM call goes through check() first."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .db import DB


class BudgetExceeded(RuntimeError):
    pass


class KillSwitch(RuntimeError):
    pass


class Guard:
    def __init__(self, db: DB, cfg: dict, root: Path):
        self.db = db
        self.budget = cfg["budget"]
        self.kill_file = root / cfg["safety"]["kill_switch_file"]
        self.team_caps: dict[str, float] = {}      # set by Ultron each cycle
        self.cycle_start_id = 0                    # llm_spend rows after this id belong to the cycle

    # --- kill switch ---------------------------------------------------------

    def stopped(self) -> bool:
        return self.kill_file.exists()

    def assert_running(self) -> None:
        if self.stopped():
            raise KillSwitch(f"Kill switch is on ({self.kill_file.name} exists). Run `python -m ultron resume`.")

    # --- spend ---------------------------------------------------------------

    def spent_today(self) -> float:
        day = datetime.now(timezone.utc).date().isoformat()
        return self.db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE ts >= ?", day, default=0.0)

    def spent_this_month(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return self.db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE ts >= ?", month + "-01", default=0.0)

    def spent_this_cycle(self, team: str) -> float:
        return self.db.scalar(
            "SELECT SUM(cost_usd) FROM llm_spend WHERE id > ? AND team = ?", self.cycle_start_id, team, default=0.0
        )

    def headroom(self) -> float:
        return max(0.0, min(self.budget["daily_llm_usd"] - self.spent_today(),
                            self.budget["monthly_llm_usd"] - self.spent_this_month()))

    def start_cycle(self, caps: dict[str, float]) -> None:
        self.team_caps = caps
        self.cycle_start_id = self.db.scalar("SELECT MAX(id) FROM llm_spend", default=0)

    def check(self, team: str) -> None:
        self.assert_running()
        if self.spent_today() >= self.budget["daily_llm_usd"]:
            raise BudgetExceeded(f"Daily LLM cap ${self.budget['daily_llm_usd']:.2f} reached")
        if self.spent_this_month() >= self.budget["monthly_llm_usd"]:
            raise BudgetExceeded(f"Monthly LLM cap ${self.budget['monthly_llm_usd']:.2f} reached")
        cap = self.team_caps.get(team)
        if cap is not None and self.spent_this_cycle(team) >= cap:
            raise BudgetExceeded(f"{team} used its ${cap:.3f} allocation for this cycle")

"""Everything an agent needs, bundled once and passed around."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .approvals import Approvals
from .db import DB
from .guard import Guard
from .llm import LLM


@dataclass
class Context:
    root: Path
    cfg: dict
    db: DB
    guard: Guard
    llm: LLM
    approvals: Approvals

    @property
    def out(self) -> Path:
        return self.root / "out"

    def path(self, stored: str | Path) -> Path:
        """Find a generated file from the path saved in the database.

        Paths are saved absolute, so moving the project folder (or syncing it to the cloud)
        leaves them pointing at the old location. Anything under an `out/` folder is looked
        up again under this project's `out/`.
        """
        p = Path(stored)
        if p.exists():
            return p
        parts = p.parts
        if "out" in parts:
            i = len(parts) - 1 - parts[::-1].index("out")
            moved = self.out.joinpath(*parts[i + 1:])
            if moved.exists():
                return moved
        return p

    def team_cfg(self, name: str) -> dict:
        return self.cfg["teams"][name]

    @staticmethod
    def env(name: str) -> str | None:
        value = os.getenv(name)
        return value.strip() if value and value.strip() else None


def load(root: Path | None = None) -> Context:
    root = (root or Path.cwd()).resolve()
    load_dotenv(root / ".env")
    cfg = yaml.safe_load((root / "config.yaml").read_text())
    db = DB(root / "data" / "ultron.db")
    guard = Guard(db, cfg, root)
    llm = LLM(cfg["llm"], db, guard)
    approvals = Approvals(db, cfg["safety"]["require_approval"])
    (root / "out").mkdir(exist_ok=True)
    return Context(root, cfg, db, guard, llm, approvals)

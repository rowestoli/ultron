"""Human-in-the-loop gate. Agents queue actions; nothing leaves your machine until you approve it."""
from __future__ import annotations

import json
import traceback
from typing import Callable

from .db import DB, now

Executor = Callable[[dict], dict]


class Approvals:
    def __init__(self, db: DB, require_approval: bool):
        self.db = db
        self.require_approval = require_approval
        self.executors: dict[str, Executor] = {}

    def register(self, action: str, fn: Executor) -> None:
        self.executors[action] = fn

    def queue(self, team: str, action: str, summary: str, payload: dict) -> int:
        status = "pending" if self.require_approval else "approved"
        return self.db.insert("approvals", team=team, action=action, summary=summary,
                              payload_json=json.dumps(payload), status=status,
                              decided_ts=None if self.require_approval else now())

    def pending(self):
        return self.db.all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")

    def decide(self, approval_id: int, approve: bool, note: str = "") -> None:
        row = self.db.one("SELECT * FROM approvals WHERE id = ?", approval_id)
        if row is None or row["status"] != "pending":
            raise ValueError(f"Approval #{approval_id} is not pending")
        self.db.update("approvals", approval_id, status="approved" if approve else "rejected",
                       decided_ts=now(), result_json=json.dumps({"note": note}) if note else None)
        if not approve:
            target = json.loads(row["payload_json"]).get("_on_reject")
            if target:
                table, row_id = target
                self.db.update(table, row_id, status="rejected")

    def retry(self, approval_id: int) -> None:
        """Put a failed action back in line to run on the next publish."""
        row = self.db.one("SELECT status FROM approvals WHERE id = ?", approval_id)
        if row is None or row["status"] != "failed":
            raise ValueError(f"#{approval_id} is not a failed action")
        self.db.update("approvals", approval_id, status="approved")

    def execute_approved(self) -> list[tuple[int, str, str]]:
        """Run every approved action. Returns (id, action, outcome) tuples."""
        results = []
        for row in self.db.all("SELECT * FROM approvals WHERE status = 'approved' ORDER BY id"):
            fn = self.executors.get(row["action"])
            if fn is None:
                results.append((row["id"], row["action"], "skipped: no executor registered"))
                continue
            try:
                outcome = fn(json.loads(row["payload_json"]))
                self.db.update("approvals", row["id"], status="executed", result_json=json.dumps(outcome))
                results.append((row["id"], row["action"], outcome.get("message", "done")))
            except Exception as err:  # keep going; record the failure for review
                self.db.update("approvals", row["id"], status="failed",
                               result_json=json.dumps({"error": str(err), "trace": traceback.format_exc()[-2000:]}))
                results.append((row["id"], row["action"], f"FAILED: {err}"))
        return results

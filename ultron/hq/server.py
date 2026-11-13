"""Ultron HQ: a local, game-style control room for the agents.

Runs only on this computer (binds to 127.0.0.1). Reads the same SQLite memory the agents write to.
Actions (approve, reject, publish, run cycle, kill switch) need a per-session token that only the
page served by this process knows, so other websites can't trigger them.
"""
from __future__ import annotations

import io
import json
import mimetypes
import re
import secrets
import shutil
import threading
import traceback
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import context
from ..db import now
from ..revise import EditError, apply_changes, body_of, revise

HERE = Path(__file__).parent
AGENT_TEAM = {"researcher": "etsy", "designer": "etsy", "lister": "etsy", "analyst": "etsy",
              "pin_writer": "pinterest", "memo": "ultron", "chat": "ultron"}


class Jobs:
    """One background job at a time (cycle or publish), so the UI never blocks."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.state = {"running": False, "kind": None, "started": None, "finished": None,
                      "output": "", "error": None}

    def start(self, kind: str, arg: str | None = None) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "kind": kind, "started": now(), "finished": None,
                          "output": "", "error": None}
        threading.Thread(target=self._run, args=(kind, arg), daemon=True).start()
        return True

    def _run(self, kind: str, arg: str | None = None):
        buf = io.StringIO()
        error = None
        try:
            with redirect_stdout(buf):
                from ..orchestrator import Ultron
                ctx = context.load(self.root)
                if kind == "chat":
                    from ..command import handle
                    handle(ctx, arg or "")
                elif kind == "cycle":
                    ultron = Ultron(ctx)
                    out = ultron.run_cycle()
                    print("Allocation:", json.dumps(out["allocation"]))
                    for team, res in out["results"].items():
                        print(f"{team}: {json.dumps(res, default=str)}")
                    if out.get("memo"):
                        print("Memo:", out["memo"])
                else:
                    Ultron(ctx)   # registers the publish executors
                    ctx.guard.assert_running()
                    results = ctx.approvals.execute_approved()
                    if not results:
                        print("No approved actions to run.")
                    for i, action, outcome in results:
                        print(f"#{i} {action}: {outcome}")
        except Exception as err:
            error = f"{type(err).__name__}: {err}"
            buf.write("\n" + traceback.format_exc()[-1500:])
        with self.lock:
            self.state.update(running=False, finished=now(), output=buf.getvalue()[-6000:], error=error)


def _rel(ctx, path: str | None) -> str | None:
    """Turn an absolute asset path into a URL served by /file, only if it lives under out/."""
    if not path:
        return None
    try:
        rel = ctx.path(path).resolve().relative_to(ctx.out.resolve())
    except (ValueError, OSError):
        return None
    return "/file?p=" + str(rel).replace("\\", "/")


def build_state(root: Path, jobs: Jobs) -> dict:
    ctx = context.load(root)
    db, cfg = ctx.db, ctx.cfg
    days = cfg["ultron"]["lookback_days"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def team_stats(team):
        rev = db.scalar("SELECT SUM(amount_usd) FROM revenue WHERE team = ? AND ts >= ?", team, since, default=0.0)
        llm = db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE team = ? AND ts >= ?", team, since, default=0.0)
        exp = db.scalar("SELECT SUM(amount_usd) FROM expenses WHERE team = ? AND ts >= ?", team, since, default=0.0)
        return {"revenue": round(rev, 2), "llm_cost": round(llm, 4), "other_cost": round(exp, 2),
                "profit": round(rev - llm - exp, 2) or 0.0}

    agents = {}
    for name, team in AGENT_TEAM.items():
        row = db.one("SELECT COUNT(*) AS calls, SUM(input_tokens) AS tin, SUM(output_tokens) AS tout, "
                     "SUM(cost_usd) AS cost, MAX(ts) AS last FROM llm_spend WHERE agent = ?", name)
        cost30 = db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE agent = ? AND ts >= ?", name, since, default=0.0)
        agents[name] = {"team": team, "calls": row["calls"] or 0, "tokens_in": row["tin"] or 0,
                        "tokens_out": row["tout"] or 0, "cost": round(row["cost"] or 0.0, 4),
                        "cost_30d": round(cost30, 4), "last_ts": row["last"]}

    niches = [dict(r) for r in db.all("SELECT id, ts, keyword, product_type, rationale, competing_listings, "
                                      "avg_favorites, score, verified, status FROM niches ORDER BY id DESC LIMIT 60")]
    assets = []
    for r in db.all("SELECT a.*, n.keyword FROM assets a LEFT JOIN niches n ON n.id = a.niche_id "
                    "ORDER BY a.id DESC LIMIT 40"):
        spec = json.loads(r["spec_json"] or "{}")
        assets.append({"id": r["id"], "ts": r["ts"], "title": r["title"], "kind": r["kind"], "keyword": r["keyword"],
                       "pages": len(spec.get("pages", [])), "accent": spec.get("accent_color"),
                       "preview_url": _rel(ctx, r["preview_path"]), "pdf_url": _rel(ctx, r["file_path"])})
    listings = []
    for r in db.all("SELECT l.*, a.preview_path FROM listings l LEFT JOIN assets a ON a.id = l.asset_id "
                    "ORDER BY l.id DESC LIMIT 60"):
        m = db.latest_metrics(r["id"])
        listings.append({"id": r["id"], "ts": r["ts"], "title": r["title"], "status": r["status"],
                         "price": r["price_usd"], "url": r["url"], "platform": r["platform"],
                         "preview_url": _rel(ctx, r["preview_path"]),
                         "views": m["views"] if m else None, "favorites": m["favorites"] if m else None,
                         "sales": m["sales"] if m else None, "revenue": m["revenue_usd"] if m else None})
    pins = [{"id": r["id"], "ts": r["ts"], "title": r["title"], "status": r["status"],
             "image_url": _rel(ctx, r["image_path"]), "listing_id": r["listing_id"]}
            for r in db.all("SELECT * FROM pins ORDER BY id DESC LIMIT 40")]

    def approval(r):
        p = json.loads(r["payload_json"] or "{}")
        res = json.loads(r["result_json"]) if r["result_json"] else {}
        chat = [{"role": c["role"], "text": c["text"], "ts": c["ts"]}
                for c in db.all("SELECT * FROM revisions WHERE approval_id = ? ORDER BY id", r["id"])]
        return {"id": r["id"], "ts": r["ts"], "team": r["team"], "action": r["action"], "summary": r["summary"],
                "status": r["status"], "decided_ts": r["decided_ts"],
                "title": p.get("title"), "price": p.get("price_usd"), "tags": p.get("tags"),
                "description": p.get("description"), "link": p.get("link"),
                "description_body": body_of(p.get("description", "")) if r["action"].startswith("etsy") else p.get("description"),
                "alt_text": p.get("alt_text"), "headline": p.get("headline"), "chat": chat,
                "image_url": _rel(ctx, p.get("preview_path") or p.get("image_path")),
                "pdf_url": _rel(ctx, p.get("file_path")),
                "result": res.get("message") or res.get("error") or res.get("note")}

    pending = [approval(r) for r in db.all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")]
    approved = [approval(r) for r in db.all("SELECT * FROM approvals WHERE status = 'approved' ORDER BY id")]
    recent = [approval(r) for r in db.all("SELECT * FROM approvals WHERE status NOT IN ('pending', 'approved') "
                                          "ORDER BY decided_ts DESC, id DESC LIMIT 25")]
    lessons = [dict(r) for r in db.all("SELECT ts, team, lesson, source FROM playbook ORDER BY id DESC")]
    cycles = [{"id": r["id"], "ts": r["ts"], "allocation": json.loads(r["allocation_json"] or "{}"),
               "results": json.loads(r["results_json"] or "{}"), "memo": r["memo"]}
              for r in db.all("SELECT * FROM cycles ORDER BY id DESC LIMIT 15")]
    revenue = [dict(r) for r in db.all("SELECT ts, team, source, amount_usd, note FROM revenue ORDER BY id DESC LIMIT 50")]
    expenses = [dict(r) for r in db.all("SELECT ts, team, kind, amount_usd, note FROM expenses ORDER BY id DESC LIMIT 50")]

    events = []
    for n in niches[:15]:
        events.append({"ts": n["ts"], "agent": "researcher", "text": f"Found niche: {n['keyword']}"})
    for a in assets[:15]:
        events.append({"ts": a["ts"], "agent": "designer", "text": f"Built printable: {a['title']}"})
    for r in db.all("SELECT * FROM approvals ORDER BY id DESC LIMIT 25"):
        who = "pin_writer" if r["action"].startswith("pinterest") else "lister"
        p = json.loads(r["payload_json"] or "{}")
        events.append({"ts": r["ts"], "agent": who, "text": f"Sent to the gate: {p.get('title', r['action'])}"})
        if r["decided_ts"] and r["status"] != "pending":
            verb = {"rejected": "Rejected", "executed": "Shipped", "failed": "Failed to ship"}.get(r["status"], "Approved")
            events.append({"ts": r["decided_ts"], "agent": "you", "text": f"{verb} #{r['id']}: {p.get('title', '')}"})
    for r in revenue[:15]:
        events.append({"ts": r["ts"], "agent": "vault", "text": f"Sale +${r['amount_usd']:.2f}: {r['note'] or ''}"})
    for c in cycles[:10]:
        events.append({"ts": c["ts"], "agent": "ultron", "text": f"Cycle #{c['id']} complete"})
    for l in lessons[:10]:
        events.append({"ts": l["ts"], "agent": "playbook", "text": f"New lesson: {l['lesson']}"})
    events.sort(key=lambda e: e["ts"] or "", reverse=True)

    chat = [dict(r) for r in reversed(db.all("SELECT id, ts, role, text FROM chat ORDER BY id DESC LIMIT 40"))]

    b = cfg["budget"]
    rev30 = db.scalar("SELECT SUM(amount_usd) FROM revenue WHERE ts >= ?", since, default=0.0)
    cost30 = (db.scalar("SELECT SUM(cost_usd) FROM llm_spend WHERE ts >= ?", since, default=0.0)
              + db.scalar("SELECT SUM(amount_usd) FROM expenses WHERE ts >= ?", since, default=0.0))
    with jobs.lock:
        job = dict(jobs.state)

    return {
        "now": now(), "provider": ctx.llm.provider, "simulated": ctx.llm.simulated, "killed": ctx.guard.stopped(),
        "require_approval": cfg["safety"]["require_approval"], "lookback_days": days,
        "budget": {"daily_cap": b["daily_llm_usd"], "monthly_cap": b["monthly_llm_usd"], "cycle_cap": b["cycle_llm_usd"],
                   "spent_today": round(ctx.guard.spent_today(), 4), "spent_month": round(ctx.guard.spent_this_month(), 4)},
        "totals": {"revenue_30d": round(rev30, 2), "cost_30d": round(cost30, 4), "profit_30d": round(rev30 - cost30, 2) or 0.0,
                   "revenue_all": round(db.scalar("SELECT SUM(amount_usd) FROM revenue", default=0.0), 2)},
        "teams": {name: {**team_stats(name), "enabled": t.get("enabled", False), "role": t.get("role")}
                  for name, t in cfg["teams"].items()},
        "agents": agents, "niches": niches, "assets": assets, "listings": listings, "pins": pins,
        "pending": pending, "approved": approved, "recent": recent, "lessons": lessons, "cycles": cycles,
        "revenue": revenue, "expenses": expenses, "events": events[:60], "job": job, "chat": chat,
    }


DEMO_TOKEN = "__DEMO__"


def export_demo(root: Path, dest: Path, include_pdfs: bool = False) -> dict:
    """Write a read-only copy of HQ to `dest` for hosting anywhere as static files.

    The page reads a frozen state.json instead of the live API, every button explains what it
    would do instead of doing it, and only the images the page shows are copied. The PDFs are
    the products for sale, so they stay out unless `include_pdfs` is set.
    Nothing from .env, the Etsy token or the job log goes in. Draft listing URLs are dropped
    because Etsy drafts aren't public, and so are local file paths.
    """
    ctx = context.load(root)
    state = build_state(root, Jobs(root))
    state["job"] = {"running": False, "kind": None, "started": None, "finished": None, "output": "", "error": None}
    for listing in state["listings"]:
        if listing.get("status") != "active":
            listing["url"] = None
    # Approval summaries end with a local file path, which would show the computer's user name.
    for group in ("pending", "approved", "recent"):
        for a in state[group]:
            a["summary"] = re.sub(r"\s+\|\s+(preview|image): .*$", "", a.get("summary") or "")
    state["demo"] = {"exported": now()}

    dest.mkdir(parents=True, exist_ok=True)
    files = dest / "files"
    copied: set[str] = set()

    def relink(value):
        if isinstance(value, str) and value.startswith("/file?p="):
            rel = value[len("/file?p="):]
            if not include_pdfs and rel.lower().endswith(".pdf"):
                return None
            src = (ctx.out / rel).resolve()
            if ctx.out.resolve() in src.parents and src.is_file():
                (files / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, files / rel)
                copied.add(rel)
            return "files/" + rel
        if isinstance(value, dict):
            return {k: relink(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relink(v) for v in value]
        return value

    state = relink(state)
    (dest / "state.json").write_text(json.dumps(state, default=str), encoding="utf-8")
    page = (HERE / "index.html").read_text(encoding="utf-8").replace("__HQ_TOKEN__", DEMO_TOKEN)
    (dest / "index.html").write_text(page, encoding="utf-8")
    return {"dest": str(dest), "files": len(copied), "listings": len(state["listings"])}


def make_handler(root: Path, token: str, jobs: Jobs):
    page = (HERE / "index.html").read_text(encoding="utf-8").replace("__HQ_TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                try:
                    self._json(build_state(root, jobs))
                except Exception as err:
                    self._json({"error": f"{type(err).__name__}: {err}"}, 500)
            elif url.path == "/file":
                rel = parse_qs(url.query).get("p", [""])[0]
                out = (root / "out").resolve()
                target = (out / rel).resolve()
                if not rel or out not in target.parents or not target.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.headers.get("X-HQ-Token") != token:
                self._json({"error": "forbidden"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            path = urlparse(self.path).path
            try:
                ctx = context.load(root)
                if path in ("/api/approve", "/api/reject"):
                    approve = path.endswith("approve")
                    note = str(body.get("note") or "").strip()
                    ctx.approvals.decide(int(body["id"]), approve, note)
                    if not approve and note:
                        team = ctx.db.one("SELECT team FROM approvals WHERE id = ?", int(body["id"]))["team"]
                        ctx.db.add_lesson(team, f"Owner rejected: {note}", source="owner")
                    self._json({"ok": True})
                elif path == "/api/retry":
                    ctx.approvals.retry(int(body["id"]))
                    self._json({"ok": True})
                elif path == "/api/edit":
                    fields = {k: body.get(k) for k in ("title", "price_usd", "tags", "description", "alt_text", "headline")
                              if body.get(k) is not None}
                    self._json({"ok": True, "applied": apply_changes(ctx, int(body["id"]), fields)})
                elif path == "/api/revise":
                    self._json({"ok": True, **revise(ctx, int(body["id"]), str(body.get("instruction") or ""))})
                elif path in ("/api/cycle", "/api/publish"):
                    if ctx.guard.stopped():
                        self._json({"error": "Kill switch is on"}, 409)
                    elif jobs.start("cycle" if path.endswith("cycle") else "publish"):
                        self._json({"ok": True})
                    else:
                        self._json({"error": "A job is already running"}, 409)
                elif path == "/api/chat":
                    message = str(body.get("message") or "").strip()[:2000]
                    if not message:
                        self._json({"error": "Type a message first"}, 400)
                    elif ctx.guard.stopped():
                        self._json({"error": "Kill switch is on"}, 409)
                    elif jobs.start("chat", message):
                        self._json({"ok": True})
                    else:
                        self._json({"error": "Ultron is busy with another job. Try again when it finishes."}, 409)
                elif path == "/api/kill":
                    if body.get("on"):
                        ctx.guard.kill_file.write_text("Stopped from Ultron HQ.\n")
                    else:
                        ctx.guard.kill_file.unlink(missing_ok=True)
                    self._json({"ok": True, "killed": ctx.guard.stopped()})
                else:
                    self._json({"error": "unknown action"}, 404)
            except Exception as err:
                self._json({"error": f"{type(err).__name__}: {err}"}, 400)

    return Handler


def serve(root: Path, port: int = 8787, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    jobs = Jobs(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, token, jobs))
    url = f"http://127.0.0.1:{port}/"
    print(f"Ultron HQ running at {url}  (local only, Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHQ closed.")
    finally:
        server.server_close()

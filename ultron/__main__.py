"""Command line for Ultron. Run from the project folder:  python -m ultron <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import context
from .guard import BudgetExceeded, KillSwitch


def cmd_cycle(ctx, args):
    from .orchestrator import Ultron
    out = Ultron(ctx).run_cycle(only=args.team)
    print("\nAllocation:", json.dumps(out["allocation"]))
    for team, res in out["results"].items():
        print(f"{team}: {json.dumps(res, default=str)}")
    if out.get("memo"):
        print(f"\nUltron memo: {out['memo']}")
    pending = len(ctx.approvals.pending())
    if pending:
        print(f"\n{pending} action(s) waiting. Run: python -m ultron review")


def _show(row):
    payload = json.loads(row["payload_json"])
    print(f"\n#{row['id']}  [{row['team']}] {row['action']}")
    print(f"  {row['summary']}")
    for key in ("title", "price_usd", "tags", "link", "description"):
        if key in payload:
            value = payload[key]
            if key == "description":
                value = value[:400] + ("…" if len(value) > 400 else "")
            print(f"  {key}: {value}")


def cmd_review(ctx, args):
    from .orchestrator import Ultron
    Ultron(ctx)  # registers executors
    rows = ctx.approvals.pending()
    if not rows:
        print("Nothing waiting for approval.")
        return
    for row in rows:
        _show(row)
        choice = input("  [a]pprove  [r]eject  [s]kip  [q]uit > ").strip().lower()
        if choice == "q":
            break
        if choice in ("a", "r"):
            note = input("  note (optional, helps the agents learn) > ").strip() if choice == "r" else ""
            ctx.approvals.decide(row["id"], choice == "a", note)
            if choice == "r" and note:
                ctx.db.add_lesson(row["team"], f"Owner rejected: {note}", source="owner")
    if input("\nPublish approved actions now? [y/N] > ").strip().lower() == "y":
        cmd_publish(ctx, args)


def cmd_decide(ctx, args, approve: bool):
    ids = [r["id"] for r in ctx.approvals.pending()] if args.all else args.ids
    note = getattr(args, "note", "") or ""
    for i in ids:
        ctx.approvals.decide(int(i), approve, note)
        if not approve and note:
            team = ctx.db.one("SELECT team FROM approvals WHERE id = ?", int(i))["team"]
            ctx.db.add_lesson(team, f"Owner rejected: {note}", source="owner")
        print(f"#{i} {'approved' if approve else 'rejected'}")


def cmd_publish(ctx, args):
    from .orchestrator import Ultron
    ctx.guard.assert_running()
    Ultron(ctx)
    results = ctx.approvals.execute_approved()
    if not results:
        print("No approved actions to run.")
    for i, action, outcome in results:
        print(f"#{i} {action}: {outcome}")


def cmd_retry(ctx, args):
    for i in args.ids:
        ctx.approvals.retry(int(i))
        print(f"#{i} queued to retry. Run: python -m ultron publish")


def cmd_status(ctx, args):
    from .orchestrator import Ultron
    u = Ultron(ctx)
    b = ctx.cfg["budget"]
    sim = "  (simulated, mock mode)" if ctx.llm.simulated else ""
    print(f"LLM provider: {ctx.llm.provider}{'  |  KILL SWITCH ON' if ctx.guard.stopped() else ''}")
    print(f"LLM spend today ${ctx.guard.spent_today():.4f} / ${b['daily_llm_usd']:.2f}   "
          f"month ${ctx.guard.spent_this_month():.4f} / ${b['monthly_llm_usd']:.2f}{sim}")
    print(f"\nLast {ctx.cfg['ultron']['lookback_days']} days by team:")
    for name, team in u.teams.items():
        print(f"  {name:10} ({team.role}) {json.dumps(u.stats(name))}")
    rows = ctx.db.all("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")
    print("\nListings:", ", ".join(f"{r['status']} {r['n']}" for r in rows) or "none")
    rows = ctx.db.all("SELECT status, COUNT(*) AS n FROM niches GROUP BY status")
    print("Niches:  ", ", ".join(f"{r['status']} {r['n']}" for r in rows) or "none")
    print(f"Pending approvals: {len(ctx.approvals.pending())}")
    last = ctx.db.one("SELECT ts, memo FROM cycles WHERE memo IS NOT NULL ORDER BY id DESC LIMIT 1")
    if last:
        print(f"\nLast memo ({last['ts']}): {last['memo']}")


def cmd_listings(ctx, args):
    rows = ctx.db.all("SELECT * FROM listings ORDER BY id DESC LIMIT 50")
    for r in rows:
        m = ctx.db.latest_metrics(r["id"])
        stats = f"views {m['views']} fav {m['favorites']} sales {m['sales']} ${m['revenue_usd']:.2f}" if m else "no data"
        print(f"#{r['id']:<4} {r['status']:<15} ${r['price_usd'] or 0:<6.2f} {r['title'][:60]:<60}  {stats}")


def cmd_metrics(ctx, args):
    """Manual stats entry when the Etsy API isn't connected. Values are totals to date."""
    listing = ctx.db.one("SELECT * FROM listings WHERE id = ?", args.listing_id)
    if not listing:
        sys.exit(f"No listing #{args.listing_id}")
    prev = ctx.db.latest_metrics(args.listing_id)
    prev_rev = prev["revenue_usd"] if prev else 0.0
    ctx.db.insert("metrics", listing_id=args.listing_id, views=args.views, favorites=args.favorites,
                  sales=args.sales, revenue_usd=args.revenue, source="manual")
    if args.revenue > prev_rev:
        ctx.db.insert("revenue", team=listing["team"], source="manual", amount_usd=round(args.revenue - prev_rev, 2),
                      note=listing["title"], ref=f"manual-{args.listing_id}-{args.revenue}")
    print(f"Recorded metrics for #{args.listing_id}.")


def cmd_link(ctx, args):
    listing = ctx.db.one("SELECT * FROM listings WHERE id = ?", args.listing_id)
    if not listing:
        sys.exit(f"No listing #{args.listing_id}")
    external = args.external_id or "".join(ch for ch in args.url.split("/listing/")[-1].split("/")[0] if ch.isdigit())
    ctx.db.update("listings", args.listing_id, url=args.url, external_id=external or None, status="active")
    fee = ctx.cfg["teams"].get(listing["team"], {}).get("listing_fee_usd")
    if fee:
        ctx.db.insert("expenses", team=listing["team"], kind="listing_fee", amount_usd=fee, note=f"listing {external}")
    print(f"Listing #{args.listing_id} marked active -> {args.url}")


def cmd_lessons(ctx, args):
    for r in ctx.db.all("SELECT * FROM playbook ORDER BY id"):
        print(f"[{r['team']}/{r['source']}] {r['lesson']}")


def cmd_stop(ctx, args):
    ctx.guard.kill_file.write_text("Ultron is stopped. Delete this file or run `python -m ultron resume`.\n")
    print("Kill switch ON. Nothing will run or publish.")


def cmd_resume(ctx, args):
    ctx.guard.kill_file.unlink(missing_ok=True)
    print("Kill switch OFF.")


def cmd_etsy_auth(ctx, args):
    from .integrations.etsy_api import EtsyAPI
    api = EtsyAPI(ctx)
    if not api.key:
        sys.exit("Set ETSY_API_KEY (keystring:shared_secret) in .env first.")
    url, verifier, state = api.auth_url()
    print("1. Open this URL, sign in, and allow access:\n\n" + url)
    print(f"\n2. Etsy redirects to {api.redirect_uri}?code=... The page won't load; that's fine.")
    redirected = input("3. Paste the full URL from the address bar here > ").strip()
    params = parse_qs(urlparse(redirected).query)
    if params.get("state", [""])[0] != state:
        sys.exit("State mismatch; start again.")
    token = api.exchange_code(params["code"][0], verifier)
    print(f"Connected. Shop ID {token.get('shop_id') or '(none found: set ETSY_SHOP_ID in .env)'}. "
          f"Token saved to {api.token_path}")


def cmd_etsy_taxonomy(ctx, args):
    from .integrations.etsy_api import EtsyAPI
    api = EtsyAPI(ctx)
    if not api.can_read:
        sys.exit("Set ETSY_API_KEY in .env first.")
    query = args.query.lower()
    for node in api.taxonomy_nodes():
        if query in node["path"].lower():
            print(f"{node['id']:>6}  {node['path']}")
    print("\nPut the id that fits best into config.yaml -> teams.etsy.taxonomy_id")


def cmd_edit(ctx, args):
    from .revise import apply_changes
    fields = {"title": args.title, "price_usd": args.price, "tags": args.tags, "description": args.description}
    applied = apply_changes(ctx, args.id, {k: v for k, v in fields.items() if v is not None})
    print(f"#{args.id} updated: {', '.join(applied) or 'nothing changed'}")


def cmd_revise(ctx, args):
    from .revise import revise
    out = revise(ctx, args.id, " ".join(args.instruction))
    print(out["reply"])


def cmd_chat(ctx, args):
    from .command import handle
    if args.message:
        handle(ctx, " ".join(args.message))
        return
    print("Talk to Ultron. Examples: 'draft 5 versions of a dog walking log', 'just research niches for teachers'.")
    print("Empty line or Ctrl+C to quit.")
    while True:
        try:
            line = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        handle(ctx, line)


def cmd_hq(ctx, args):
    from .hq.server import serve
    serve(ctx.root, port=args.port, open_browser=not args.no_browser)


def cmd_hq_demo(ctx, args):
    from .hq.server import export_demo
    out = export_demo(ctx.root, Path(args.dest).resolve(), include_pdfs=args.include_pdfs)
    print(f"Read-only HQ written to {out['dest']} ({out['listings']} listings, {out['files']} files).")
    print("Upload that folder to any static host. It has no buttons that do anything.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m ultron", description="Ultron agent orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("cycle", help="run one cycle: metrics -> allocate -> teams -> memo")
    s.add_argument("--team", help="run only this team")
    sub.add_parser("review", help="approve or reject queued actions interactively")
    s = sub.add_parser("approve", help="approve by id (or --all)")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("reject", help="reject by id (or --all)")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true")
    s.add_argument("--note", default="")
    sub.add_parser("publish", help="execute approved actions")
    s = sub.add_parser("retry", help="re-queue failed actions by id, then run publish")
    s.add_argument("ids", nargs="+", type=int)
    sub.add_parser("status", help="spend, ROI, listings, last memo")
    sub.add_parser("listings", help="list listings with latest stats")
    s = sub.add_parser("metrics", help="enter stats by hand (totals to date)")
    s.add_argument("listing_id", type=int)
    s.add_argument("--views", type=int, default=0)
    s.add_argument("--favorites", type=int, default=0)
    s.add_argument("--sales", type=int, default=0)
    s.add_argument("--revenue", type=float, default=0.0)
    s = sub.add_parser("link", help="mark a manually uploaded listing as live")
    s.add_argument("listing_id", type=int)
    s.add_argument("--url", required=True)
    s.add_argument("--external-id")
    sub.add_parser("lessons", help="show the playbook")
    sub.add_parser("stop", help="kill switch on")
    sub.add_parser("resume", help="kill switch off")
    sub.add_parser("etsy-auth", help="connect your Etsy shop (OAuth)")
    s = sub.add_parser("etsy-taxonomy", help="find the taxonomy id for your listings")
    s.add_argument("query")
    s = sub.add_parser("edit", help="edit a queued item before approving it")
    s.add_argument("id", type=int)
    s.add_argument("--title")
    s.add_argument("--price", type=float)
    s.add_argument("--tags", help="comma-separated")
    s.add_argument("--description")
    s = sub.add_parser("revise", help='ask the AI to change a queued item, e.g. revise 3 "make it $2.99"')
    s.add_argument("id", type=int)
    s.add_argument("instruction", nargs="+")
    s = sub.add_parser("chat", help='talk to Ultron, e.g. chat "draft 5 versions of a dog walking log"')
    s.add_argument("message", nargs="*")
    s = sub.add_parser("hq", help="open Ultron HQ, the local game-style control room")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("hq-demo", help="export a read-only copy of HQ for hosting (no secrets, no live buttons)")
    s.add_argument("dest", nargs="?", default="out/hq-demo")
    s.add_argument("--include-pdfs", action="store_true", help="also copy the printables themselves (they're the products)")
    args = p.parse_args(argv)

    ctx = context.load()
    handlers = {
        "cycle": cmd_cycle, "review": cmd_review, "publish": cmd_publish, "status": cmd_status,
        "listings": cmd_listings, "metrics": cmd_metrics, "link": cmd_link, "lessons": cmd_lessons,
        "stop": cmd_stop, "resume": cmd_resume, "retry": cmd_retry, "chat": cmd_chat, "hq": cmd_hq, "hq-demo": cmd_hq_demo, "edit": cmd_edit, "revise": cmd_revise, "etsy-auth": cmd_etsy_auth, "etsy-taxonomy": cmd_etsy_taxonomy,
        "approve": lambda c, a: cmd_decide(c, a, True), "reject": lambda c, a: cmd_decide(c, a, False),
    }
    try:
        handlers[args.cmd](ctx, args)
    except (KillSwitch, BudgetExceeded, ValueError) as err:
        sys.exit(str(err))


if __name__ == "__main__":
    main()

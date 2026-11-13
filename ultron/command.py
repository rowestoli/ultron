"""Talk to Ultron: turn a plain-language request into specific team steps, run only those, report back.

Examples:
  "draft 5 versions of a dog walking log"      -> design 5 distinct printables, queue each for approval
  "design 3 wedding seating charts, don't list" -> design only, nothing goes to the gate
  "just research niches for teachers"           -> research only, nothing designed or listed
  "make #4 $2.99"                               -> revise an item waiting at the gate
  "always use muted colors"                     -> standing instruction saved to the playbook

Nothing here publishes. Listings still wait at the approval gate.
"""
from __future__ import annotations

import json
import math
import re

from .guard import BudgetExceeded

MAX_DRAFTS = 8
MAX_RESEARCH = 10

ROUTER_SYSTEM = """You are Ultron, the orchestrator of an AI team that makes and sells printable PDFs on Etsy
(researcher -> designer -> lister -> owner approval). The owner is talking to you. Turn the message into actions.

Actions you can take:
- {{"type": "research", "count": 1-10, "focus": "optional theme"}}
    Find and score new niches only. Nothing is designed or listed.
- {{"type": "draft", "topic": "the buyer's search phrase", "niche_id": optional existing niche id,
    "count": 1-8, "product_type": "one of: {types}", "notes": "owner's style/content directions",
    "list": true or false}}
    Design `count` distinct versions of a printable. list=true also writes listing copy and sends each
    to the approval gate (nothing goes live without the owner's approval). Use list=false when the owner
    says design only, don't list, or just wants to see options.
- {{"type": "list", "design_id": N}}
    Write listing copy for a design that isn't listed yet and send it to the approval gate.
- {{"type": "revise", "approval_id": N, "instruction": "what to change"}}
    Change an item waiting at the approval gate (price, title, tags, description, colors, pages).
- {{"type": "guidance", "team": "etsy" or "pinterest" or "ultron", "lesson": "the instruction"}}
    A standing instruction every agent should follow from now on. Save to the playbook.
- {{"type": "cycle"}}
    Run a normal full cycle. Only if the owner explicitly asks for a cycle or a full run.

Rules:
- Do exactly what was asked, nothing more. "Just research" means one research action and nothing else.
- Several actions are fine when asked. You can't see one action's results before the next runs, so for
  "research, then draft the best one", do the research and tell the owner to pick a niche to draft.
- No trademarks, brands, celebrities or licensed characters; no recipes, sewing/knitting patterns or
  how-to guides; no medical, legal or financial advice. Decline those in the reply with no action.
- If the request is unclear, ask one short question and take no action.
- Questions about status: answer from the state below, no action.
- Reply in plain language, 1-3 sentences, no hype.

Return: {{"reply": "what you'll do or your answer", "actions": [...]}}"""

ROUTER_PROMPT = """Current state:
{state}

Recent conversation:
{history}

Owner: {message}"""


# --- state the router sees ------------------------------------------------------

def _state(ctx) -> str:
    db = ctx.db
    niches = db.all("SELECT id, keyword, product_type, score, status FROM niches WHERE team = 'etsy' "
                    "ORDER BY id DESC LIMIT 20")
    pending = db.all("SELECT id, payload_json FROM approvals WHERE status = 'pending' ORDER BY id")
    lines = [f"Budget left today: ${ctx.guard.headroom():.2f}",
             "Niches (newest first): " + ("; ".join(
                 f"#{n['id']} {n['keyword']} ({n['product_type']}, score {n['score']}, {n['status']})" for n in niches)
                 or "none")]
    items = []
    for r in pending:
        p = json.loads(r["payload_json"] or "{}")
        price = f" ${p['price_usd']:.2f}" if p.get("price_usd") is not None else ""
        items.append(f"#{r['id']} {p.get('title', '')[:70]}{price}")
    lines.append("Waiting at the approval gate: " + ("; ".join(items) or "nothing"))
    unlisted = db.all("SELECT a.id, a.title, n.keyword FROM assets a LEFT JOIN niches n ON n.id = a.niche_id "
                      "WHERE NOT EXISTS (SELECT 1 FROM listings l WHERE l.asset_id = a.id) ORDER BY a.id DESC LIMIT 15")
    lines.append("Designs not listed yet: " + ("; ".join(f"#{a['id']} {a['title']} ({a['keyword']})" for a in unlisted)
                                                or "none"))
    lines.append(f"Designs made so far: {db.scalar('SELECT COUNT(*) FROM assets')}, "
                 f"listings: {db.scalar('SELECT COUNT(*) FROM listings')}")
    return "\n".join(lines)


def _history(ctx, limit: int = 8) -> str:
    rows = ctx.db.all("SELECT role, text FROM chat ORDER BY id DESC LIMIT ?", limit)
    return "\n".join(f"{'Owner' if r['role'] == 'owner' else 'Ultron'}: {r['text']}" for r in reversed(rows)) or "(none)"


# --- offline stand-in for the router ----------------------------------------------

def _mock_route(message: str) -> dict:
    m = message.lower().strip()
    num = re.search(r"\b(\d+)\b", m)
    n = int(num.group(1)) if num else 3
    if "research" in m and not re.search(r"\b(draft|design)\b|versions? of", m):
        focus = re.sub(r".*research( \d+)?( new)?( niches)?( for| on| about| into)?", "", m)
        focus = re.split(r",|\bdon'?t\b|\bbut\b", focus)[0].strip(" .") or None
        return {"reply": f"Researching {n} niches{f' around {focus}' if focus else ''}. Nothing will be designed or listed.",
                "actions": [{"type": "research", "count": n, "focus": focus}]}
    listed = re.search(r"\blist\s+(?:design\s+)?#?(\d+)", m)
    if listed:
        return {"reply": f"Listing design #{listed.group(1)}.", "actions": [{"type": "list", "design_id": int(listed.group(1))}]}
    hit = re.search(r"(?:versions?|options?|variations?|designs?)\s+of\s+(?:an?\s+)?(.+)", m) or \
        re.search(r"(?:draft|design|make)\s+(?:\d+\s+)?(?:an?\s+)?(.+)", m)
    if hit:
        topic = re.split(r",|\bbut\b|\bdon'?t\b|\bwith\b", hit.group(1))[0].strip(" .")
        dont_list = bool(re.search(r"don'?t list|no listing|design only|just design|without listing", m))
        return {"reply": f"Drafting {n} versions of {topic}.",
                "actions": [{"type": "draft", "topic": topic, "count": n, "product_type": None,
                             "notes": None, "list": not dont_list}]}
    return {"reply": "Mock mode can only handle 'research ...' and 'draft N versions of ...'.", "actions": []}


# --- validation -------------------------------------------------------------------

def _clean(ctx, actions) -> list[dict]:
    types = ctx.team_cfg("etsy")["product_types"]
    out = []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        kind = a.get("type")
        if kind == "research":
            out.append({"type": "research", "count": _clamp(a.get("count"), 3, MAX_RESEARCH),
                        "focus": (str(a.get("focus") or "").strip() or None)})
        elif kind == "draft" and (a.get("topic") or a.get("niche_id")):
            ptype = a.get("product_type") if a.get("product_type") in types else None
            out.append({"type": "draft", "topic": str(a.get("topic") or "").strip().lower() or None,
                        "niche_id": _int(a.get("niche_id")), "count": _clamp(a.get("count"), 1, MAX_DRAFTS),
                        "product_type": ptype, "notes": (str(a.get("notes") or "").strip() or None),
                        "list": a.get("list") is not False})
        elif kind == "list" and _int(a.get("design_id")):
            out.append({"type": "list", "design_id": _int(a["design_id"])})
        elif kind == "revise" and _int(a.get("approval_id")) and a.get("instruction"):
            out.append({"type": "revise", "approval_id": _int(a["approval_id"]), "instruction": str(a["instruction"])})
        elif kind == "guidance" and a.get("lesson"):
            team = a.get("team") if a.get("team") in ("etsy", "pinterest", "ultron") else "etsy"
            out.append({"type": "guidance", "team": team, "lesson": str(a["lesson"]).strip()})
        elif kind == "cycle":
            out.append({"type": "cycle"})
    return out[:4]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp(v, default: int, top: int) -> int:
    n = _int(v)
    return default if n is None else max(1, min(top, n))


# --- running the steps --------------------------------------------------------------

def _etsy(ctx):
    from .teams.etsy import EtsyTeam
    team = EtsyTeam(ctx)
    team.register()
    return team


def _find_or_add_niche(ctx, team, topic: str, product_type: str | None, notes: str | None):
    from .teams.etsy.research import BLOCKED_TERMS
    db = ctx.db
    row = db.one("SELECT * FROM niches WHERE team = 'etsy' AND keyword = ?", topic)
    if row:
        return row
    if any(t in topic for t in BLOCKED_TERMS):
        raise ValueError(f"'{topic}' touches a blocked area (brands, characters, patterns or recipes).")
    types = team.cfg["product_types"]
    ptype = product_type or next((t for t in types if t in topic), None) or types[0]
    count, avg_fav, verified = team.researcher._market_check(topic)
    score = round(math.log1p(avg_fav) / math.log10(count + 10) * 10, 2) if verified else None
    niche_id = db.insert("niches", team="etsy", keyword=topic, product_type=ptype,
                         rationale="Requested by the owner." + (f" {notes}" if notes else ""),
                         competing_listings=count, avg_favorites=avg_fav, score=score, verified=int(verified))
    return db.one("SELECT * FROM niches WHERE id = ?", niche_id)


def _run(ctx, action: dict, log) -> str:
    kind = action["type"]
    if kind == "research":
        team = _etsy(ctx)
        ids = team.researcher.run(action["count"], focus=action["focus"])
        rows = [ctx.db.one("SELECT id, keyword, score, verified FROM niches WHERE id = ?", i) for i in ids]
        found = "; ".join(f"#{r['id']} {r['keyword']} (score {r['score']}{'' if r['verified'] else ', estimated'})"
                          for r in rows)
        return f"Found {len(ids)} niche{'s' if len(ids) != 1 else ''}: {found or 'none new'}."

    if kind == "draft":
        team = _etsy(ctx)
        if action["niche_id"]:
            niche = ctx.db.one("SELECT * FROM niches WHERE id = ?", action["niche_id"])
            if niche is None:
                return f"There's no niche #{action['niche_id']}."
        else:
            niche = _find_or_add_niche(ctx, team, action["topic"], action["product_type"], action["notes"])
        made, queued = [], []
        for i in range(action["count"]):
            log(f"  designing version {i + 1} of {action['count']} for '{niche['keyword']}'")
            asset_id = team.designer.run(niche, notes=action["notes"])
            made.append(f"#{asset_id} " + ctx.db.one("SELECT title FROM assets WHERE id = ?", asset_id)["title"])
            if action["list"]:
                listing_id = team.lister.run(asset_id)
                queued.append(listing_id)
            niche = ctx.db.one("SELECT * FROM niches WHERE id = ?", niche["id"])
        where = ("They're at the approval gate. Approve the ones you like and reject the rest."
                 if action["list"] else "Design only: they're in the Designer's gallery. Say \"list #N\" to send one to the gate.")
        return f"Made {len(made)} version{'s' if len(made) != 1 else ''} of {niche['keyword']}: {', '.join(made)}. {where}"

    if kind == "list":
        asset = ctx.db.one("SELECT id, title FROM assets WHERE id = ?", action["design_id"])
        if asset is None:
            return f"There's no design #{action['design_id']}."
        if ctx.db.one("SELECT 1 FROM listings WHERE asset_id = ?", asset["id"]):
            return f"Design #{asset['id']} already has a listing."
        _etsy(ctx).lister.run(asset["id"])
        return f"Wrote the listing for {asset['title']} (design #{asset['id']}). It's at the approval gate."

    if kind == "revise":
        from .revise import revise
        return f"#{action['approval_id']}: " + revise(ctx, action["approval_id"], action["instruction"])["reply"]

    if kind == "guidance":
        ctx.db.add_lesson(action["team"], action["lesson"], source="owner")
        return f"Saved to the playbook for {action['team']}: {action['lesson']}"

    if kind == "cycle":
        from .orchestrator import Ultron
        out = Ultron(ctx).run_cycle()
        return "Cycle done. " + (out.get("memo") or json.dumps(out["results"], default=str))
    return f"Skipped unknown action {kind}."


def handle(ctx, message: str, log=print) -> dict:
    """Route one owner message, run what it asks for, record both sides of the conversation."""
    message = message.strip()
    if not message:
        return {"reply": "", "results": []}
    ctx.guard.assert_running()
    db = ctx.db
    history = _history(ctx)
    db.insert("chat", role="owner", text=message)

    cap = ctx.cfg["budget"]["cycle_llm_usd"]          # one request may spend at most one cycle's budget
    ctx.guard.start_cycle({"etsy": cap, "pinterest": cap, "ultron": cap})
    try:
        plan = ctx.llm.json(
            ROUTER_SYSTEM.format(types=", ".join(ctx.team_cfg("etsy")["product_types"])),
            ROUTER_PROMPT.format(state=_state(ctx), history=history, message=message),
            team="ultron", agent="chat", tier="smart", max_tokens=800,
            mock=lambda: _mock_route(message))
    except BudgetExceeded as err:
        reply = f"I can't think about that right now: {err}."
        db.insert("chat", role="ultron", text=reply)
        return {"reply": reply, "results": []}

    actions = _clean(ctx, plan.get("actions"))
    reply = str(plan.get("reply") or ("On it." if actions else "I'm not sure what you want me to do.")).strip()
    db.insert("chat", role="ultron", text=reply, actions_json=json.dumps(actions))
    log(f"Ultron: {reply}")

    results = []
    for action in actions:
        try:
            result = _run(ctx, action, log)
        except BudgetExceeded as err:
            result = f"Stopped: {err}. Anything finished before that is saved."
        except Exception as err:
            result = f"{action['type']} failed: {err}"
        results.append(result)
        log(f"  -> {result}")
    if results:
        db.insert("chat", role="ultron", text="\n".join(results))
    return {"reply": reply, "actions": actions, "results": results}

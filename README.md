# Ultron

An orchestrator agent ("Ultron") that splits a fixed AI budget across teams of agents trying to earn real revenue, measures what earns, and moves budget toward it.

Version 1 has two teams:

| Team | Role | Agents |
|---|---|---|
| **etsy** | product (earns) | researcher → designer → lister → analyst |
| **pinterest** | support (drives traffic) | pin writer |

```
                ┌─────────────────────── Ultron ───────────────────────┐
                │ refresh metrics → split budget by ROI → run teams    │
                │ → strategy memo → lessons into the shared playbook   │
                └───────────────┬──────────────────────────┬───────────┘
                         budget │                          │ budget
             ┌──────────────────▼─────────┐     ┌──────────▼───────────────┐
             │ Etsy team                  │     │ Pinterest team           │
             │ research niches (Etsy API) │     │ pins for live listings   │
             │ design PDF + listing image │     │                          │
             │ write listing + disclosure │     │                          │
             │ analyze: scale/keep/kill   │     │                          │
             └──────────────┬─────────────┘     └──────────┬───────────────┘
                            └──── approval queue (you) ────┘
                                         │ publish
                                Etsy API / Pinterest API
                            (or manual upload packets)
```

## Quick start (free, mock mode)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python -m ultron cycle      # research, design 2 printables, queue listings
python -m ultron review     # approve or reject each one
python -m ultron publish    # no Etsy token yet -> writes upload packets to out/packets/
python -m ultron status
```

`config.yaml` ships with `provider: mock`: every agent gets canned answers and costs are simulated. The PDFs and listing images are real, so look in `out/assets/`.

## Go live with an API model

1. Put `ANTHROPIC_API_KEY=...` in `.env`.
2. Set `llm.provider: anthropic` in `config.yaml`, or set `ULTRON_LLM_PROVIDER=anthropic` in `.env`.
3. Start with the default caps ($1/day, $20/month, $0.50/cycle). Routine work runs on Haiku 5.5 ($0.10 / $0.50 per million tokens). Judgment calls run on Sonnet 5.5 ($2 / $10). A cycle typically costs a few cents.

## Connect Etsy

1. Create an app at developer.etsy.com. Put `keystring:shared_secret` in `ETSY_API_KEY`, and register `http://localhost:3003/callback` as a callback URL.
2. **API key only:** the researcher checks every niche against live Etsy search, counting competing listings and averaging favorites on the top results.
3. **`python -m ultron etsy-auth`:** OAuth login. Once connected, approved listings are created through the API and analytics pull views, favorites and sales.
4. **`python -m ultron etsy-taxonomy planner`:** pick a category id and set `teams.etsy.taxonomy_id`.
5. **`publish_state: draft`** (the default) puts listings into your shop as drafts that you activate yourself. Switch it to `active` once you trust the pipeline. Etsy charges $0.20 per active listing, and that fee is logged as an expense. To make drafts live later, run `python -m ultron activate --all` (or pass listing ids); running it is your approval for the fees.

Without OAuth, `publish` writes a folder per listing to `out/packets/etsy/`. Each folder has the PDF, the image, and the title, tags and description, ready to paste into Etsy. After you upload by hand, run:

```bash
python -m ultron link 3 --url https://www.etsy.com/listing/123456789/...
python -m ultron metrics 3 --views 80 --favorites 6 --sales 1 --revenue 4.99   # totals to date, whenever you check
```

## Connect Pinterest

Set `PINTEREST_ACCESS_TOKEN` and `PINTEREST_BOARD_ID`. The pin writer only targets listings that are **active and have a URL**. Pinterest limits what new apps can do, so until you have a working token, pins come out as manual packets in `out/packets/pinterest/`.

## Ultron HQ (the control room)

```bash
python -m ultron hq        # opens http://127.0.0.1:8787 in your browser
```

A 16-bit stone-and-iron hall lit by neon. Ultron sits on the throne, and the agents work at their stations below. Click anything:

| Click | What you see |
|---|---|
| Ultron (the throne) | Latest memo, AI budget, last budget split, profit per team, cycle history, **Run cycle**, **Publish approved** |
| Researcher | Niches found, market scores, status |
| Designer | Every printable, with images and PDFs |
| Lister | Every listing, its status and stats |
| Analyst | Performance, scale or drop decisions, lessons |
| Pin Writer | Pins for Pinterest |
| Approval gate | Everything waiting: **Approve**, **Reject** (your reason is saved to the playbook), **Publish** |
| Revenue (the chest) | Sales, fees, profit |
| Playbook (the bookshelf) | Lessons every agent reads |
| Red lever | Kill switch |

When an agent is working, its station and eyes light up and pulses run along the cables from the throne. Items waiting for you stack up at the gate, and gold piles up in the chest as sales come in. The activity bar at the bottom shows events as they happen, and the page refreshes every 2 seconds. Esc closes a panel and M toggles sound.

Everything stays on your computer. The server listens only on 127.0.0.1, uses no outside fonts, scripts or images, and only serves files from `out/`. Actions need a token that's created fresh each time the server starts, so another website can't press your buttons. Keep `python -m ultron hq` running in one terminal window, and use another window for the other commands.

### Share a read-only copy

```bash
python -m ultron hq-demo out/hq-demo
```

Writes a static copy of HQ with the current data frozen in `state.json`. Upload the folder to any static host. Every button explains what it would do instead of doing it. Nothing from `.env` or the Etsy token goes in, draft listing links and local file paths are dropped, and the PDFs stay out because they're the products for sale (`--include-pdfs` adds them).

## Talk to Ultron

Tell Ultron what you want in plain language. It runs only the steps you ask for.

In HQ, click Ultron and use **Talk to Ultron** at the top of the panel. In the terminal:

```bash
python -m ultron chat "draft 5 versions of a sourdough starter log, warm rustic style"
python -m ultron chat            # back-and-forth until you press Enter on an empty line
```

| You say | Ultron does |
|---|---|
| "draft 5 versions of a dog walking log" | designs 5 distinct printables; nothing is listed yet |
| "list #12" | writes the listing for design 12 and sends it to the approval gate |
| "make and list 2 meal planners" | designs 2 and sends both to the gate |
| "just research teacher printables" | finds and scores niches; nothing is designed or listed |
| "make #4 $2.99" | changes an item waiting at the gate |
| "from now on keep prices under $4" | saves a standing rule to the playbook |
| "how many things are waiting for me?" | answers; does nothing |

One request can spend at most one cycle's budget (`cycle_llm_usd`), and the daily and monthly caps still apply.
Nothing publishes from chat: listings still wait at the gate for your approval.

## Editing before you approve

Every item at the approval gate can be changed before it goes out:

- **Edit:** change the title, price, tags or description by hand, then **Save changes**.
- **Ask AI for changes:** type what you want in plain English and press Enter. For example:
  - "make it $2.99"
  - "shorter title, lead with 'wedding checklist'"
  - "use a darker green and add a notes page"
  - "replace the tag 'planner insert' with 'bridal shower'"

  Changes to the printable itself (colors, pages, columns, wording) make the AI redraw the PDF and listing image. Each item keeps its own chat thread, so you can go back and forth.

Price changes are saved to the playbook, so the lister learns your pricing over time. The AI disclosure is always added back automatically. The same works from the terminal:

```bash
python -m ultron edit 3 --price 2.99 --tags "wedding checklist, bridal planner"
python -m ultron revise 3 make it cheaper and use navy
```

Mock mode can only change prices and colors through the chat. Connect a real model for everything else.

## Daily loop

```bash
python -m ultron cycle && python -m ultron review
```

Every agent reads the shared **playbook** (`python -m ultron lessons`). Lessons come from the analyst, from Ultron's memo, and from you: the note you give when you reject something is saved as a lesson, so the agents stop repeating the mistake.

## Moving to the RTX 3060 PC

Install Ollama on the PC and pull models that fit in 12 GB, such as an 8B model for `fast` and a 14B model compressed to 4-bit for `smart`. Then:

```yaml
llm:
  provider: openai_compatible
  base_url: http://<pc-ip>:11434/v1
  models: {fast: qwen3:8b, smart: qwen3:14b}
```

Nothing else changes. Local models aren't in `pricing`, so they count as $0 and the caps stop binding. Ultron then allocates by revenue alone. A hybrid also works: keep `smart` on the API and run `fast` locally.

## Safety rails

- **Approval gate:** nothing is posted or listed until you approve it (`safety.require_approval`).
- **Budget caps:** daily, monthly and per-cycle limits on LLM spend, plus a per-team share of each cycle. Hitting a cap stops a team mid-cycle without breaking anything.
- **Kill switch:** `python -m ultron stop` (it creates a `STOP` file) blocks every cycle and every publish. `resume` turns it back off.
- **Etsy compliance:** an AI disclosure is appended to every listing in code (not left to the model). Niches with trademarks are blocked, and so are recipes and patterns, which Etsy bans when AI-made.
- **No money-moving permissions:** the agents never touch payments or ad spend.

## Adding a team

1. Subclass `Team` in `ultron/teams/` and implement `run_cycle()`. Queue anything that publishes through `ctx.approvals.queue(...)`, and register its executor in `register()`.
2. Record revenue in the `revenue` table and costs in `expenses`. Ultron's allocation reads both.
3. Add it to `REGISTRY` in `ultron/teams/__init__.py` and under `teams:` in `config.yaml` (`role: product` or `support`).

A paid-scraper team on the Apify Store is the natural next team.

## Moving the project folder

Generated files are saved in the database with full paths. If the folder moves (for example into iCloud Drive), anything that lived under `out/` is found again under the new `out/`, so HQ previews and publishing keep working.

## Files

```
config.yaml            budgets, models, team settings
.env                   secrets (never commit)
data/ultron.db         shared memory: niches, assets, listings, metrics, spend, revenue, playbook
out/assets/            generated PDFs, listing images, pin images
out/packets/           manual upload packets
ultron/orchestrator.py Ultron: allocation + memo
ultron/llm.py          provider-agnostic model layer
ultron/guard.py        caps + kill switch
ultron/approvals.py    approval queue + executors
ultron/revise.py       your edits and AI change requests before approval
ultron/render.py       spec -> PDF / listing image / pin image
ultron/teams/          etsy/ (4 agents), pinterest.py
ultron/hq/             the control room: server.py + index.html (one file, no dependencies)
```

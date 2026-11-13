"""Canned responses so the whole loop runs in mock mode with zero API spend."""
from __future__ import annotations

import random

NICHE_POOL = [
    ("dog medication tracker printable", "tracker", "dog owners managing daily meds"),
    ("rental property maintenance log", "log", "small landlords"),
    ("wedding vendor contact checklist", "checklist", "couples planning a wedding"),
    ("sourdough starter feeding log", "log", "home bakers"),
    ("garage sale pricing checklist", "checklist", "people running a yard sale"),
    ("babysitter info sheet printable", "worksheet", "parents leaving kids with a sitter"),
    ("houseplant watering schedule tracker", "tracker", "plant owners"),
    ("college application deadline tracker", "tracker", "high school seniors and parents"),
    ("home inventory worksheet for insurance", "worksheet", "homeowners and renters"),
    ("fishing trip log printable", "log", "anglers"),
    ("moving day checklist printable", "checklist", "people moving house"),
    ("monthly bill payment tracker", "tracker", "anyone budgeting monthly bills"),
]

PALETTE = ["#2F6F62", "#7A4E9E", "#B5523B", "#2E5E8C", "#8A6D1F", "#4F7A3A"]


def niches(n: int, avoid: set[str], seed: int) -> dict:
    rng = random.Random(seed)
    pool = [p for p in NICHE_POOL if p[0] not in avoid]
    rng.shuffle(pool)
    return {"niches": [
        {"keyword": k, "product_type": t, "buyer": b,
         "rationale": f"Specific search phrase for {b}; practical, reusable printable.",
         "est_demand": rng.randint(2, 5), "est_competition": rng.randint(1, 4)}
        for k, t, b in pool[:n]
    ]}


def design(keyword: str, product_type: str, seed: int) -> dict:
    rng = random.Random(seed)
    name = keyword.replace(" printable", "").title()
    return {
        "title": name,
        "subtitle": f"A simple, fill-in {product_type} that keeps everything in one place.",
        "accent_color": rng.choice(PALETTE),
        "pages": [
            {"heading": "How to use this", "intro": f"Print as many copies of this {product_type} as you need.",
             "blocks": [
                 {"type": "checklist", "title": "Quick start",
                  "items": ["Print the pages you need", "Fill in the basics on page 2", "Keep it somewhere visible",
                            "Review it once a week"]},
                 {"type": "lines", "label": "Notes", "count": 8},
             ]},
            {"heading": "Details", "blocks": [
                {"type": "table", "title": "Key information", "columns": ["Item", "Details", "Notes"],
                 "widths": [1, 2, 2], "rows": 8},
            ]},
            {"heading": name, "intro": "Use one row per entry.", "blocks": [
                {"type": "table", "columns": ["Date", "Entry", "Amount / Time", "Done"],
                 "widths": [1, 3, 1.4, 0.7], "rows": 22},
            ]},
        ],
    }


def listing(keyword: str, price: float) -> dict:
    base = keyword.replace(" printable", "")
    words = base.split()
    tags = [base[:20], "printable pdf", "instant download", "digital download", "fillable planner",
            f"{words[0]} printable"[:20], "organizer printable", "letter size pdf", "gift for organizer",
            "home organization", f"{words[-1]} template"[:20], "minimalist printable", "planner insert"]
    return {
        "title": f"{base.title()} Printable, Instant Download PDF, Letter Size Fill-In {base.split()[-1].title()}",
        "tags": tags,
        "description": f"Stay organized with this {base} printable.\n\nWHAT YOU GET\n- Printable PDF\n- US Letter size\n\n"
                       "HOW IT WORKS\nDownload, print at home or at a print shop, and start filling it in.",
        "price_usd": price,
    }


def review(listing_ids: list[int]) -> dict:
    return {"verdicts": [{"listing_id": i, "verdict": "keep", "reason": "Too early to judge."} for i in listing_ids],
            "lessons": ["Listings need at least 14 days of data before a scale or kill decision."]}

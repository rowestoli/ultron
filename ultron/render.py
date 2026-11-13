"""Turns a design spec (JSON from the designer agent) into a printable PDF, a listing image and a pin image.

Deterministic on purpose: the LLM decides content and structure, code guarantees a clean, printable layout.
No GPU or image model needed, so it runs the same on the laptop and the PC.
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
BOTTOM = 0.75 * inch
INK = HexColor("#2B2B2B")
RULE = HexColor("#B9B9B9")
DEFAULT_ACCENT = "#2F6F62"


def hex_or_default(value: str | None) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else DEFAULT_ACCENT


# --- PDF ------------------------------------------------------------------------

class _PdfWriter:
    def __init__(self, spec: dict, path: Path):
        self.spec = spec
        self.accent = HexColor(hex_or_default(spec.get("accent_color")))
        self.c = canvas.Canvas(str(path), pagesize=letter)
        self.c.setTitle(spec.get("title", "Printable"))
        self.heading = ""
        self.y = PAGE_H

    def _text(self, text, x, y, font="Helvetica", size=10, color=INK):
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        self.c.drawString(x, y, text)

    def _wrap(self, text, font, size, width=CONTENT_W):
        return simpleSplit(str(text), font, size, width)

    def start_page(self, heading: str, intro: str = "", continued: bool = False):
        self.heading = heading
        c = self.c
        c.setFillColor(self.accent)
        c.rect(0, PAGE_H - 10, PAGE_W, 10, stroke=0, fill=1)
        y = PAGE_H - MARGIN - 6
        self._text(self.spec.get("title", ""), MARGIN, y, "Helvetica", 9, RULE)
        y -= 26
        self._text(heading + (" (continued)" if continued else ""), MARGIN, y, "Helvetica-Bold", 20, self.accent)
        y -= 10
        c.setStrokeColor(self.accent)
        c.setLineWidth(1.5)
        c.line(MARGIN, y, MARGIN + 60, y)
        y -= 18
        if intro and not continued:
            for line in self._wrap(intro, "Helvetica", 10.5):
                self._text(line, MARGIN, y, "Helvetica", 10.5)
                y -= 14
            y -= 6
        self.y = y

    def end_page(self):
        n = self.c.getPageNumber()
        self._text(f"{self.spec.get('title', '')}  ·  page {n}", MARGIN, 0.45 * inch, "Helvetica", 8, RULE)
        self.c.showPage()

    def need(self, height: float):
        if self.y - height < BOTTOM:
            self.end_page()
            self.start_page(self.heading, continued=True)

    def label(self, text: str):
        if not text:
            return
        self.need(26)
        self.y -= 4
        self._text(text, MARGIN, self.y, "Helvetica-Bold", 11.5)
        self.y -= 16

    # --- blocks ---

    def block(self, b: dict):
        kind = b.get("type")
        if kind == "text":
            for line in self._wrap(b.get("text", ""), "Helvetica", 10.5):
                self.need(14)
                self._text(line, MARGIN, self.y, "Helvetica", 10.5)
                self.y -= 14
            self.y -= 8
        elif kind == "lines":
            self.label(b.get("label", ""))
            for _ in range(max(1, min(int(b.get("count", 5)), 40))):
                self.need(22)
                self.y -= 20
                self.c.setStrokeColor(RULE)
                self.c.setLineWidth(0.6)
                self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y)
            self.y -= 14
        elif kind == "checklist":
            self.label(b.get("title", ""))
            items = [str(i) for i in b.get("items", [])] + [""] * max(0, min(int(b.get("blank", 0)), 20))
            for item in items:
                lines = self._wrap(item, "Helvetica", 10.5, CONTENT_W - 24) or [""]
                self.need(8 + 14 * len(lines))
                self.c.setStrokeColor(self.accent)
                self.c.setLineWidth(1)
                self.c.rect(MARGIN, self.y - 10, 10, 10, stroke=1, fill=0)
                if item:
                    for i, line in enumerate(lines):
                        self._text(line, MARGIN + 20, self.y - 9 - 14 * i, "Helvetica", 10.5)
                else:
                    self.c.setStrokeColor(RULE)
                    self.c.line(MARGIN + 20, self.y - 10, PAGE_W - MARGIN, self.y - 10)
                self.y -= 8 + 14 * len(lines)
            self.y -= 12
        elif kind == "table":
            self.label(b.get("title", ""))
            cols = [str(c) for c in b.get("columns", [])][:8] or ["Item", "Notes"]
            weights = b.get("widths") if isinstance(b.get("widths"), list) and len(b["widths"]) == len(cols) else [1] * len(cols)
            total = sum(float(w) for w in weights) or 1
            widths = [CONTENT_W * float(w) / total for w in weights]
            rows = max(1, min(int(b.get("rows", 10)), 40))

            def header():
                self.need(22)
                self.c.setFillColor(self.accent)
                self.c.rect(MARGIN, self.y - 18, CONTENT_W, 18, stroke=0, fill=1)
                x = MARGIN
                for col, w in zip(cols, widths):
                    label = self._wrap(col, "Helvetica-Bold", 9, w - 8)[:1] or [""]
                    self._text(label[0], x + 4, self.y - 13, "Helvetica-Bold", 9, HexColor("#FFFFFF"))
                    x += w
                self.y -= 18

            header()
            for _ in range(rows):
                if self.y - 22 < BOTTOM:
                    self.need(1000)  # force a new page, then repeat the header
                    header()
                self.c.setStrokeColor(RULE)
                self.c.setLineWidth(0.6)
                self.c.rect(MARGIN, self.y - 22, CONTENT_W, 22, stroke=1, fill=0)
                x = MARGIN
                for w in widths[:-1]:
                    x += w
                    self.c.line(x, self.y, x, self.y - 22)
                self.y -= 22
            self.y -= 16

    def write(self):
        for page in self.spec.get("pages", []):
            self.start_page(page.get("heading", ""), page.get("intro", ""))
            for b in page.get("blocks", []):
                self.block(b)
            self.end_page()
        self.c.save()


def render_pdf(spec: dict, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    _PdfWriter(spec, path).write()
    with pymupdf.open(path) as doc:
        return doc.page_count


# --- images ----------------------------------------------------------------------

FONT_CANDIDATES = {
    True: ["DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    False: ["DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def _font(size: int, bold: bool = False):
    for name in FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _page_image(pdf: Path, index: int, height: int) -> Image.Image | None:
    with pymupdf.open(pdf) as doc:
        if index >= doc.page_count:
            return None
        pix = doc[index].get_pixmap(dpi=150)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img.resize((int(img.width * height / img.height), height), Image.LANCZOS)


def _paste_with_shadow(bg: Image.Image, img: Image.Image, xy: tuple[int, int]):
    shadow = Image.new("RGBA", (img.width + 60, img.height + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([30, 30, 30 + img.width, 30 + img.height], fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    bg.paste(shadow, (xy[0] - 18, xy[1] - 12), shadow)
    bg.paste(img, xy)


def _tint(hex_color: str, amount: float) -> tuple[int, int, int]:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(int(c + (255 - c) * amount) for c in (r, g, b))


def _wrap_px(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_listing_image(pdf: Path, spec: dict, pages: int, out: Path) -> Path:
    """2000x1600 listing photo: page mockups on the left, product name and specs on the right."""
    accent = hex_or_default(spec.get("accent_color"))
    bg = Image.new("RGB", (2000, 1600), _tint(accent, 0.86))
    # Front: the last page (usually the main fill-in page). Behind it: page 1.
    first = _page_image(pdf, max(pages - 1, 0), 1300)
    second = _page_image(pdf, 0, 1250) if pages > 1 else None
    if second:
        _paste_with_shadow(bg, second, (250, 150))
    _paste_with_shadow(bg, first, (120, 220))

    draw = ImageDraw.Draw(bg)
    x, width = 1250, 690
    y = 380
    for line in _wrap_px(draw, spec.get("title", ""), _font(78, True), width)[:4]:
        draw.text((x, y), line, font=_font(78, True), fill=accent)
        y += 92
    y += 30
    for line in _wrap_px(draw, spec.get("subtitle", ""), _font(40), width)[:3]:
        draw.text((x, y), line, font=_font(40), fill=(60, 60, 60))
        y += 52
    y += 50
    for fact in ["Printable PDF", f"{pages} page{'s' if pages != 1 else ''} · US Letter", "Instant download"]:
        draw.rounded_rectangle([x, y, x + 520, y + 70], radius=35, fill=accent)
        draw.text((x + 36, y + 14), fact, font=_font(36, True), fill="white")
        y += 95
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, optimize=True)
    return out


def make_pin_image(pdf: Path, title: str, accent: str | None, out: Path) -> Path:
    """1000x1500 vertical pin: headline band on top, first page below."""
    accent = hex_or_default(accent)
    bg = Image.new("RGB", (1000, 1500), _tint(accent, 0.88))
    draw = ImageDraw.Draw(bg)
    draw.rectangle([0, 0, 1000, 420], fill=accent)
    y = 70
    for line in _wrap_px(draw, title, _font(64, True), 860)[:4]:
        draw.text((70, y), line, font=_font(64, True), fill="white")
        y += 78
    draw.text((70, 345), "Printable PDF · instant download", font=_font(34), fill=_tint(accent, 0.7))
    page = _page_image(pdf, 0, 1000)
    _paste_with_shadow(bg, page, ((1000 - page.width) // 2, 470))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, optimize=True)
    return out

"""Server-side PDF rendering for GET /articles/{article_id}/pdf — turns an already-generated
ArticleOutput into a downloadable PDF, using the same theme colors and layout shape as the demo
page's rendered view (header band with headline/subheadline, hero placeholder, body sections, CTA).

Built with reportlab (pure Python, no native system libraries) rather than an HTML/CSS-to-PDF tool
like weasyprint. weasyprint needs Pango/Cairo/GDK-Pixbuf, which aren't part of Azure App Service's
standard Python Linux runtime — getting them installed reliably would likely mean moving off App
Service to a custom Docker container, just for this one feature. reportlab is a normal pip
dependency and needs nothing extra from the hosting environment (decision-log.md — PDF export
entry, reverses the earlier weasyprint pick once this dependency risk surfaced at build time).

Deliberately knows nothing about FastAPI or HTTP — takes an ArticleOutput in, returns PDF bytes
out. Same separation-of-concerns pattern as generation.py relative to routers/generate.py.

Every piece of article text is run through `_esc()` (xml.sax.saxutils.escape) before being wrapped
in a reportlab Paragraph. reportlab's Paragraph has its own lightweight markup language and treats
"<", ">", and "&" as tag-delimiting syntax (it understands things like <b>, <i>, <br/>) — text
containing an unescaped "<...>" (e.g. a hand-edit like "<EDITED TEXT> ...") gets silently parsed as
an unrecognized tag and dropped rather than rendered literally, with no error raised. Escaping
first makes any literal angle bracket or ampersand in article text (LLM-authored or hand-edited via
/revise) display as written instead of being swallowed.

Text also goes through `_normalize_hyphens()` first: the model frequently writes compound words
(e.g. "cross-plant", "45-minute") using the Unicode HYPHEN (U+2010) or NON-BREAKING HYPHEN (U+2011)
codepoints rather than plain ASCII hyphen-minus (U+002D). reportlab's default Helvetica font (base
14 PDF font, WinAnsiEncoding) has no glyph for either Unicode variant, so they render as a visible
"missing glyph" box — confirmed by extracting text from a rendered PDF. Browsers never show this
because system fonts cover Unicode hyphens, which is why the bug only appears in the export, not
the on-screen render.
"""
from __future__ import annotations

import io
import logging
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import ArticleOutput, ImageSlot
from app.storage import download_blob_bytes

logger = logging.getLogger(__name__)

_HYPHEN_VARIANTS = "‐‑"  # HYPHEN, NON-BREAKING HYPHEN — no glyph in reportlab's default Helvetica font


def _clean(text: str) -> str:
    """Normalizes Unicode hyphen variants to ASCII hyphen-minus, then XML-escapes the result.
    Every piece of dynamic article text should be passed through this (not just `_esc()` alone)
    before being wrapped in a reportlab Paragraph — see the module docstring for why both steps
    are needed."""
    for variant in _HYPHEN_VARIANTS:
        text = text.replace(variant, "-")
    return _esc(text)


_PAGE_WIDTH, _PAGE_HEIGHT = LETTER
_MARGIN = 0.6 * inch
_CONTENT_WIDTH = _PAGE_WIDTH - 2 * _MARGIN

_HEADLINE_STYLE = ParagraphStyle(
    "headline", fontName="Helvetica-Bold", fontSize=20, textColor=colors.white, leading=24,
)
_SUBHEADLINE_STYLE = ParagraphStyle(
    "subheadline", fontName="Helvetica", fontSize=12, textColor=colors.white, leading=15,
)
_LOGO_FALLBACK_STYLE = ParagraphStyle(
    "logo-fallback", fontName="Helvetica", fontSize=8, textColor=colors.white,
)
_HERO_CAPTION_STYLE = ParagraphStyle(
    "hero-caption", fontName="Helvetica-Oblique", fontSize=9,
    textColor=colors.HexColor("#555555"), alignment=1,
)
_META_STYLE = ParagraphStyle(
    "meta", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#6b7280"),
)

# theme.secondary_color is the actual background of the body-sections + CTA panel (2026-07-31
# revision — see _content_panel() below), so section heading/body/CTA text color can't be a fixed
# module-level style the way the ones above are: it has to be picked per-article, based on whatever
# secondary_color that article's caller supplied. _section_styles()/_cta_style() build those on
# demand instead.

_PANEL_H_PADDING = 18
_PANEL_V_PADDING = 18
_PANEL_INNER_WIDTH = _CONTENT_WIDTH - 2 * _PANEL_H_PADDING


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative-luminance formula — same one demo/index.html's relativeLuminance() uses, kept
    as two independent implementations (JS for the browser, this for reportlab) rather than one
    shared source, since the two runtimes don't share code today. Keep both in sync if this ever
    changes."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def _lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _readable_text_color(bg_hex: str) -> colors.Color:
    """Picks dark or light text so the secondary_color panel stays legible whether the caller's
    color choice is light or dark — mirrors demo/index.html's readableTextColor()."""
    return colors.HexColor("#1a1a1a") if _relative_luminance(bg_hex) > 0.5 else colors.HexColor("#f5f5f5")


def _section_styles(text_color: colors.Color) -> tuple[ParagraphStyle, ParagraphStyle]:
    heading = ParagraphStyle(
        "section-heading", fontName="Helvetica-Bold", fontSize=13,
        spaceBefore=14, spaceAfter=4, textColor=text_color,
    )
    body = ParagraphStyle(
        "section-body", fontName="Helvetica", fontSize=10.5, leading=15, textColor=text_color,
    )
    return heading, body


def _cta_style() -> ParagraphStyle:
    # CTA text stays white regardless of secondary_color, unchanged from before this revision —
    # it sits on theme.accent_color (the button), not on the secondary panel behind it.
    return ParagraphStyle("cta", fontName="Helvetica-Bold", fontSize=12, textColor=colors.white, alignment=1)


def _logo_flowable(slot: ImageSlot, width: float = 1.3 * inch, height: float = 0.45 * inch):
    """Real embedded image if the blob downloads successfully, otherwise a bracketed text label —
    same fallback shape as the demo page's logoBoxHtml(), just producing a PDF flowable instead of
    a DOM node. A download failure here (missing blob, transient network issue) never fails the
    whole PDF — it degrades to the same placeholder text the caption already carries."""
    if slot.blob_path:
        try:
            image_bytes = download_blob_bytes(slot.blob_path)
            return Image(io.BytesIO(image_bytes), width=width, height=height, kind="proportional")
        except Exception:
            logger.warning(
                "pdf_export: could not download %s for slot %s, falling back to a text label.",
                slot.blob_path, slot.slot_id,
            )
    return Paragraph(f"[{_clean(slot.caption)}]", _LOGO_FALLBACK_STYLE)


def _header_band(article: ArticleOutput, slots: dict[str, ImageSlot]) -> Table:
    """Sender/receiver logos on one row, headline (+ optional subheadline) below, all on a
    primary_color background — mirrors the demo page's .article-header block."""
    rows = [[_logo_flowable(slots["logo_sender"]), "", _logo_flowable(slots["logo_receiver"])]]
    style_commands = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(article.theme.primary_color)),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("VALIGN", (2, 0), (2, 0), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]

    headline_row = len(rows)
    rows.append([Paragraph(_clean(article.headline), _HEADLINE_STYLE), "", ""])
    style_commands.append(("SPAN", (0, headline_row), (-1, headline_row)))

    if article.subheadline:
        subheadline_row = len(rows)
        rows.append([Paragraph(_clean(article.subheadline), _SUBHEADLINE_STYLE), "", ""])
        style_commands.append(("SPAN", (0, subheadline_row), (-1, subheadline_row)))

    table = Table(rows, colWidths=[_CONTENT_WIDTH * 0.4, _CONTENT_WIDTH * 0.2, _CONTENT_WIDTH * 0.4])
    table.setStyle(TableStyle(style_commands))
    return table


def _hero_flowable(hero: ImageSlot | None):
    """Real image if the blob downloads, otherwise a bracketed caption — matches the demo page's
    treatment (and the documented known limitation that hero_contextual has no real image source
    in this prototype: limitations.md)."""
    if hero is None:
        return None
    if hero.blob_path:
        try:
            image_bytes = download_blob_bytes(hero.blob_path)
            return Image(io.BytesIO(image_bytes), width=_CONTENT_WIDTH, height=2.2 * inch, kind="proportional")
        except Exception:
            logger.warning("pdf_export: could not download hero image %s, falling back to caption.", hero.blob_path)
    return Paragraph(f"[ Image: {_clean(hero.caption)} ]", _HERO_CAPTION_STYLE)


def _cta_band(article: ArticleOutput, width: float = _CONTENT_WIDTH) -> Table:
    table = Table([[Paragraph(_clean(article.call_to_action), _cta_style())]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(article.theme.accent_color)),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return table


def _content_panel(article: ArticleOutput) -> Table:
    """Body sections + CTA button, all on one theme.secondary_color background — mirrors
    demo/index.html's .body-sections/.cta-row treatment (2026-07-31 revision). Superseded an
    earlier version of this function that rendered secondary_color as a thin divider strip above a
    plain-white body; that gave secondary_color no real purpose in the layout, per feedback that
    it should actually color the document's content area the way primary_color colors the header
    and accent_color colors the CTA button.

    Built as ONE Table (not separate flowables placed directly in the story) so the colored
    background covers everything — headings, body paragraphs, and the CTA row — as a single
    continuous panel, the same way a Table is what gives _header_band its solid-color background
    above. The CTA sub-table is nested inside this table's one cell, sized to _PANEL_INNER_WIDTH
    (not _CONTENT_WIDTH) since that's the width actually available once this panel's own left/right
    padding is subtracted — passing the wrong width here would overflow the cell."""
    text_color = _readable_text_color(article.theme.secondary_color)
    heading_style, body_style = _section_styles(text_color)

    content: list = []
    for section in article.body_sections:
        content.append(Paragraph(_clean(section.heading), heading_style))
        content.append(Paragraph(_clean(section.text), body_style))
    content.append(Spacer(1, 12))
    content.append(_cta_band(article, width=_PANEL_INNER_WIDTH))

    table = Table([[content]], colWidths=[_CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(article.theme.secondary_color)),
        ("LEFTPADDING", (0, 0), (-1, -1), _PANEL_H_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), _PANEL_H_PADDING),
        ("TOPPADDING", (0, 0), (-1, -1), _PANEL_V_PADDING),
        ("BOTTOMPADDING", (0, 0), (-1, -1), _PANEL_V_PADDING),
    ]))
    return table


def build_pdf(article: ArticleOutput) -> bytes:
    """Renders one ArticleOutput to PDF bytes. Pure function apart from the best-effort logo/hero
    image downloads above — callers (routers/articles.py) decide what to do with the returned
    bytes (this module never touches the HTTP layer itself)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER,
        leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN,
    )

    slots = {s.slot_id: s for s in article.image_slots}
    story = [_header_band(article, slots), Spacer(1, 14)]

    hero_flowable = _hero_flowable(slots.get("hero_contextual"))
    if hero_flowable is not None:
        story.append(hero_flowable)
        story.append(Spacer(1, 16))

    story.append(_content_panel(article))
    story.append(Spacer(1, 16))

    meta_line = (
        f"article_id: {article.article_id} | version: {article.version} | "
        f"status: {article.status} | words: {article.total_word_count} | "
        f"created_at: {article.created_at.isoformat()}"
    )
    story.append(Paragraph(meta_line, _META_STYLE))

    doc.build(story)
    return buffer.getvalue()

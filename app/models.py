"""Pydantic schemas.

Kept in one place so the API contract is easy to scan, and so parsing.py and
storage.py can share the same typed objects (ParsedDocument, ExtractedImage)
without depending on each other directly.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

Role = Literal["sender", "receiver"]


def word_count(text: str) -> int:
    """Whitespace-split word count — matches how the layout template's word limits are phrased
    ("<= 12 words"), not a token count. Backs every word-limit validator below."""
    return len(text.split())


# ---- POST /upload -----------------------------------------------------------

class DocumentIngestResult(BaseModel):
    """Outcome of ingesting a single uploaded PDF."""

    document_id: UUID
    sender_id: str
    receiver_id: str
    role: Role
    filename: str
    status: Literal["parsed", "failed"]
    pages_extracted: int = 0
    tables_extracted: int = 0
    images_extracted: int = 0
    error: str | None = None
    uploaded_at: datetime


class UploadResponse(BaseModel):
    """Response body of POST /upload — one result per uploaded file."""

    results: list[DocumentIngestResult]


# ---- Internal contract between parsing.py and storage.py --------------------
#
# Being Pydantic models (not plain dicts or dataclasses) means this contract
# is actually enforced, not just documented: when parsing.py constructs
# `ParsedDocument(text=..., tables=..., page_count=..., images=...)`, Pydantic
# checks each value against its declared type right then. If parsing.py's
# logic ever produced the wrong shape (e.g. a table as a string instead of a
# list of rows), this line raises a ValidationError immediately — instead of
# a malformed object silently flowing into storage.py and failing somewhere
# less obvious later.

class ExtractedImage(BaseModel):
    filename: str
    content: bytes  # raw image file bytes (real PNG/JPEG bytes) — not base64, not a path
    page_number: int


class ParsedDocument(BaseModel):
    """What parsing.py hands back. Not part of the public API — /upload's response
    is DocumentIngestResult, built separately in routers/upload.py."""

    text: str
    tables: list[list[list[str]]] = Field(default_factory=list)  # list of tables -> list of rows -> list of cell strings
    page_count: int
    images: list[ExtractedImage] = Field(default_factory=list)


# ---- POST /generate ----------------------------------------------------------

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ThemeColors(BaseModel):
    """Header/CTA/accent colors for the rendered layout. Caller-supplied on every /generate
    call and echoed straight through to ArticleOutput.theme — the LLM never sees or writes these."""

    primary_color: str
    secondary_color: str
    accent_color: str

    @field_validator("primary_color", "secondary_color", "accent_color")
    @classmethod
    def _must_be_hex(cls, v: str) -> str:
        if not _HEX_COLOR_RE.match(v):
            raise ValueError(f"'{v}' is not a 6-digit hex color like #1B3A5C")
        return v


class GenerateRequest(BaseModel):
    """Request body of POST /generate."""

    sender_id: str  # pairs with an existing /upload role="sender" batch
    receiver_id: str  # pairs with an existing /upload role="receiver" batch
    theme: ThemeColors
    feedback: str | None = None  # optional, plain string carried over from a prior /evaluate call


class BodySection(BaseModel):
    """One heading + body chunk of the article. Shared, as-is, between LLMDraft, ArticleOutput, and
    ArticleRevisionRequest (via _HeadlineSubheadlineCtaLimits' sibling body_sections field) — so a
    hand-edit via POST /articles/{article_id}/revise is held to the exact same limits an LLM-authored
    draft is.

    heading previously had no length limit at all (only `text` did) — a customer's hand-edit via
    /revise could submit an arbitrarily long section heading and it would save successfully, no
    422, nothing flagged. Fixed 2026-07-31: heading now gets the same 12-word cap as the article's
    top-level `headline`, since a body_sections heading is the same kind of UI element (a short
    section title, not a sentence) — see decision-log.md."""

    heading: str
    text: str

    @field_validator("heading")
    @classmethod
    def _heading_max_12_words(cls, v: str) -> str:
        n = word_count(v)
        if n > 12:
            raise ValueError(f"body_sections[].heading must be <=12 words, got {n}: '{v}'")
        return v

    @field_validator("text")
    @classmethod
    def _max_150_words(cls, v: str) -> str:
        n = word_count(v)
        if n > 150:
            raise ValueError(f"body_sections[].text must be <=150 words, got {n}")
        return v


ImageSlotId = Literal["logo_sender", "logo_receiver", "hero_contextual"]
ImageSourceType = Literal["sender_logo", "receiver_logo", "contextual"]


class ImageSlot(BaseModel):
    """One of the article's 3 fixed image slots — always sender logo, receiver logo, then one
    contextual hero image, in that order. blob_path is resolved from Table Storage
    (generation.resolve_image_slots); caption is LLM-authored."""

    slot_id: ImageSlotId
    source_type: ImageSourceType
    blob_path: str | None
    image_url: str | None = None  # short-lived SAS URL for blob_path, browser-fetchable directly
    caption: str


class _HeadlineSubheadlineCtaLimits(BaseModel):
    """Shared word-limit validators for headline/subheadline/call_to_action — pulled out so
    LLMDraft (model-authored) and ArticleRevisionRequest (customer-hand-edited) enforce the exact
    same limits from one place instead of two copies drifting apart over time."""

    headline: str  # <=12 words
    subheadline: str | None = None  # optional, <=20 words
    call_to_action: str  # <=25 words

    @field_validator("headline")
    @classmethod
    def _headline_max_12(cls, v: str) -> str:
        n = word_count(v)
        if n > 12:
            raise ValueError(f"headline must be <=12 words, got {n}: '{v}'")
        return v

    @field_validator("subheadline")
    @classmethod
    def _subheadline_max_20(cls, v: str | None) -> str | None:
        if v is None:
            return v
        n = word_count(v)
        if n > 20:
            raise ValueError(f"subheadline must be <=20 words, got {n}: '{v}'")
        return v

    @field_validator("call_to_action")
    @classmethod
    def _cta_max_25(cls, v: str) -> str:
        n = word_count(v)
        if n > 25:
            raise ValueError(f"call_to_action must be <=25 words, got {n}: '{v}'")
        return v


class LLMDraft(_HeadlineSubheadlineCtaLimits):
    """The subset of ArticleOutput the model is responsible for authoring. This, not
    ArticleOutput, is passed as `response_format` to Azure OpenAI (app/generation.py) — the
    model is never shown article_id, version, sender_id/receiver_id, or theme, so it can't
    hallucinate or overwrite them.

    body_sections' 2-3 item count is enforced structurally via Field(min_length/max_length) as
    part of the JSON Schema sent to Azure OpenAI. Word limits use @field_validator instead — JSON
    Schema has no word-count primitive, so these can only be checked after the API responds; a
    failure here is exactly what generation.generate_draft_with_retry() retries on.
    """

    body_sections: list[BodySection] = Field(min_length=2, max_length=3)
    sender_logo_caption: str  # typically just the sender's display name
    receiver_logo_caption: str  # typically just the receiver's display name
    contextual_image_caption: str  # describes what the hero image should depict


class ArticleOutput(BaseModel):
    """Response body of POST /generate. Field-for-field match of the layout/schema
    contract documented in docs/architecture/api-payload-schemas.drawio. Nothing here is typed by
    the LLM directly — generation.assemble_article() builds it by combining LLMDraft (LLM-authored),
    GenerateRequest (caller input, echoed), and freshly computed values (article_id, version,
    total_word_count, created_at)."""

    article_id: UUID
    sender_id: str
    receiver_id: str
    version: int  # auto-incremented per (sender_id, receiver_id) pair
    headline: str
    subheadline: str | None = None
    body_sections: list[BodySection]
    call_to_action: str
    image_slots: list[ImageSlot] = Field(min_length=3, max_length=3)
    theme: ThemeColors
    total_word_count: int  # target 300-600 — soft, logged not enforced (see generate_article)
    status: Literal["draft"] = "draft"  # no publish workflow in this prototype
    created_at: datetime


# ---- POST /articles/{article_id}/revise --------------------------------------

class ArticleRevisionRequest(_HeadlineSubheadlineCtaLimits):
    """Request body of POST /articles/{article_id}/revise — a customer's hand-edit of a
    previously generated article. Deliberately has NO sender_id, receiver_id, theme, article_id,
    version, created_at, or image blob_path/image_url fields — those are server-controlled and
    always taken from the original article (generation.assemble_revised_article), not from
    client input. Unlike the /generate -> /revise "force-overwrite protected fields" approach,
    this model makes the locked fields structurally impossible to submit in the first place.

    image_captions is keyed by slot_id and optional per-key: only slots the customer actually
    edited need to be included, everything else keeps its existing caption. blob_path/image_url
    are never editable — see docstring above.
    """

    body_sections: list[BodySection] = Field(min_length=2, max_length=3)
    image_captions: dict[ImageSlotId, str] = Field(default_factory=dict)


# ---- POST /evaluate -----------------------------------------------------------
#
# Deliberately decoupled from /generate — this endpoint never calls /generate, and /generate
# never calls this. The only thing connecting them is a shared contract: EvaluationResult.feedback
# is a plain str, the exact same shape as GenerateRequest.feedback, so a caller can take an
# /evaluate response and hand its .feedback straight to /generate with no reshaping. Whether (and
# when) to actually do that regenerate round trip is left to the caller — the demo UI does it as
# an explicit "Regenerate with this feedback" button click, not an automatic threshold-triggered
# loop (decision-log.md §9).
#
# No deterministic schema/word-limit re-check here: ArticleOutput's own fields (BodySection.text,
# image_slots length, etc.) are already Pydantic-validated at the moment an article is generated
# or revised — re-checking the same constraints against the same stored object would just repeat
# work Pydantic already did, not catch anything new. What /evaluate adds is the thing Pydantic
# structurally can't check: is the content actually good (grounded, relevant, coherent) — hence
# LLM-as-judge only.

class JudgeDraft(BaseModel):
    """The subset of EvaluationResult the LLM judge is responsible for authoring — mirrors how
    LLMDraft relates to ArticleOutput above. Passed as `response_format` to Azure OpenAI
    (generation.judge_article) so the model is structurally incapable of writing article_id,
    version, threshold, passed, or overall_score — those are always computed by generation.py, not
    the judge itself."""

    groundedness: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    coherence: float = Field(ge=0, le=1)
    feedback: str  # single paragraph — specific, actionable, same shape as GenerateRequest.feedback


class EvaluateRequest(BaseModel):
    """Request body of POST /evaluate — deliberately just an ID, per decision-log.md §13. Looks up
    an already-generated/-revised article (get_article, the same lookup /revise uses) rather than
    taking article content directly, so the score is always against what's actually persisted."""

    article_id: UUID


class EvaluationResult(BaseModel):
    """Response body of POST /evaluate. Three LLM-as-judge sub-scores (0-1 each, G-Eval style,
    RAGAS-inspired faithfulness concept — decision-log.md §9), plus their mean as overall_score.
    All three are always shown alongside the mean, not just the mean, so the score fusion choice
    itself (why mean vs. weighted) is a visible, discussable part of the output.

    Not persisted anywhere — computed fresh on every call and returned directly. A prototype-scale
    simplicity choice: re-evaluating is cheap (one LLM call), so there's no stored evaluation
    history to keep in sync. See limitations.md."""

    article_id: UUID
    version: int
    groundedness: float  # 0-1 — every claim traceable to SENDER/RECEIVER context actually on file
    relevance: float  # 0-1 — speaks to the receiver's actual context, not generic copy
    coherence: float  # 0-1 — tone/flow/structure quality, independent of factual grounding
    overall_score: float  # mean(groundedness, relevance, coherence) — see docstring above
    threshold: float  # echoes settings.evaluation_pass_threshold at evaluation time
    passed: bool  # overall_score >= threshold
    feedback: str  # single AI-composed paragraph — same shape as GenerateRequest.feedback
    evaluated_at: datetime


# ---- GET /articles ------------------------------------------------------------

class ArticleListResponse(BaseModel):
    """Response body of GET /articles?sender_id=&receiver_id= — every version generated so far
    for one pair (auto-/generate and hand-edited /revise rows both included, newest first), so
    the demo can browse and re-render a past version without calling /generate again."""

    articles: list[ArticleOutput]

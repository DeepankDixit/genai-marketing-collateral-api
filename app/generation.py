"""Prompt construction, Azure OpenAI call, validation retry, and article assembly for /generate.

Deliberately knows nothing about FastAPI or HTTP — it takes context dicts (from
storage.fetch_context) and a GenerateRequest in, returns an ArticleOutput out. Mirrors
parsing.py's separation from the router: keeps routers/generate.py thin and this file
independently testable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from openai import AzureOpenAI
from pydantic import ValidationError

from app.config import settings
from app.models import (
    ArticleOutput,
    ArticleRevisionRequest,
    EvaluationResult,
    GenerateRequest,
    ImageSlot,
    JudgeDraft,
    LLMDraft,
    word_count,
)
from app.storage import get_image_url, get_next_version

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # 1 initial attempt + 1 retry with the validation error fed back as feedback

SYSTEM_PROMPT = """You are a B2B marketing copywriter for a marketing agency. You write short, \
factually grounded outbound marketing articles that bridge a SENDER company (what they sell) and a \
RECEIVER company (who the article is being written for).

Rules:
1. Every factual claim must be grounded in the SENDER CONTEXT or RECEIVER CONTEXT provided in the \
user message. Do not invent facts, statistics, product features, or company details that are not \
present in that context.
2. The article must draw from BOTH sides — at least one body section should reference a specific \
receiver pain point, and at least one should reference a specific sender capability that addresses \
it. Do not write generic copy that could apply to any receiver.
3. Tone: persuasive, professional outbound marketing collateral — not a dry summary and not an RFP \
response.
4. Word limits (hard constraints, checked automatically after you respond — stay under these):
   - headline: <= 12 words
   - subheadline: <= 20 words (optional — return null if it doesn't add value)
   - each body_sections[].text: <= 150 words
   - body_sections: exactly 2 or 3 sections
   - call_to_action: <= 25 words
5. sender_logo_caption / receiver_logo_caption: just the company's display name.
6. contextual_image_caption: describe what a supporting hero image for this article should depict \
(no image is actually generated from this caption in this prototype — see resolve_image_slots).
"""


def format_context_for_prompt(ctx: dict) -> str:
    """Renders one side's context bundle (storage.fetch_context's output) as a plain-text block:
    the parsed PDF text, followed by every extracted table as "|"-separated rows, so facts that
    only exist in a table are visible to the model as readable text. ctx["image_paths"] is
    deliberately not included — images are never sent to the LLM as text; they're resolved
    separately in resolve_image_slots, after the LLM call, from blob paths already on hand."""
    lines = [ctx["text"]]
    for i, table in enumerate(ctx["tables"], start=1):
        lines.append(f"\n[Table {i}]")
        for row in table:
            lines.append(" | ".join(row))
    return "\n".join(lines)


def build_user_prompt(sender_ctx: dict, receiver_ctx: dict, feedback: str | None = None) -> str:
    """Labeled SENDER CONTEXT / RECEIVER CONTEXT blocks, optional FEEDBACK block (a plain string
    from a prior /evaluate call), and a closing instruction. The explicit labels are the actual
    mechanism behind the domain-bridging requirement — the model never has to guess which company
    is selling vs. being pitched to."""
    parts = [
        "=== SENDER CONTEXT (the company being marketed — what they sell) ===\n"
        + format_context_for_prompt(sender_ctx),
        "=== RECEIVER CONTEXT (the pitch target — who this article is for) ===\n"
        + format_context_for_prompt(receiver_ctx),
    ]
    if feedback:
        parts.append(
            "=== FEEDBACK FROM A PRIOR REVIEW — address this specifically in the new draft ===\n"
            + feedback
        )
    parts.append("Write the article now. Ground every claim in the context above — do not invent facts.")
    return "\n\n".join(parts)


def get_client() -> AzureOpenAI:
    """Builds the Azure OpenAI SDK client from Settings. Not cached — built fresh per call, same
    deliberate simplicity trade-off as storage.py's _blob_service()/_table_service()."""
    assert settings.azure_openai_endpoint and settings.azure_openai_api_key, (
        "AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set. Provision the Azure OpenAI resource "
        "and fill .env first."
    )
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def generate_draft(sender_ctx: dict, receiver_ctx: dict, feedback: str | None = None) -> LLMDraft:
    """The one Azure OpenAI call in this module — everything else here is prompt-building or
    post-processing. Uses structured-output mode (response_format=LLMDraft) so the model is
    structurally incapable of writing article_id, version, sender_id/receiver_id, or theme. Has no
    retry logic of its own — that's generate_draft_with_retry() below; this function is one prompt
    in, one validated draft out."""
    client = get_client()
    user_prompt = build_user_prompt(sender_ctx, receiver_ctx, feedback)
    completion = client.beta.chat.completions.parse(
        model=settings.azure_openai_deployment_name,  # Azure OpenAI: deployment name, not base model name
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format=LLMDraft,
    )
    choice = completion.choices[0]
    if choice.message.refusal:
        raise RuntimeError(f"Model refused: {choice.message.refusal}")
    return choice.message.parsed


def generate_draft_with_retry(
    sender_ctx: dict, receiver_ctx: dict, feedback: str | None = None
) -> tuple[LLMDraft, int]:
    """Validation and repair: not blind trust in the LLM's JSON. Azure OpenAI's structured-output
    mode already guarantees the *shape* (required fields, body_sections count), but has no
    word-count primitive — that's enforced by LLMDraft's Pydantic validators, which can still fail
    on a structurally valid response. On failure, the ValidationError is fed back to the model as
    extra instruction for one retry (MAX_ATTEMPTS=2 total) before giving up loud.

    Returns (validated draft, attempts_used).
    """
    last_error: ValidationError | None = None
    current_feedback = feedback
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return generate_draft(sender_ctx, receiver_ctx, feedback=current_feedback), attempt
        except ValidationError as exc:
            last_error = exc
            retry_note = f"Your previous draft failed validation — fix this and resubmit:\n{exc}"
            current_feedback = f"{feedback}\n\n{retry_note}" if feedback else retry_note
    raise RuntimeError(f"Gave up after {MAX_ATTEMPTS} attempts. Last error:\n{last_error}")


def resolve_image_slots(sender_ctx: dict, receiver_ctx: dict, draft: LLMDraft) -> list[ImageSlot]:
    """Builds the 3 fixed image slots, in schema order: logo_sender, logo_receiver, then one
    hero_contextual slot. Logo paths are resolved deterministically — the first image extracted
    from each side's uploaded documents — no vision model involved.

    Known gap: there is no contextual image *source* ingested anywhere in this pipeline (/upload
    only sees letterhead PDFs, not a stock-photo or generated-image feed), so hero_contextual's
    blob_path is left None rather than faked. A real build would add a stock-image search or a
    text-to-image call (e.g. DALL-E via the same Azure OpenAI resource) fed by contextual_image_caption.
    """
    sender_logo_path = sender_ctx["image_paths"][0] if sender_ctx["image_paths"] else None
    receiver_logo_path = receiver_ctx["image_paths"][0] if receiver_ctx["image_paths"] else None

    return [
        ImageSlot(slot_id="logo_sender", source_type="sender_logo",
                  blob_path=sender_logo_path, image_url=get_image_url(sender_logo_path),
                  caption=draft.sender_logo_caption),
        ImageSlot(slot_id="logo_receiver", source_type="receiver_logo",
                  blob_path=receiver_logo_path, image_url=get_image_url(receiver_logo_path),
                  caption=draft.receiver_logo_caption),
        ImageSlot(slot_id="hero_contextual", source_type="contextual",
                  blob_path=None, image_url=None, caption=draft.contextual_image_caption),
    ]


def assemble_article(request: GenerateRequest, draft: LLMDraft, image_slots: list[ImageSlot]) -> ArticleOutput:
    """Turns a validated LLMDraft into the full ArticleOutput /generate returns, by combining three
    sources: draft's fields (LLM-generated, copied as-is), request's fields (sender_id, receiver_id,
    theme — echoed from the caller), and freshly computed backend values (article_id, version,
    total_word_count, created_at).

    total_word_count's 300-600 target is a soft check — logged, not raised — since the per-field
    hard limits already bound it loosely (2-3 sections x <=150 words + headline/subheadline/CTA caps).
    """
    total_words = (
        word_count(draft.headline)
        + (word_count(draft.subheadline) if draft.subheadline else 0)
        + sum(word_count(s.text) for s in draft.body_sections)
        + word_count(draft.call_to_action)
    )
    if not (300 <= total_words <= 600):
        logger.warning("total_word_count=%d is outside the 300-600 soft target.", total_words)

    return ArticleOutput(
        article_id=uuid4(),
        sender_id=request.sender_id,
        receiver_id=request.receiver_id,
        version=get_next_version(request.sender_id, request.receiver_id),
        headline=draft.headline,
        subheadline=draft.subheadline,
        body_sections=draft.body_sections,
        call_to_action=draft.call_to_action,
        image_slots=image_slots,
        theme=request.theme,
        total_word_count=total_words,
        created_at=datetime.now(timezone.utc),
    )


def assemble_revised_article(original: ArticleOutput, revision: ArticleRevisionRequest) -> ArticleOutput:
    """Turns a customer hand-edit into a new ArticleOutput row, same append-only versioning
    pattern as /generate (decision-log.md — /revise entry): a fresh article_id and the next
    version under the *same* (sender_id, receiver_id) pair, never an in-place overwrite of
    `original`. That keeps a full audit trail ("v1 auto-generated, v2 manually tweaked") and means
    anything already holding a reference to `original.article_id` never sees its content change
    underneath it.

    Locked fields (sender_id, receiver_id, theme, and each image slot's blob_path/image_url) are
    always copied from `original`, never read from `revision` — ArticleRevisionRequest doesn't
    even have those fields, so this isn't a filter step, it's just "these values only ever come
    from one place." Only `caption` on each image slot is replaceable, and only for slot_ids
    present in revision.image_captions — everything else in image_slots is copied as-is.
    """
    revised_image_slots = [
        slot.model_copy(update={"caption": revision.image_captions[slot.slot_id]})
        if slot.slot_id in revision.image_captions
        else slot
        for slot in original.image_slots
    ]

    total_words = (
        word_count(revision.headline)
        + (word_count(revision.subheadline) if revision.subheadline else 0)
        + sum(word_count(s.text) for s in revision.body_sections)
        + word_count(revision.call_to_action)
    )
    if not (300 <= total_words <= 600):
        logger.warning("assemble_revised_article: total_word_count=%d is outside the 300-600 soft target.", total_words)

    return ArticleOutput(
        article_id=uuid4(),
        sender_id=original.sender_id,
        receiver_id=original.receiver_id,
        version=get_next_version(original.sender_id, original.receiver_id),
        headline=revision.headline,
        subheadline=revision.subheadline,
        body_sections=revision.body_sections,
        call_to_action=revision.call_to_action,
        image_slots=revised_image_slots,
        theme=original.theme,
        total_word_count=total_words,
        created_at=datetime.now(timezone.utc),
    )


# ---- POST /evaluate ----------------------------------------------------------
#
# LLM-as-judge only — no deterministic word-limit/schema re-check here. ArticleOutput's own fields
# were already Pydantic-validated at the moment the article was generated or revised; re-running the
# same constraint checks against the same stored object wouldn't catch anything new. What's missing
# after that structural validation is whether the content is actually *good* — grounded, relevant,
# coherent — which is exactly what has no Pydantic primitive and needs a judge call.

JUDGE_SYSTEM_PROMPT = """You are a strict quality reviewer for B2B marketing articles. You are given \
a SENDER CONTEXT block, a RECEIVER CONTEXT block, and an already-written ARTICLE that was supposed to \
bridge the two. Score the article honestly — do not default to high scores.

Score three dimensions, each 0.0-1.0:
1. groundedness: is every factual claim in the article (product features, capabilities, receiver \
details, statistics) actually present in SENDER CONTEXT or RECEIVER CONTEXT? Invented, generic, or \
unverifiable claims lower this score.
2. relevance: does the article speak to the RECEIVER's actual, specific situation (their real pain \
points, industry, business details from RECEIVER CONTEXT) rather than generic copy that could be sent \
to any company?
3. coherence: is the article well-structured, professional, on-tone outbound marketing copy — clear \
flow from headline through call to action, not choppy or repetitive?

Then write one feedback paragraph: specific, actionable guidance on what the next draft should change \
to score higher. If all three scores are already high, say so briefly and note only minor polish —
don't invent problems to fill space.
"""


def format_article_for_prompt(article: ArticleOutput) -> str:
    """Renders an already-generated ArticleOutput as plain text for the judge prompt — same idea as
    format_context_for_prompt, just for the article side instead of the sender/receiver side. Image
    slot captions are included (they're judge-relevant text); blob_path/image_url are not (not
    content the judge is scoring)."""
    lines = [f"HEADLINE: {article.headline}"]
    if article.subheadline:
        lines.append(f"SUBHEADLINE: {article.subheadline}")
    for section in article.body_sections:
        lines.append(f"\n[{section.heading}]\n{section.text}")
    lines.append(f"\nCALL TO ACTION: {article.call_to_action}")
    for slot in article.image_slots:
        lines.append(f"[image slot {slot.slot_id}] caption: {slot.caption}")
    return "\n".join(lines)


def build_judge_prompt(article: ArticleOutput, sender_ctx: dict, receiver_ctx: dict) -> str:
    """Labeled SENDER CONTEXT / RECEIVER CONTEXT / ARTICLE blocks — same labeling mechanism as
    build_user_prompt, so the judge (like the writer) never has to guess which side is which."""
    parts = [
        "=== SENDER CONTEXT (the company being marketed — what they sell) ===\n"
        + format_context_for_prompt(sender_ctx),
        "=== RECEIVER CONTEXT (the pitch target — who this article is for) ===\n"
        + format_context_for_prompt(receiver_ctx),
        "=== ARTICLE TO REVIEW ===\n" + format_article_for_prompt(article),
        "Score the article now.",
    ]
    return "\n\n".join(parts)


def judge_article(article: ArticleOutput, sender_ctx: dict, receiver_ctx: dict) -> JudgeDraft:
    """The one Azure OpenAI call for /evaluate — same structured-output pattern as generate_draft:
    response_format=JudgeDraft means the model is structurally incapable of writing article_id,
    version, overall_score, threshold, or passed. No retry loop here (unlike generate_draft_with_retry)
    — JudgeDraft's only constraints are the 0-1 Field bounds, which structured-output mode already
    enforces at the JSON-Schema level, so there's no post-hoc Pydantic failure mode to retry on."""
    client = get_client()
    user_prompt = build_judge_prompt(article, sender_ctx, receiver_ctx)
    completion = client.beta.chat.completions.parse(
        model=settings.azure_openai_deployment_name,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format=JudgeDraft,
    )
    choice = completion.choices[0]
    if choice.message.refusal:
        raise RuntimeError(f"Judge model refused: {choice.message.refusal}")
    return choice.message.parsed


def evaluate_article(article: ArticleOutput, sender_ctx: dict, receiver_ctx: dict) -> EvaluationResult:
    """Turns a JudgeDraft into the full EvaluationResult /evaluate returns. overall_score is the
    plain mean of the three sub-scores (not weighted) — all three are returned alongside it, not
    just the mean, so the fusion choice itself stays visible rather than hidden inside one number.
    Not persisted anywhere — see EvaluationResult's docstring."""
    judged = judge_article(article, sender_ctx, receiver_ctx)
    overall_score = round((judged.groundedness + judged.relevance + judged.coherence) / 3, 3)
    threshold = settings.evaluation_pass_threshold
    return EvaluationResult(
        article_id=article.article_id,
        version=article.version,
        groundedness=judged.groundedness,
        relevance=judged.relevance,
        coherence=judged.coherence,
        overall_score=overall_score,
        threshold=threshold,
        passed=overall_score >= threshold,
        feedback=judged.feedback,
        evaluated_at=datetime.now(timezone.utc),
    )

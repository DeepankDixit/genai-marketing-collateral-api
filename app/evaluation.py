"""Prompt construction, Azure OpenAI judge call, and result assembly for /evaluate.

Its own file rather than a section of generation.py — mirrors this codebase's existing
one-file-per-pipeline-stage pattern (parsing.py for /upload's extraction, generation.py for
/generate's writing, pdf_export.py for /articles/{id}/pdf's rendering): evaluate is its own stage,
not a sub-feature of generate, even though it shares a couple of small building blocks with it
(get_client, format_context_for_prompt — imported from generation.py below, not duplicated).

Deliberately knows nothing about FastAPI or HTTP, same as generation.py — takes an already-assembled
ArticleOutput plus sender/receiver context dicts in, returns an EvaluationResult out. Keeps
routers/evaluate.py thin and this file independently testable.

LLM-as-judge only — no deterministic word-limit/schema re-check here. ArticleOutput's own fields
were already Pydantic-validated at the moment the article was generated or revised; re-running the
same constraint checks against the same stored object wouldn't catch anything new. What's missing
after that structural validation is whether the content is actually *good* — grounded, relevant,
coherent — which is exactly what has no Pydantic primitive and needs a judge call.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.generation import format_context_for_prompt, get_client
from app.models import ArticleOutput, EvaluationResult, JudgeDraft

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
    generation.format_context_for_prompt, just for the article side instead of the sender/receiver
    side. Image slot captions are included (they're judge-relevant text); blob_path/image_url are
    not (not content the judge is scoring)."""
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
    generation.build_user_prompt, so the judge (like the writer) never has to guess which side is
    which."""
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
    """The one Azure OpenAI call for /evaluate — same structured-output pattern as
    generation.generate_draft: response_format=JudgeDraft means the model is structurally incapable
    of writing article_id, version, overall_score, threshold, or passed. No retry loop here (unlike
    generate_draft_with_retry) — JudgeDraft's only constraints are the 0-1 Field bounds, which
    structured-output mode already enforces at the JSON-Schema level, so there's no post-hoc
    Pydantic failure mode to retry on."""
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

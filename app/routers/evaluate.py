"""POST /evaluate — score an already-generated article: LLM-as-judge groundedness, relevance, and
coherence, no deterministic re-check (see models.EvaluationResult docstring for why).

Thin by design, same pattern as routers/generate.py and routers/articles.py: this module just looks
up the article and its original context, calls evaluation.evaluate_article(), and returns the
result. It writes nothing to storage — every call re-evaluates fresh (models.EvaluationResult
docstring).

Deliberately decoupled from /generate: this file never imports from routers/generate.py, and nothing
here calls POST /generate. The two endpoints are connected only by the shared feedback contract
(EvaluationResult.feedback and GenerateRequest.feedback are both plain str) — a caller is free to
take one straight into the other, but that round trip is the caller's decision, not this endpoint's.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.evaluation import evaluate_article
from app.models import EvaluateRequest, EvaluationResult
from app.storage import fetch_context, get_article

router = APIRouter()


@router.post("/evaluate", response_model=EvaluationResult)
def evaluate(request: EvaluateRequest) -> EvaluationResult:
    """Evaluates one already-generated or already-revised article by ID.

    JSON body is just {"article_id": ...} (EvaluateRequest) — same request-is-a-body-model
    convention as /generate and /revise, rather than a query param, for consistency across the API.

    Raises 404 if article_id doesn't match any stored article. Re-fetches the same sender/receiver
    context /generate originally used (storage.fetch_context, keyed off the article's own
    sender_id/receiver_id) so the judge scores groundedness against the real source material, not
    just the article in isolation.
    """
    article = get_article(request.article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"No article found with article_id={request.article_id}")

    sender_ctx = fetch_context(article.sender_id, article.receiver_id, role="sender")
    receiver_ctx = fetch_context(article.sender_id, article.receiver_id, role="receiver")
    return evaluate_article(article, sender_ctx, receiver_ctx)

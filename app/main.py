import logging

from azure.monitor.opentelemetry import configure_azure_monitor
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.config import settings

# None of the routes are defined in this file — their handlers live in
# app/routers/upload.py, app/routers/generate.py, app/routers/articles.py, and
# app/routers/evaluate.py, and these imports just pull in those router objects
# to register below.
#
# Why split it out instead of defining @app.post(...) right here, even though
# there are only a handful of endpoints total: each endpoint's real logic (looping over
# uploaded files, handling parse failures one-by-one for /upload; prompt
# construction + validation/retry for /generate) is substantial enough that
# inlining all of it here would make this file long and force a reader to
# scroll past someone else's endpoint to find the one they care about. Keeping
# main.py as a short "table of contents" — app setup, startup hook, health
# check, router registration — means anyone (including reviewers during a
# live walkthrough) can see the whole API surface at a glance, then jump into
# the relevant router for the actual logic.
from app.routers.articles import router as articles_router
from app.routers.evaluate import router as evaluate_router
from app.routers.generate import router as generate_router
from app.routers.upload import router as upload_router
from app.storage import ensure_resources_exist

logger = logging.getLogger(__name__)

# Wires this app into Application Insights: outbound calls to Azure OpenAI / Azure Storage
# (dependency latency, broken out separately from total request time) and an OpenTelemetry
# handler on the root logger so every existing logger.warning()/logger.error() call in this
# codebase (generation.py, storage.py, pdf_export.py, here) ships to App Insights too — no
# changes needed in those files. configure_azure_monitor() itself raises ValueError if the
# connection string is empty (confirmed by testing this locally — it does NOT fail open like
# the Azure Storage SDK calls below), so this guards the call explicitly — same fail-open
# pattern as ensure_resources_exist() uses for Azure Storage, applied by hand.
app_insights_enabled = bool(settings.applicationinsights_connection_string)
if app_insights_enabled:
    configure_azure_monitor(connection_string=settings.applicationinsights_connection_string)
else:
    logger.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set — skipping Application Insights setup.")

app = FastAPI(
    title="GenAI Marketing Collateral API",
    description="Ingests Sender/Receiver context PDFs and generates tailored, factually-grounded articles.",
    version="0.1.0",
)

# Explicitly instruments this app instance so incoming requests show up in App Insights'
# `requests` table (route, status, latency). Verified live (2026-08-01) that relying on
# configure_azure_monitor()'s implicit "patch the FastAPI class before it's instantiated"
# behavior alone was NOT enough on this deployment (Gunicorn, 2 workers): `dependencies`
# populated correctly (outbound Storage/OpenAI calls), but `requests` stayed empty even an
# hour later — proof the request-level span specifically wasn't being created. This explicit
# call is the standard, documented fix. No-ops harmlessly if App Insights isn't configured
# (creates spans against a default no-op tracer that go nowhere) but only bothering when it's
# actually wired up keeps local dev output clean, same reasoning as the guard above.
if app_insights_enabled:
    FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches anything that isn't already an HTTPException (our deliberate 404s/502s keep using
    FastAPI's own built-in handler, which is more specific and already returns JSON — this one
    never intercepts those). Without this, an unexpected exception (e.g. storage.get_article()
    hitting a Table Storage row that no longer matches the current Pydantic schema — see
    storage.list_articles' docstring) falls through to Starlette's default plain-text/HTML 500
    body. Every call in demo/index.html does `await res.json()` unconditionally, so a non-JSON
    error response doesn't surface as "here's what went wrong" — it surfaces as a confusing
    "Unexpected token 'I', 'Internal S'... is not valid JSON" network error instead, hiding the
    real problem. This returns a real {"detail": ...} body so the demo's existing
    formatErrorDetail() renders it like any other error, and logs the full traceback server-side
    (not sent to the client) for debugging."""
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": f"Internal server error: {exc}"})


@app.on_event("startup")
def on_startup() -> None:
    # Creates the blob containers / table (inside Azure Storage Account we created) if they don't exist yet. No-ops
    # gracefully if Azure Storage isn't configured (e.g. local dev before
    # the Storage Account exists) — see storage.ensure_resources_exist.
    ensure_resources_exist()


@app.get("/health")
def health_check() -> dict[str, str]:
    """Liveness check — also used to verify the GitHub -> Azure App Service deploy pipeline."""
    return {"status": "ok"}


app.include_router(upload_router)
app.include_router(generate_router)
app.include_router(articles_router)
app.include_router(evaluate_router)

# Static demo page (demo/index.html) — mounted last so it never shadows the API routes above.
# Served same-origin as the API (e.g. http://127.0.0.1:8000/demo/), so its fetch('/upload') and
# fetch('/generate') calls hit this same app with no CORS configuration needed. This is a demo
# aid, not a real frontend — no build step, no framework, just static HTML/JS.
app.mount("/demo", StaticFiles(directory="demo", html=True), name="demo")

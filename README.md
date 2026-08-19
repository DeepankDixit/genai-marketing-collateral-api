# GenAI Marketing Collateral API

Lightweight FastAPI prototype that ingests Sender/Receiver context PDFs (text, tables, images) and generates tailored, factually-grounded B2B marketing articles as structured JSON — ready to map into a layout template with word limits, image placeholders, and theme colors. See `CODE_MAP.md` and `docs/architecture/` for design decisions and the full request/response flow.

Built solo, end to end: requirements/design docs, a FastAPI backend with Pydantic-validated structured output, a real Azure cloud deployment with CI/CD, and structured observability — not just a local script.

## Cloud architecture & deployment

Designed and provisioned entirely on a personal Azure subscription (not shared/borrowed infra), as a deliberate demonstration of the cloud/software engineering side of this build, not just the AI side. Everything sat in one resource group, so the whole environment could be provisioned and torn down as a unit:

- **Azure OpenAI** — a Cognitive Services "OpenAI" resource with a `gpt-5-mini` model deployment (Global Standard) for generation, and the same resource re-used for an LLM-as-judge `/evaluate` endpoint. (Originally deployed against `gpt-4o-mini`; pivoted to `gpt-5-mini` mid-build when the 4o family was retired from Azure — a real platform-change-under-you moment, not a planning miss.)
- **Azure Storage Account** — Blob containers for raw uploaded PDFs and extracted images, plus Table Storage for parsed document metadata and generated article versions (chosen over Cosmos DB/Postgres for a lightweight, schema-flexible prototype — see `CODE_MAP.md`/`docs/architecture/`).
- **Azure App Service** (Linux, Python 3.11, Basic tier) — hosted the FastAPI app plus a static demo page (`demo/index.html`), served same-origin via a `StaticFiles` mount, no CORS setup needed.
- **Application Insights + Log Analytics** — structured JSON logging (request ID, endpoint, latency, status) wired through Python's `logging` module, giving queryable per-request and per-dependency (outbound Azure OpenAI/Storage call) telemetry, not just stdout logs.
- **CI/CD** — GitHub Actions building and deploying to App Service on every push to `main`, authenticated via a user-assigned managed identity with OIDC federated credentials (no stored cloud secrets in GitHub). Hit and diagnosed a real integration bug here: GitHub's OIDC tokens use an immutable-ID-qualified subject claim that didn't match the plain-name-format federated credential Azure's setup wizard auto-created, causing every login to fail with `AADSTS700213`. Root-caused from the raw Actions logs (not by guessing) and fixed by adding a second federated credential with the correct subject string — no code changes required.

**The app was deployed live on a public Azure URL for roughly three weeks** (built and deployed 2026-07-30, verified end-to-end multiple times — upload → generate → evaluate → revise → PDF export — then the resource group deliberately deleted on 2026-08-18 once its purpose was served, to avoid open-ended cost on a personal subscription). It isn't hosted right now; the steps below run the same app locally, and the same App Service + GitHub Actions setup would bring it back online.

## Prerequisites

- Python 3.11+
- VS Code with the [Postman extension](https://marketplace.visualstudio.com/items?itemName=Postman.postman-for-vscode) (for testing)

## Setup

```bash
cd genai-marketing-collateral-api
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in Azure OpenAI values once that resource exists
```

## Run

```bash
uvicorn app.main:app --reload
```

Server starts at `http://127.0.0.1:8000`. Leave this terminal running; use a second terminal or the Postman extension for testing. Stop with `Ctrl+C`.

## Demo page

A static, no-build-step demo lives at `http://127.0.0.1:8000/demo/` — the same page that was served at `/demo/` on the live Azure URL — via a FastAPI `StaticFiles` mount (`demo/index.html`), same origin as the API, no CORS setup needed. It exercises every endpoint the API exposes:

- **`GET /health`**: basic liveness check, not wired into the demo UI itself; see the Test section below for how to call it directly.
- **`POST /upload`**: sender_id / receiver_id / role / PDF file(s), renders the per-file parse results (status, pages/tables/images extracted, error if any).
- **`POST /generate`**: sender_id / receiver_id / theme colors, renders the returned JSON as an actual styled article (headline, subheadline, body sections, CTA) using the response's own theme colors live. Image slots (`logo_sender`, `logo_receiver`, `hero_contextual`) render as captioned placeholder boxes with the resolved `blob_path` shown as proof it came from real Table Storage data, deliberately not a live Blob image fetch (kept a lightweight lookup instead of adding image-serving infra for a prototype).
- **`POST /evaluate`**: a panel to the right of the rendered article, LLM-as-judge scores (groundedness/relevance/coherence, plus their mean) and a feedback paragraph for whichever article is currently displayed. "Regenerate with this feedback" re-submits `/generate` with that feedback and the same sender_id/receiver_id/theme, fully decoupled from `/evaluate` itself — deliberately no auto-chaining, so a human stays in the loop on whether to regenerate.
- **`GET /articles`**: a horizontal strip above the article panel listing every version generated so far for the current sender_id/receiver_id pair (both auto-generated and hand-edited), newest first. Selecting one loads it without regenerating.
- **`GET /articles/{article_id}/pdf`**: the "Export PDF" button on a displayed article; read-only download of whatever's already stored, never triggers a new generation.
- **`POST /articles/{article_id}/revise`**: the edit view lets you hand-edit the displayed article's JSON and submit it as a new version, stored the same append-only way `/generate` stores its own versions (v1, v2, v3...).

This is the direct, live proof of the core design goal: structured JSON mapping cleanly into a layout template with word limits, image placeholders, and theme colors.

## Architecture

`docs/architecture/` has four editable draw.io diagrams covering the system end to end:

- `solution-architecture.drawio` — overall pipeline (ingestion → generation → validation) and the cloud services behind each stage, plus the alternatives considered.
- `system-walkthrough.drawio` — endpoint-by-endpoint trace of exactly what code runs and what Azure resource it touches, for every route.
- `api-payload-schemas.drawio` — request/response JSON schema for every endpoint.
- `pydantic-model-validation.drawio` — how each Pydantic model enforces the contract (word limits, fixed image-slot counts, hex-color validation) before anything is returned to a caller.

## Test

**Option A (Swagger UI, fastest, no extra setup):** open `http://127.0.0.1:8000/docs` in a browser, expand `GET /health`, click **Try it out** then **Execute**.

**Option B (Postman extension in VS Code):**
1. Open the Postman icon in the VS Code sidebar, sign in or continue without an account.
2. Click **New**, then **HTTP Request**.
3. Method `GET`, URL `http://127.0.0.1:8000/health`.
4. Click **Send**.
5. Expect `200 OK` with body `{"status": "ok"}`.

As `/upload` and `/generate` are added, save each as a request in a shared Postman collection (exportable from the extension) so the whole flow can be re-run and demoed live.

## Deploy

Designed to run on any standard Python App Service / container host behind CI/CD — e.g. Azure App Service with GitHub Actions triggering an Oryx build + redeploy on every push to `main`. Not currently hosted; run locally per the steps above.

Startup command (works the same in App Service's Configuration > General settings, or any process manager):

```
gunicorn --bind=0.0.0.0 --timeout 600 app.main:app -k uvicorn.workers.UvicornWorker
```

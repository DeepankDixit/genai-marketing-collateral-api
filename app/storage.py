"""Azure Blob + Table Storage client.

All Azure Storage I/O lives here so parsing.py and the routers stay
storage-agnostic. Blob Storage holds raw PDF bytes and extracted images;
Table Storage holds the metadata record /generate reads later — a lookup by
(sender_id, receiver_id, document_id), never a query or search, which is why
Table Storage was chosen over Cosmos DB / Postgres (decision-log.md §16).
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

import mimetypes
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableServiceClient  # Table Storage SDK — structured, schema-less rows
from azure.storage.blob import (  # Blob Storage SDK — raw file/binary storage
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

from app.config import settings
from app.models import ArticleOutput, DocumentIngestResult, ExtractedImage, ParsedDocument, Role

# Both clients point at the SAME Storage Account — one Storage
# Account can serve multiple "services" (Blob, Table, Queue, File), each with
# its own client library because they store fundamentally different shapes
# of data:
#   - azure.storage.blob -> Blob service: arbitrary binary files (our raw
#     PDF bytes and extracted images). Addressed by a path-like blob name.
#   - azure.data.tables  -> Table service: structured key-value "rows"
#     (PartitionKey + RowKey + properties). Used for the parsed metadata
#     record /generate looks up by (sender_id, receiver_id, document_id) —
#     see decision-log.md §16 for why this over Cosmos DB/Postgres.
# Same connection string works for both — they're just different endpoints
# on the same account, hence two client classes but one settings value.

logger = logging.getLogger(__name__)

RAW_PDF_CONTAINER = "raw-pdfs"
IMAGE_CONTAINER = "extracted-images"
METADATA_TABLE = "documentmetadata"
ARTICLES_TABLE = "articles"  # sibling table, same account — one row per generated article

# Table Storage caps individual string properties at 64KB.
_MAX_TABLE_TEXT_LENGTH = 60_000


# These two build a fresh client on every call rather than one shared global
# client created once at import time. Deliberate simplicity trade-off: a
# global client would need careful init-order handling (it must not be
# constructed before .env is loaded) and shared-state/thread-safety
# reasoning. Rebuilding it per call is a few extra milliseconds — irrelevant
# at this prototype's traffic volume — in exchange for each function being a
# simple, self-contained, stateless call. Every function below (upload_raw_pdf,
# save_document_metadata, etc.) calls one of these to get a client, uses it,
# and is done — nothing to manage or clean up.


def _blob_service() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)


def _table_service() -> TableServiceClient:
    return TableServiceClient.from_connection_string(settings.azure_storage_connection_string)


def ensure_resources_exist() -> None:
    """Creates the containers/table if they don't already exist. Safe to call on every startup.

    No-ops with a warning if no connection string is configured yet, so local
    health-check testing still works before the Azure Storage account exists.
    """
    if not settings.azure_storage_connection_string:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set — skipping Azure Storage setup.")
        return

    blob_service = _blob_service()
    for container in (RAW_PDF_CONTAINER, IMAGE_CONTAINER):
        try:
            blob_service.create_container(container)
        except ResourceExistsError:
            pass

    table_service = _table_service()
    for table in (METADATA_TABLE, ARTICLES_TABLE):
        try:
            table_service.create_table(table)
        except ResourceExistsError:
            pass


def upload_raw_pdf(
    document_id: UUID, sender_id: str, receiver_id: str, role: Role, filename: str, content: bytes
) -> str:
    """Uploads the original PDF bytes and returns its blob path."""
    blob_path = f"{sender_id}/{receiver_id}/{role}/{document_id}/{filename}"
    client = _blob_service().get_blob_client(container=RAW_PDF_CONTAINER, blob=blob_path)
    client.upload_blob(content, overwrite=True)
    return blob_path


def upload_extracted_images(
    document_id: UUID, sender_id: str, receiver_id: str, role: Role, images: list[ExtractedImage]
) -> list[str]:
    """Uploads each extracted image and returns their blob paths.

    Sets Content-Type explicitly from the file extension (parsing._extract_images always embeds
    the real one, e.g. "...p1_1.png") — without it, the SDK defaults to application/octet-stream,
    which makes browsers force a download instead of rendering the image, whether navigated to
    directly or loaded via <img src=...>."""
    service = _blob_service()
    blob_paths = []
    for image in images:
        blob_path = f"{sender_id}/{receiver_id}/{role}/{document_id}/{image.filename}"
        content_type = mimetypes.guess_type(image.filename)[0] or "application/octet-stream"
        client = service.get_blob_client(container=IMAGE_CONTAINER, blob=blob_path)
        client.upload_blob(
            image.content, overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        blob_paths.append(blob_path)
    return blob_paths


def download_blob_bytes(blob_path: str, container: str = IMAGE_CONTAINER) -> bytes:
    """Downloads a blob's raw bytes into memory. Used by pdf_export.py to embed real logo/hero
    images in an exported PDF — reportlab needs actual image bytes, unlike the demo page's
    `<img src=SAS_URL>` where the browser does the fetching itself. Raises whatever the SDK raises
    (e.g. ResourceNotFoundError) if blob_path doesn't exist; callers catch this and fall back to a
    text-only placeholder rather than failing the whole PDF.
    """
    client = _blob_service().get_blob_client(container=container, blob=blob_path)
    return client.download_blob().readall()


def get_image_url(blob_path: str | None, container: str = IMAGE_CONTAINER, hours_valid: int = 6) -> str | None:
    """Builds a short-lived, read-only SAS URL for a blob in Blob Storage, so the demo page (or any
    browser) can render it directly via `<img src=...>` without the container being public and
    without any CORS configuration — <img> tags don't need CORS, only fetch()/XHR do. Returns None
    if blob_path is None (e.g. hero_contextual, which has no image source — see
    generation.resolve_image_slots), so callers don't need a separate null check.

    A fresh SAS token is minted on every /generate call rather than cached — this prototype's
    traffic doesn't justify caching, and a short expiry (default 6h, comfortably past one demo
    session) keeps the exposure window small without needing the container itself to be public."""
    if not blob_path:
        return None
    service = _blob_service()
    sas_token = generate_blob_sas(
        account_name=service.account_name,
        container_name=container,
        blob_name=blob_path,
        account_key=service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours_valid),
    )
    return f"{service.get_blob_client(container, blob_path).url}?{sas_token}"


def save_document_metadata(
    result: DocumentIngestResult,
    parsed: ParsedDocument,
    raw_pdf_blob_path: str,
    image_blob_paths: list[str],
) -> None:
    """Persists parsed content + blob references so /generate can look this document up by ID."""
    # Note what's NOT here: the actual image bytes. Images already live in
    # Blob Storage (uploaded by upload_extracted_images above) — this row
    # only stores their blob *paths* (plain strings) so /generate knows where
    # to fetch them from. Table Storage properties are meant for small
    # structured values, not binary blobs.
    entity = {
        "PartitionKey": f"{result.sender_id}__{result.receiver_id}",  # groups all docs for this sender+receiver pair
        "RowKey": str(result.document_id),  # unique within the partition — this one document
        "role": result.role,
        "filename": result.filename,
        "raw_pdf_blob_path": raw_pdf_blob_path,
        "text": parsed.text[:_MAX_TABLE_TEXT_LENGTH],
        "tables_json": json.dumps(parsed.tables),  # tables is a nested list -> JSON-encoded into one string property
        "image_blob_paths_json": json.dumps(image_blob_paths),  # list of blob path strings, not the images themselves
        "page_count": parsed.page_count,
        "uploaded_at": result.uploaded_at.isoformat(),
    }
    _table_service().get_table_client(METADATA_TABLE).upsert_entity(entity)


def fetch_context(sender_id: str, receiver_id: str, role: Role) -> dict:
    """Looks up every previously-uploaded document for one side of a pair and merges them into a
    single context bundle for /generate. Raises ValueError (not an empty result) if nothing was
    uploaded — fail loud, same policy as parsing.PdfParseError.

    Deduplicates by exact text match: /upload assigns a fresh document_id on every call regardless
    of content, so re-uploading the same PDF creates a second row with identical text. Left
    unfiltered, that duplicate would reach the LLM twice — wasted tokens and a risk of skewing the
    generated article toward whatever got accidentally repeated. A skipped duplicate is logged, not
    silently dropped.
    """
    partition_key = f"{sender_id}__{receiver_id}"
    entities = list(
        _table_service()
        .get_table_client(METADATA_TABLE)
        .query_entities(query_filter=f"PartitionKey eq '{partition_key}' and role eq '{role}'")
    )
    if not entities:
        raise ValueError(
            f"No {role!r} documents found for sender_id={sender_id!r} receiver_id={receiver_id!r}. "
            f"Upload context PDFs via POST /upload first."
        )

    texts, tables, image_paths, filenames = [], [], [], []
    seen_texts: set[str] = set()
    duplicates_skipped = 0
    for entity in entities:
        text = entity.get("text", "")
        if text in seen_texts:
            duplicates_skipped += 1
            logger.info(
                "fetch_context: skipping duplicate %s document %r (document_id=%s) — identical "
                "text already included from an earlier upload of this pair.",
                role, entity.get("filename"), entity.get("RowKey"),
            )
            continue
        seen_texts.add(text)
        texts.append(text)
        tables.extend(json.loads(entity.get("tables_json", "[]")))
        image_paths.extend(json.loads(entity.get("image_blob_paths_json", "[]")))
        filenames.append(entity.get("filename"))

    return {
        "role": role,
        "document_count": len(filenames),  # after dedup
        "duplicates_skipped": duplicates_skipped,
        "filenames": filenames,
        "text": "\n\n".join(texts),
        "tables": tables,
        "image_paths": image_paths,
    }


def get_next_version(sender_id: str, receiver_id: str) -> int:
    """Auto-increments ArticleOutput.version per (sender_id, receiver_id) pair, so regenerating for
    the same pair produces v2, v3, ... instead of overwriting v1. A read, performed before the new
    article exists — save_article() below is the separate write."""
    partition_key = f"{sender_id}__{receiver_id}"
    existing = list(
        _table_service()
        .get_table_client(ARTICLES_TABLE)
        .query_entities(query_filter=f"PartitionKey eq '{partition_key}'")
    )
    if not existing:
        return 1
    return max(e["version"] for e in existing) + 1


def list_articles(sender_id: str, receiver_id: str) -> list[ArticleOutput]:
    """Every article version generated so far for one (sender_id, receiver_id) pair — both
    /generate rows and /revise rows live in the same table with the same shape, so this returns
    both without distinguishing them (see limitations.md: created_at doesn't say which is which).
    Sorted newest-version-first. Backs GET /articles, which lets the demo browse and re-render a
    past version without calling /generate again. Returns an empty list (not a 404) if the pair
    has no articles yet — an empty result set isn't an error the way a missing upload is.

    Each row is deserialized individually, not in one list comprehension, and a row that fails is
    skipped (logged) rather than failing the whole call. Reason this matters in practice: tightening
    a Pydantic validator (e.g. BodySection.heading's word limit, added 2026-07-31) is a schema
    change, but it does nothing to rows already persisted under the older, looser schema — the next
    time model_validate_json() reads one of those older rows back, it now legitimately fails, and
    without per-row isolation that ValidationError would take down every OTHER version for that
    pair too, not just the one stale row. Skipping and logging keeps every still-valid version
    browsable; the one bad row simply won't appear until it's regenerated or hand-fixed in Table
    Storage."""
    partition_key = f"{sender_id}__{receiver_id}"
    entities = list(
        _table_service()
        .get_table_client(ARTICLES_TABLE)
        .query_entities(query_filter=f"PartitionKey eq '{partition_key}'")
    )
    articles: list[ArticleOutput] = []
    for entity in entities:
        try:
            articles.append(ArticleOutput.model_validate_json(entity["article_json"]))
        except Exception:
            logger.warning(
                "list_articles: skipping unreadable row for pair=%s__%s, RowKey=%s — likely "
                "written before a validator was tightened (see decision-log.md).",
                sender_id, receiver_id, entity.get("RowKey"), exc_info=True,
            )
    articles.sort(key=lambda a: a.version, reverse=True)
    return articles


def get_article(article_id: UUID) -> ArticleOutput | None:
    """Looks up one article by article_id (RowKey) alone, regardless of which sender/receiver
    partition it lives in. Cross-partition query (no PartitionKey in the filter) — a full-table
    scan, acceptable at this prototype's article-table scale (dozens of rows, not millions).

    Backs POST /articles/{article_id}/revise, the only caller that needs to resolve an article by
    ID alone rather than by (sender_id, receiver_id) — get_next_version and fetch_context both
    already know the pair and don't need this.
    """
    entities = list(
        _table_service()
        .get_table_client(ARTICLES_TABLE)
        .query_entities(query_filter=f"RowKey eq '{article_id}'")
    )
    if not entities:
        return None
    return ArticleOutput.model_validate_json(entities[0]["article_json"])


def save_article(article: ArticleOutput) -> None:
    """Persists one already-assembled ArticleOutput as a single row in the "articles" table. Same
    Table Storage justification as documentmetadata (decision-log.md §16): the only read pattern is
    an exact-ID lookup, never a search, so a simple key-value row is a direct fit.

    The full ArticleOutput is stored as one JSON string (article_json) rather than modeled as
    individual Table columns — simplest way to keep it fully retrievable without hand-mapping every
    nested field. upsert (not insert) is defensive: RowKey is a fresh UUID every time, so this
    normally creates a new row, but an accidental retry overwrites harmlessly instead of erroring.
    """
    entity = {
        "PartitionKey": f"{article.sender_id}__{article.receiver_id}",
        "RowKey": str(article.article_id),
        "version": article.version,
        "article_json": article.model_dump_json(),
        "created_at": article.created_at.isoformat(),
    }
    _table_service().get_table_client(ARTICLES_TABLE).upsert_entity(entity)

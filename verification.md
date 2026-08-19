# Verification — manual checks after calling each endpoint

Quick reference for confirming `/upload` and `/generate` actually did what their JSON
response claims, by cross-checking Azure Portal → your Storage account → **Storage
browser**. Useful for the demo itself (proving the response isn't hand-waved) and for
catching mistakes early (e.g. the wrong file attached to the right IDs).

## After `/upload`

1. **Postman response** — `results[0].status == "parsed"`, sane
   `pages_extracted`/`tables_extracted`/`images_extracted` counts, `error: null`.
2. **Blob containers → `raw-pdfs`** → drill into `{sender_id}/{receiver_id}/{role}/{document_id}/`
   → confirm the original PDF is there, filename matches.
3. **Blob containers → `extracted-images`** → same path → one blob per `images_extracted`
   count (usually the logo PNG).
4. **Tables → `documentmetadata`** → find the row by `RowKey = document_id` (or filter
   `PartitionKey eq 'sender_id__receiver_id'`) → sanity-check `role`, `filename`,
   `page_count`, and that `tables_json`/`image_blob_paths_json` aren't empty if the source
   PDF actually had a table/image.

## After `/generate`

1. **Postman response** — `article_id` present, `version` incremented as expected,
   `body_sections` actually reference facts from both sides (not generic copy),
   `image_slots` has exactly 3 entries in fixed order, `theme` echoes what was sent.
2. **Tables → `articles`** (sibling to `documentmetadata`) → find the row by
   `RowKey = article_id` → confirm `version` matches and `article_json` holds the full
   payload.
3. **Cross-check images resolve**: take a `blob_path` from `image_slots` in the response,
   go to **Blob containers → `extracted-images`**, navigate to that exact path, open the
   blob's Overview panel → confirm `CREATION TIME`/`SIZE` look right (non-zero, timestamp
   matches when you called `/generate`). **Don't** paste the blob's raw URL into a browser
   tab expecting it to load — containers are private by design (`PublicAccessNotPermitted`
   is expected, not a bug); use the Portal's **Download** button (authenticated) or
   **Generate SAS** (time-limited signed URL) if you need to actually view the image.
4. One thing to *not* find, as a sanity check: no new blobs in `raw-pdfs` from this call —
   `/generate` only reads what `/upload` already wrote, it should never create a raw PDF
   blob.

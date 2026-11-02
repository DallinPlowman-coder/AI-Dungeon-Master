# AI Dungeon Master

A solo roleplaying app with a future AI Dungeon Master. This first milestone is **The Archive**: upload a book and turn it into a persistent, searchable knowledge source for retrieval-augmented generation (RAG).

## Run locally

Use Python 3.12–3.14 (tested with 3.12). No Node.js, Docker, GPU, paid API, or API key is required.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m dungeon_master
```

Open **http://127.0.0.1:8000**. Upload a book to **Rules** or **Campaigns**, or try a sample in either area. Wait for **Ready for retrieval**, then use **Find a passage** to verify the index. **View passages** shows the extracted text and its source location.

## Two source collections

- **Rules:** rulebooks, combat and spell references, character options, and house rules.
- **Campaigns:** adventure books, settings, locations, NPCs, and campaign notes.

Each collection has its own upload area, book list, indexing progress, and retrieval scope. Search **Rules only**, **Campaigns only**, or **Rules + Campaigns**; optionally narrow further to one book. Both semantic and keyword retrieval apply the selected collection before ranking. Returned passages and context identify the collection alongside the source citation.

Duplicate detection is per collection: the same file can be indexed in both collections, while repeated uploads within one collection reuse the existing book. Use **Move to Rules / Campaigns** to change a finished book's collection without regenerating its embeddings. If the destination already contains that file, the move is rejected and both existing entries are preserved.

Existing libraries upgrade automatically. Earlier uploads are placed under Campaigns and keep their original IDs, passages, vectors, and source files. Move any rulebooks into Rules as needed. The pre-upgrade database is backed up to `data/library.before-collections.sqlite3` before the transactional migration.

The first upload downloads the English `BAAI/bge-small-en-v1.5` embedding model, approximately 70 MB, from Hugging Face. Subsequent processing uses the cached model. Book contents and queries are processed locally. You can download the model ahead of time:

```sh
python -m dungeon_master --prepare-model
```

The tested dependency versions, including test tools, are recorded in `requirements.lock.txt`. To reproduce them in a clean Python 3.12 environment:

```sh
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
```

## What “ragified” means here

1. **Store:** validate the upload, save its Rules/Campaigns collection, and store the original file with a generated filename. SHA-256 deduplicates identical uploads within the selected collection, including files renamed by the user.
2. **Extract:** read PDF pages, EPUB sections in spine order, or UTF-8 / BOM-marked UTF-16 text. PDF citations use physical, one-based PDF page numbers, which may differ from printed page numbers.
3. **Chunk:** normalize text and split it into overlapping passages, bounded by the source page or section. Each passage fits within 360 tokens of the actual embedding tokenizer. Paragraph and sentence boundaries are preferred; short chunks use proportionally smaller overlaps.
4. **Embed:** generate normalized, 384-dimensional vectors locally with FastEmbed / ONNX on the CPU.
5. **Index:** atomically commit passages, vectors, source references, model identity, and a full-text index to SQLite. A book only becomes searchable once the entire index is ready.
6. **Retrieve:** combine cosine similarity and SQLite FTS5 keyword search with reciprocal rank fusion. Return excerpts, stable book IDs, chunk IDs, source labels, and a citation-labeled context string for the future DM.

The app includes background processing, persistent status/progress, warnings for PDF pages without readable text, retries/reindexing, passage previews with pagination, per-book search, and deletion of the original file and its index. Interrupted jobs resume from the beginning on server restart.

**This milestone supplies retrieval, not an AI-generated story or chat.** It prepares the knowledge the eventual DM will consume. Multiplayer, accounts, campaigns, and generation will be added separately.

## Supported books and limits

| Format | Extraction | Source reference |
| --- | --- | --- |
| PDF | Embedded text, page by page | Physical PDF page |
| EPUB | DRM-free HTML chapters in the book's reading order | Section number and title |
| TXT / Markdown | UTF-8 or BOM-marked UTF-16 | Text source and passage ID |

- Default upload limit: 100 MB per file. Extracted text is limited to 20 million characters, 5,000 PDF pages, and 30,000 passages per book. EPUB expansion is also bounded.
- **Scanned/image-only books need OCR before upload.** OCR, illustrations, map understanding, and table reconstruction are outside this milestone. Mixed PDFs retain readable pages and warn about skipped pages. PDF reading order depends on the book's text layer, so inspect passages from complex layouts.
- This is a local, single-user application. Run one server worker per data directory; a process lock prevents competing ingestion workers. Authentication and shared deployments need a later service layer.
- Vectors are searched in batches with an exact scan. This suits a personal library; a large/shared library can move to pgvector or a vector service behind the retrieval layer.
- Retrieval returns the closest excerpts even when they do not answer the query. Scores are ranking signals, not confidence or proof. Check the cited source.
- Future generation should treat retrieved book text as untrusted reference material and keep DM instructions separate from source excerpts.

## Storage and settings

All runtime data is under `data/` by default and is ignored by Git. Back up the entire directory to retain books and indexes.

```text
data/
  uploads/           # Original books, stored by generated book ID
  models/            # Cached local embedding model
  library.sqlite3    # Books, job state, passages, vectors, FTS index
  server.lock        # Single-worker process lock
```

Optional shell settings (see `.env.example`; `.env` is not automatically loaded):

```sh
export DM_DATA_DIR="/path/to/my/library"
export DM_MAX_UPLOAD_MB=100
```

`DM_EMBEDDING_MODEL` defaults to `BAAI/bge-small-en-v1.5`. The pipeline is tuned for this English model and its 512-token window; replacement models must support at least that window. Changing the model marks existing books as needing reindexing, and incompatible indexes are excluded from retrieval.

Use `python -m dungeon_master --port 8001` to change the port.

## API

Interactive documentation: **http://127.0.0.1:8000/docs**.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Read limits, format support, and model name |
| POST | `/api/books` | Multipart fields `file` and `collection` (`rules` or `campaign`); 202 queued, 200 duplicate |
| GET | `/api/books?collection=rules` | List books in one collection; omit filter for all books |
| GET | `/api/books/{id}` | Poll a book's progress / failure details |
| PATCH | `/api/books/{id}` | Move a finished book using `{"collection":"rules"}` or `{"collection":"campaign"}` |
| GET | `/api/books/{id}/chunks?offset=0&limit=10` | Preview source passages |
| POST | `/api/books/{id}/reindex` | Retry a failed book or rebuild its index |
| DELETE | `/api/books/{id}` | Remove a finished/failed book and its index |
| POST | `/api/retrieve` | Search ready books and return source context |

```sh
curl -F 'file=@/path/to/rulebook.pdf' -F 'collection=rules' http://127.0.0.1:8000/api/books

curl -F 'file=@/path/to/adventure.pdf' -F 'collection=campaign' http://127.0.0.1:8000/api/books

curl http://127.0.0.1:8000/api/books

curl http://127.0.0.1:8000/api/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"query":"How can someone cure the sick traveler?","collection":"campaign","limit":5}'
```

Set `"collection":"rules"` or `"collection":"campaign"` to scope retrieval; omit it or use `null` to search both. Add `"book_id":"<UUID returned by upload>"` to scope retrieval to one book. If both filters are supplied, the book must belong to the selected collection. Uploads without a collection default to `campaign` for compatibility with earlier clients. Each result includes `citation`, `book_id`, `chunk_id`, `collection`, `title`, `source_label`, `text`, `score`, and cosine `similarity`. The top-level `context` contains those excerpts labeled `[S1]`, `[S2]`, and so on. These are reference passages to pass to a future generation model; the service does not generate an answer.

## Verification

```sh
python -m pytest -q

# Real CPU embeddings, PDF ingestion, and paraphrased retrieval.
# Uses ./data/models; downloads the model if it is not cached.
DM_TEST_MODEL=1 python -m pytest tests/test_real_model.py -q
```

The offline tests cover PDF / EPUB extraction and ordering, unreadable/encrypted PDFs, token limits and source boundaries, per-collection duplicate detection, isolated semantic and keyword retrieval, combined search, collection moves and collisions, legacy database migration, input and size validation, atomic reindexing, restart recovery, changed model handling, and deletion of file/vector/full-text data. The opt-in test verifies real 384-dimensional embeddings and finds a medicine passage using a paraphrased query within the selected collection.

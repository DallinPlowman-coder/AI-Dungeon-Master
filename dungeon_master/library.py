import heapq
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .chunking import chunk_sections
from .config import PIPELINE_VERSION, Settings
from .database import ACTIVE_STATUSES, Database
from .embeddings import LocalEmbeddings
from .extraction import BookError, extract_book
from .rulesets import RuleSetStore


logger = logging.getLogger(__name__)


class Library:
    def __init__(self, settings: Settings, embeddings=None):
        self.settings = settings
        self.db = Database(settings.data_dir / "library.sqlite3")
        self.embeddings = embeddings or LocalEmbeddings(settings)
        self.rule_sets = RuleSetStore(self.db, self.embeddings.name)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="book-ingestion")
        self._pending = set()
        self._lock = threading.Lock()

    def start(self):
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.db.initialize()
        # The durable queue survives a stopped server. A single worker owns ingestion.
        for book in self.db.list_books():
            if book["status"] in ACTIVE_STATUSES:
                self.db.update_book(book["id"], status="queued", progress=0,
                                    stage_detail="Resuming after server restart")
                self.submit(book["id"])

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def upload_path(self, book):
        return self.settings.uploads_dir / f"{book['id']}.{book['format']}"

    def describe(self, book):
        if book is None:
            return None
        return {**book, "needs_reindex": book["status"] == "ready" and (
            book["embedding_model"] != self.embeddings.name or book["pipeline_version"] != PIPELINE_VERSION
        )}

    def submit(self, book_id):
        with self._lock:
            if book_id in self._pending:
                return
            self._pending.add(book_id)
        self.executor.submit(self._ingest, book_id)

    def reindex(self, book_id):
        # Coordinate the status transition with worker completion, including the
        # short interval between committing the index and releasing the worker.
        with self._lock:
            if book_id in self._pending or not self.db.queue_again(book_id):
                return False
            self._pending.add(book_id)
            self.executor.submit(self._ingest, book_id)
            return True

    def _ingest(self, book_id):
        try:
            book = self.db.get_book(book_id)
            if book is None:
                return
            self.db.update_book(book_id, status="extracting", progress=5, error=None,
                                stage_detail="Reading book text", warnings=[])

            def extraction_progress(done, total):
                if done == total or done % 10 == 0:
                    self.db.update_book(book_id, progress=5 + int(20 * done / max(total, 1)),
                                        stage_detail=f"Reading page / section {done} of {total}")

            extracted = extract_book(self.upload_path(book), book["format"],
                                     self.settings.max_characters, extraction_progress)
            self.db.update_book(book_id, status="chunking", progress=28,
                                page_count=extracted.page_count,
                                character_count=sum(len(s.text) for s in extracted.sections),
                                warnings=extracted.warnings,
                                stage_detail="Loading local model (downloads on first use)")
            self.embeddings.prepare()
            self.db.update_book(book_id, stage_detail="Splitting text into overlapping passages")
            chunks = chunk_sections(extracted.sections, self.embeddings.count_tokens,
                                    self.settings.chunk_tokens, self.settings.overlap_characters,
                                    self.settings.max_chunks)
            if not chunks:
                raise BookError("This book has no readable passages to index.")
            self.db.update_book(book_id, status="embedding", progress=35,
                                chunk_count=len(chunks), stage_detail=f"Embedding 0 of {len(chunks)} passages")
            vectors = []
            for start in range(0, len(chunks), 24):
                batch = chunks[start:start + 24]
                embedded = self.embeddings.passages([chunk.text for chunk in batch])
                if len(embedded) != len(batch):
                    raise RuntimeError("Embedding count did not match passage count")
                vectors.extend(embedded)
                self.db.update_book(book_id, progress=35 + int(60 * len(vectors) / len(chunks)),
                                    stage_detail=f"Embedding {len(vectors)} of {len(chunks)} passages")
            self.db.save_index(book_id, chunks, vectors, self.embeddings.name)
        except Exception as exc:
            logger.exception("Book ingestion failed: %s", book_id)
            message = str(exc) if isinstance(exc, BookError) else (
                "Processing failed. The first upload needs internet access to download the embedding "
                "model. Check the server log, then retry this book."
            )
            self.db.update_book(book_id, status="failed", stage_detail="Processing failed", error=message)
        finally:
            with self._lock:
                self._pending.discard(book_id)

    def retrieve(self, query: str, book_id=None, limit=5, collection=None, book_ids=None):
        ready = [book for book in self.db.list_books(collection) if book["status"] == "ready"
                 and not self.describe(book)["needs_reindex"] and (book_id is None or book["id"] == book_id)
                 and (book_ids is None or book["id"] in book_ids)]
        if not ready:
            return {"query": query, "matches": [], "context": ""}
        vector = self.embeddings.query(query)
        best = []
        for batch in self.db.vector_batches(self.embeddings.name, book_id, collection, book_ids):
            matrix = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in batch])
            scores = matrix @ vector
            for row, score in zip(batch, scores, strict=True):
                item = (float(score), row["id"], dict(row))
                if len(best) < 40:
                    heapq.heappush(best, item)
                elif item[:2] > best[0][:2]:
                    heapq.heapreplace(best, item)
        semantic = sorted(best, reverse=True)
        terms = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))[:32]
        expression = " OR ".join('"' + term + '"' for term in terms)
        lexical = self.db.lexical_search(expression, self.embeddings.name, book_id,
                                         collection=collection, book_ids=book_ids) if terms else []
        # Reciprocal rank fusion keeps exact game terms useful alongside semantic matches.
        candidates = {}
        for rank, (similarity, chunk_id, row) in enumerate(semantic, 1):
            candidates[chunk_id] = {"row": row, "score": 1 / (60 + rank), "similarity": similarity}
        for rank, row in enumerate(lexical, 1):
            candidate = candidates.setdefault(row["id"], {"row": row, "score": 0, "similarity": None})
            candidate["score"] += 1 / (60 + rank)
        ordered = sorted(candidates.values(), key=lambda entry: entry["score"], reverse=True)
        matches = []
        for candidate in ordered:
            row = candidate["row"]
            # Avoid sending nearly identical overlapping windows to the future DM.
            words = set(row["text"].lower().split())
            if any(row["book_id"] == previous["book_id"] and
                   len(words & set(previous["text"].lower().split())) / max(1, len(words)) > .88
                   for previous in matches):
                continue
            matches.append({
                "citation": f"S{len(matches) + 1}", "chunk_id": row["id"],
                "book_id": row["book_id"], "title": row["title"],
                "collection": row["collection"],
                "source_label": row["source_label"], "text": row["text"],
                "score": round(candidate["score"], 6),
                "similarity": round(candidate["similarity"], 4) if candidate["similarity"] is not None else None,
            })
            if len(matches) == limit:
                break
        context = "\n\n".join(f"[{m['citation']}] {m['title']} — "
                              f"{m['collection'].capitalize()} · {m['source_label']}\n{m['text']}" for m in matches)
        return {"query": query, "matches": matches, "context": context}

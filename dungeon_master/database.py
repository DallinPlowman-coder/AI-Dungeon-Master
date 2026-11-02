import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import PIPELINE_VERSION


ACTIVE_STATUSES = ("queued", "extracting", "chunking", "embedding")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    format TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    stage_detail TEXT NOT NULL DEFAULT 'Waiting to be processed',
                    page_count INTEGER NOT NULL DEFAULT 0,
                    character_count INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    embedding_model TEXT,
                    pipeline_version TEXT,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    UNIQUE(book_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS chunks_book ON chunks(book_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id', tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text)
                        VALUES ('delete', old.id, old.text);
                END;
            """)

    @staticmethod
    def book(row):
        if row is None:
            return None
        result = dict(row)
        result["warnings"] = json.loads(result["warnings"])
        return result

    def list_books(self):
        with self.connect() as db:
            return [self.book(row) for row in db.execute(
                "SELECT * FROM books ORDER BY created_at DESC"
            )]

    def get_book(self, book_id):
        with self.connect() as db:
            return self.book(db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone())

    def by_hash(self, digest):
        with self.connect() as db:
            return self.book(db.execute("SELECT * FROM books WHERE sha256 = ?", (digest,)).fetchone())

    def add_book(self, *, book_id, title, filename, file_format, size_bytes, digest):
        stamp = now()
        with self.connect() as db:
            db.execute("""INSERT INTO books
                (id, title, filename, format, size_bytes, sha256, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (book_id, title, filename, file_format, size_bytes, digest, stamp, stamp))

    def update_book(self, book_id, **values):
        allowed = {"status", "progress", "stage_detail", "page_count", "character_count",
                   "chunk_count", "embedding_model", "pipeline_version", "warnings", "error"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid book update")
        if "warnings" in values:
            values["warnings"] = json.dumps(values["warnings"])
        values["updated_at"] = now()
        with self.connect() as db:
            db.execute(f"UPDATE books SET {', '.join(key + ' = ?' for key in values)} WHERE id = ?",
                       (*values.values(), book_id))

    def queue_again(self, book_id):
        with self.connect() as db:
            cursor = db.execute("""UPDATE books SET status = 'queued', progress = 0,
                error = NULL, stage_detail = 'Waiting to be processed', updated_at = ?
                WHERE id = ? AND status IN ('ready', 'failed')""", (now(), book_id))
            return cursor.rowcount == 1

    def save_index(self, book_id, chunks, vectors, model):
        # Readers see the complete new index or no ready index, never a partial one.
        with self.connect() as db:
            db.execute("DELETE FROM chunks WHERE book_id = ?", (book_id,))
            db.executemany("""INSERT INTO chunks
                (book_id, ordinal, text, source_label, token_count, embedding)
                VALUES (?, ?, ?, ?, ?, ?)""", [
                    (book_id, i, chunk.text, chunk.source_label, chunk.token_count, vector.tobytes())
                    for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ])
            db.execute("""UPDATE books SET status = 'ready', progress = 100,
                stage_detail = 'Ready for retrieval', chunk_count = ?, embedding_model = ?,
                pipeline_version = ?, error = NULL, updated_at = ? WHERE id = ?""",
                (len(chunks), model, PIPELINE_VERSION, now(), book_id))

    def delete_book(self, book_id):
        with self.connect() as db:
            return db.execute("DELETE FROM books WHERE id = ? AND status IN ('ready', 'failed')",
                              (book_id,)).rowcount == 1

    def preview_chunks(self, book_id, offset, limit):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT id, ordinal, text, source_label,
                token_count FROM chunks WHERE book_id = ? ORDER BY ordinal LIMIT ? OFFSET ?""",
                (book_id, limit, offset))]

    def vector_batches(self, model, book_id=None):
        with self.connect() as db:
            cursor = db.execute("""SELECT c.*, b.title FROM chunks c JOIN books b ON b.id = c.book_id
                WHERE b.status = 'ready' AND b.embedding_model = ? AND b.pipeline_version = ?
                AND (? IS NULL OR b.id = ?)""", (model, PIPELINE_VERSION, book_id, book_id))
            while rows := cursor.fetchmany(256):
                yield rows

    def lexical_search(self, expression, model, book_id=None, limit=40):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT c.*, b.title FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid JOIN books b ON b.id = c.book_id
                WHERE chunks_fts MATCH ? AND b.status = 'ready'
                AND b.embedding_model = ? AND b.pipeline_version = ?
                AND (? IS NULL OR b.id = ?) ORDER BY bm25(chunks_fts) LIMIT ?""",
                (expression, model, PIPELINE_VERSION, book_id, book_id, limit))]

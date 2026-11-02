import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import PIPELINE_VERSION


ACTIVE_STATUSES = ("queued", "extracting", "chunking", "embedding")
BOOKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        format TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
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
        updated_at TEXT NOT NULL,
        collection TEXT NOT NULL DEFAULT 'campaign' CHECK(collection IN ('rules', 'campaign')),
        UNIQUE(sha256, collection)
    );
"""


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
            db.executescript(BOOKS_SCHEMA + """
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
            if "collection" not in {row["name"] for row in db.execute("PRAGMA table_info(books)")}:
                self._migrate_collections(db)
            db.execute("CREATE INDEX IF NOT EXISTS books_collection ON books(collection, status)")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rule_sets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_set_books (
                    rule_set_id TEXT NOT NULL REFERENCES rule_sets(id) ON DELETE CASCADE,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, PRIMARY KEY(rule_set_id, book_id)
                );
                CREATE INDEX IF NOT EXISTS rule_set_members_book ON rule_set_books(book_id);
            """)

    def _migrate_collections(self, db):
        # Keep passages, vectors, IDs, and FTS rows intact while replacing the old
        # global hash constraint with one duplicate constraint per collection.
        backup_path = self.path.with_name("library.before-collections.sqlite3")
        if not backup_path.exists():
            with sqlite3.connect(backup_path) as backup:
                db.backup(backup)
        new_schema = BOOKS_SCHEMA.replace("IF NOT EXISTS books", "books_new", 1)
        columns = ", ".join('"' + row["name"].replace('"', '""') + '"'
                            for row in db.execute("PRAGMA table_info(books)"))
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(new_schema)
            db.execute(f"INSERT INTO books_new ({columns}) SELECT {columns} FROM books")
            db.execute("DROP TABLE books")
            db.execute("ALTER TABLE books_new RENAME TO books")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Book collection migration did not preserve foreign keys")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def book(row):
        if row is None:
            return None
        result = dict(row)
        result["warnings"] = json.loads(result["warnings"])
        return result

    def list_books(self, collection=None):
        with self.connect() as db:
            return [self.book(row) for row in db.execute(
                "SELECT * FROM books WHERE (? IS NULL OR collection = ?) ORDER BY created_at DESC",
                (collection, collection),
            )]

    def get_book(self, book_id):
        with self.connect() as db:
            return self.book(db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone())

    def by_hash(self, digest, collection="campaign"):
        with self.connect() as db:
            return self.book(db.execute("SELECT * FROM books WHERE sha256 = ? AND collection = ?",
                                       (digest, collection)).fetchone())

    def add_book(self, *, book_id, title, filename, file_format, size_bytes, digest, collection="campaign"):
        stamp = now()
        with self.connect() as db:
            db.execute("""INSERT INTO books
                (id, title, filename, format, size_bytes, sha256, created_at, updated_at, collection)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (book_id, title, filename, file_format, size_bytes, digest, stamp, stamp, collection))

    def move_book(self, book_id, collection):
        with self.connect() as db:
            moved = db.execute("""UPDATE books SET collection = ?, updated_at = ?
                WHERE id = ? AND status IN ('ready', 'failed')""",
                (collection, now(), book_id)).rowcount == 1
            if moved:
                self._touch_rule_sets(db, book_id)
            return moved

    @staticmethod
    def _touch_rule_sets(db, book_id):
        # Older libraries can be indexed before their first schema upgrade.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rule_sets'").fetchone() is None:
            return
        db.execute("""UPDATE rule_sets SET revision = revision + 1, updated_at = ?
            WHERE id IN (SELECT rule_set_id FROM rule_set_books WHERE book_id = ?)""", (now(), book_id))

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
            if {"status", "embedding_model", "pipeline_version"} & values.keys():
                self._touch_rule_sets(db, book_id)

    def queue_again(self, book_id):
        with self.connect() as db:
            cursor = db.execute("""UPDATE books SET status = 'queued', progress = 0,
                error = NULL, stage_detail = 'Waiting to be processed', updated_at = ?
                WHERE id = ? AND status IN ('ready', 'failed')""", (now(), book_id))
            if cursor.rowcount == 1:
                self._touch_rule_sets(db, book_id)
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
            self._touch_rule_sets(db, book_id)

    def delete_book(self, book_id):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM books WHERE id = ? AND status IN ('ready', 'failed')", (book_id,)).fetchone():
                self._touch_rule_sets(db, book_id)
            return db.execute("DELETE FROM books WHERE id = ? AND status IN ('ready', 'failed')",
                              (book_id,)).rowcount == 1

    def preview_chunks(self, book_id, offset, limit):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT id, ordinal, text, source_label,
                token_count FROM chunks WHERE book_id = ? ORDER BY ordinal LIMIT ? OFFSET ?""",
                (book_id, limit, offset))]

    def vector_batches(self, model, book_id=None, collection=None, book_ids=None):
        scope = "" if book_ids is None else " AND b.id IN (" + ",".join("?" for _ in book_ids) + ")" if book_ids else " AND 0"
        with self.connect() as db:
            cursor = db.execute("""SELECT c.*, b.title, b.collection FROM chunks c JOIN books b ON b.id = c.book_id
                WHERE b.status = 'ready' AND b.embedding_model = ? AND b.pipeline_version = ?
                AND (? IS NULL OR b.id = ?) AND (? IS NULL OR b.collection = ?)""" + scope,
                (model, PIPELINE_VERSION, book_id, book_id, collection, collection, *(book_ids or [])))
            while rows := cursor.fetchmany(256):
                yield rows

    def lexical_search(self, expression, model, book_id=None, limit=40, collection=None, book_ids=None):
        scope = "" if book_ids is None else " AND b.id IN (" + ",".join("?" for _ in book_ids) + ")" if book_ids else " AND 0"
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT c.*, b.title, b.collection FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid JOIN books b ON b.id = c.book_id
                WHERE chunks_fts MATCH ? AND b.status = 'ready'
                AND b.embedding_model = ? AND b.pipeline_version = ?
                AND (? IS NULL OR b.id = ?) AND (? IS NULL OR b.collection = ?)""" + scope + " ORDER BY bm25(chunks_fts) LIMIT ?",
                (expression, model, PIPELINE_VERSION, book_id, book_id, collection, collection, *(book_ids or []), limit))]

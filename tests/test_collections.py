import sqlite3
import time
import uuid

from fastapi.testclient import TestClient

from dungeon_master.config import Settings
from dungeon_master.database import Database
from dungeon_master.main import create_app
from conftest import TestEmbeddings, wait_for_book


def upload(client, collection, text=b"A lantern protects travelers from shadow creatures."):
    return client.post("/api/books", data={"collection":collection}, files={"file":("book.txt", text)})


def test_upload_and_deduplication_are_scoped_to_each_collection(client):
    ids = {}
    for collection in ("rules", "campaign"):
        response = upload(client, collection)
        assert response.status_code == 202
        book = wait_for_book(client, response.json()["book"]["id"])
        assert book["collection"] == collection and book["status"] == "ready"
        ids[collection] = book["id"]
        duplicate = upload(client, collection)
        assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
        assert duplicate.json()["book"]["id"] == book["id"]
        listing = client.get("/api/books", params={"collection":collection}).json()["books"]
        assert len(listing) == 1 and listing[0]["id"] == book["id"]
    assert ids["rules"] != ids["campaign"]
    assert len(client.get("/api/books").json()["books"]) == 2
    # Removing one copy leaves the other collection and its index intact.
    assert client.delete(f"/api/books/{ids['rules']}").status_code == 200
    result = client.post("/api/retrieve", json={"query":"lantern", "collection":"campaign"}).json()
    assert result["matches"][0]["book_id"] == ids["campaign"]


def test_both_semantic_and_keyword_search_stay_inside_the_selected_collection(client):
    for collection in ("rules", "campaign"):
        book_id = upload(client, collection).json()["book"]["id"]
        assert wait_for_book(client, book_id)["status"] == "ready"
    for collection in ("rules", "campaign"):
        for query in ("lantern", "unrelated paraphrase"):
            response = client.post("/api/retrieve", json={"query":query, "collection":collection})
            assert response.status_code == 200
            result = response.json()
            assert result["matches"] and all(match["collection"] == collection for match in result["matches"])
            assert f"{collection.capitalize()} ·" in result["context"]
    combined = client.post("/api/retrieve", json={"query":"lantern"}).json()
    assert {match["collection"] for match in combined["matches"]} == {"rules", "campaign"}


def test_empty_rules_collection_never_falls_back_to_campaigns(client):
    book_id = upload(client, "campaign").json()["book"]["id"]
    wait_for_book(client, book_id)
    result = client.post("/api/retrieve", json={"query":"lantern", "collection":"rules"}).json()
    assert result["matches"] == [] and result["context"] == ""
    assert client.post("/api/retrieve", json={"query":"lantern", "collection":"rules", "book_id":book_id}).status_code == 400


def test_move_retains_the_existing_index_and_changes_retrieval_scope(client):
    book_id = upload(client, "campaign").json()["book"]["id"]
    wait_for_book(client, book_id)
    original = client.get(f"/api/books/{book_id}/chunks").json()
    response = client.patch(f"/api/books/{book_id}", json={"collection":"rules"})
    assert response.status_code == 200
    assert response.json()["collection"] == "rules" and response.json()["status"] == "ready"
    assert client.get(f"/api/books/{book_id}/chunks").json() == original
    assert client.get("/api/books?collection=campaign").json()["books"] == []
    assert client.post("/api/retrieve", json={"query":"lantern", "collection":"campaign"}).json()["matches"] == []
    matches = client.post("/api/retrieve", json={"query":"lantern", "collection":"rules"}).json()["matches"]
    assert matches[0]["book_id"] == book_id


def test_move_collision_preserves_both_books(client):
    ids = [upload(client, collection).json()["book"]["id"] for collection in ("rules", "campaign")]
    for book_id in ids:
        wait_for_book(client, book_id)
    assert client.patch(f"/api/books/{ids[1]}", json={"collection":"rules"}).status_code == 409
    assert len(client.get("/api/books").json()["books"]) == 2
    assert client.get(f"/api/books/{ids[1]}").json()["collection"] == "campaign"


def test_invalid_collection_and_cross_origin_move_are_rejected(client):
    assert upload(client, "invalid").status_code == 422
    assert client.get("/api/books?collection=invalid").status_code == 422
    assert client.post("/api/retrieve", json={"query":"lantern", "collection":"invalid"}).status_code == 422
    book_id = upload(client, "campaign").json()["book"]["id"]
    wait_for_book(client, book_id)
    assert client.patch(f"/api/books/{book_id}", json={"collection":"invalid"}).status_code == 422
    assert client.patch(f"/api/books/{book_id}", json={"collection":"rules"}, headers={"Origin":"https://example.com"}).status_code == 403


def test_processing_books_cannot_be_moved(tmp_path):
    class SlowEmbeddings(TestEmbeddings):
        def prepare(self):
            time.sleep(.3)
    with TestClient(create_app(Settings(data_dir=tmp_path), SlowEmbeddings())) as client:
        book_id = upload(client, "campaign").json()["book"]["id"]
        assert client.patch(f"/api/books/{book_id}", json={"collection":"rules"}).status_code == 409


def test_legacy_library_migration_preserves_vectors_fts_and_foreign_keys(tmp_path):
    path = tmp_path / "library.sqlite3"
    # Build the schema of the shipped pre-collection release.
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE books (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                format TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
                stage_detail TEXT NOT NULL DEFAULT '', page_count INTEGER NOT NULL DEFAULT 0,
                character_count INTEGER NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL DEFAULT 0,
                embedding_model TEXT, pipeline_version TEXT, warnings TEXT NOT NULL DEFAULT '[]',
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, text TEXT NOT NULL, source_label TEXT NOT NULL,
                token_count INTEGER NOT NULL, embedding BLOB NOT NULL, UNIQUE(book_id, ordinal)
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize='unicode61');
            CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
        """)
    database = Database(path)
    book_id = str(uuid.uuid4())
    with database.connect() as connection:
        connection.execute("""INSERT INTO books
            (id, title, filename, format, size_bytes, sha256, created_at, updated_at)
            VALUES (?, 'Original adventure', 'source.txt', 'txt', 40, 'same-hash', '2026-11-02', '2026-11-02')""", (book_id,))
    from dungeon_master.chunking import Chunk
    vectors = TestEmbeddings().passages(["A lantern guards the portal."])
    database.save_index(book_id, [Chunk("A lantern guards the portal.", "Page 4", 8)], vectors, TestEmbeddings.name)
    database.initialize()
    assert (tmp_path / "library.before-collections.sqlite3").is_file()
    assert database.get_book(book_id)["collection"] == "campaign"
    assert database.preview_chunks(book_id, 0, 10)[0]["source_label"] == "Page 4"
    rows = list(database.vector_batches(TestEmbeddings.name, collection="campaign"))
    assert rows[0][0]["embedding"] == vectors[0].tobytes()
    assert database.lexical_search('"lantern"', TestEmbeddings.name, collection="campaign")
    database.add_book(book_id=str(uuid.uuid4()), title="Rules copy", filename="rules.txt", file_format="txt", size_bytes=40, digest="same-hash", collection="rules")
    database.initialize()  # Restart is idempotent.
    assert database.delete_book(book_id)
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'lantern'").fetchone()[0] == 0

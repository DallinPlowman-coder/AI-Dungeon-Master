import time

from fastapi.testclient import TestClient

from dungeon_master.config import Settings
from dungeon_master.database import Database
from dungeon_master.main import create_app
from conftest import TestEmbeddings, make_epub, make_pdf, wait_for_book


def upload(client, name="lore.txt", text=b"The silver dragon guards a secret portal beneath the city."):
    return client.post("/api/books", files={"file": (name, text)})


def test_complete_upload_retrieval_duplicate_and_delete(client):
    response = upload(client)
    assert response.status_code == 202
    book_id = response.json()["book"]["id"]
    book = wait_for_book(client, book_id)
    assert book["status"] == "ready" and book["progress"] == 100
    assert book["chunk_count"] > 0 and book["embedding_model"] == TestEmbeddings.name
    assert upload(client).json()["duplicate"] is True
    assert len(client.get("/api/books").json()["books"]) == 1
    preview = client.get(f"/api/books/{book_id}/chunks").json()
    assert "silver dragon" in preview["chunks"][0]["text"]
    results = client.post("/api/retrieve", json={"query": "secret portal", "book_id": book_id}).json()
    assert results["matches"][0]["book_id"] == book_id
    assert "[S1] lore" in results["context"]
    assert client.delete(f"/api/books/{book_id}").status_code == 200
    assert not list(client.app.state.library.settings.uploads_dir.iterdir())
    assert client.post("/api/retrieve", json={"query": "dragon"}).json()["matches"] == []
    with client.app.state.library.db.connect() as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'dragon'").fetchone()[0] == 0


def test_pdf_and_epub_uploads(client):
    for name, content, reference in [("rules.pdf", make_pdf(["A shield protects the knight."]), "Page 1"),
                                     ("world.epub", make_epub(), "Section 1")]:
        book_id = upload(client, name, content).json()["book"]["id"]
        assert wait_for_book(client, book_id)["status"] == "ready"
        chunks = client.get(f"/api/books/{book_id}/chunks").json()["chunks"]
        assert chunks[0]["source_label"].startswith(reference)


def test_failed_book_can_be_retried_and_deleted(client):
    book_id = upload(client, "broken.pdf", b"not a PDF").json()["book"]["id"]
    book = wait_for_book(client, book_id)
    assert book["status"] == "failed" and "could not be read" in book["error"]
    assert client.post(f"/api/books/{book_id}/reindex").status_code == 202
    assert wait_for_book(client, book_id)["status"] == "failed"
    assert client.delete(f"/api/books/{book_id}").status_code == 200


def test_validation_and_safe_filenames(client):
    assert upload(client, "book.exe").status_code == 415
    assert upload(client, "empty.txt", b"").status_code == 400
    assert client.post("/api/retrieve", json={"query": " "}).status_code == 422
    assert client.post("/api/retrieve", json={"query": "**"}).status_code == 422
    assert client.post("/api/retrieve", json={"query": "dragon", "limit": 100}).status_code == 422
    assert client.get("/api/books/not-an-id").status_code == 422
    response = upload(client, "../../../../../escaped.txt")
    book_id = response.json()["book"]["id"]
    assert wait_for_book(client, book_id)["filename"] == "escaped.txt"
    assert client.app.state.library.upload_path(response.json()["book"]).name == f"{book_id}.txt"


def test_file_size_limit(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_mb=1), TestEmbeddings())) as client:
        assert upload(client, text=b"x" * (1024 * 1024 + 1)).status_code == 413
        assert upload(client, text=b"x" * (3 * 1024 * 1024)).status_code == 413
        assert not list((tmp_path / "uploads").iterdir())


def test_streamed_upload_size_limit_without_content_length(tmp_path):
    boundary = "dungeon-test-boundary"
    def body():
        yield (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="large.txt"\r\n'
               'Content-Type: text/plain\r\n\r\n').encode()
        for _ in range(4):
            yield b"x" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_mb=1), TestEmbeddings())) as client:
        response = client.post("/api/books", content=body(), headers={"Content-Type":f"multipart/form-data; boundary={boundary}"})
        assert response.status_code == 413
        assert not list((tmp_path / "uploads").iterdir())


def test_search_filter_and_reindex_are_isolated(client):
    first = upload(client, "one.txt", b"The amber shrine contains a secret jewel.").json()["book"]["id"]
    second = upload(client, "two.txt", b"The copper inn serves warm bread.").json()["book"]["id"]
    wait_for_book(client, first)
    wait_for_book(client, second)
    matches = client.post("/api/retrieve", json={"query":"secret jewel", "book_id":second}).json()["matches"]
    assert all(match["book_id"] == second for match in matches)
    assert client.post(f"/api/books/{first}/reindex").status_code == 202
    assert wait_for_book(client, first)["status"] == "ready"
    assert client.get(f"/api/books/{first}/chunks").json()["total"] == 1


def test_queue_survives_restart_and_index_persists(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings, TestEmbeddings())
    with TestClient(app) as client:
        book_id = upload(client).json()["book"]["id"]
        assert wait_for_book(client, book_id)["status"] == "ready"
    db = Database(tmp_path / "library.sqlite3")
    db.update_book(book_id, status="embedding", progress=57)
    with TestClient(create_app(settings, TestEmbeddings())) as client:
        assert wait_for_book(client, book_id)["status"] == "ready"
        assert client.post("/api/retrieve", json={"query":"secret portal"}).json()["matches"]
    with TestClient(create_app(settings, TestEmbeddings())) as client:
        assert client.get(f"/api/books/{book_id}").json()["status"] == "ready"


def test_reindex_required_when_model_changes(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings, TestEmbeddings())) as client:
        book_id = upload(client).json()["book"]["id"]
        wait_for_book(client, book_id)
    replacement = TestEmbeddings()
    replacement.name = "another-model"
    with TestClient(create_app(settings, replacement)) as client:
        assert client.get(f"/api/books/{book_id}").json()["needs_reindex"]
        assert client.post("/api/retrieve", json={"query":"dragon", "book_id":book_id}).status_code == 409
        assert client.post("/api/retrieve", json={"query":"dragon"}).json()["matches"] == []
        assert client.post(f"/api/books/{book_id}/reindex").status_code == 202
        assert wait_for_book(client, book_id)["embedding_model"] == replacement.name


def test_processing_actions_are_guarded(tmp_path):
    class SlowEmbeddings(TestEmbeddings):
        def prepare(self):
            time.sleep(.3)
    with TestClient(create_app(Settings(data_dir=tmp_path), SlowEmbeddings())) as client:
        book_id = upload(client).json()["book"]["id"]
        assert client.delete(f"/api/books/{book_id}").status_code == 409
        assert client.post(f"/api/books/{book_id}/reindex").status_code == 409
        assert client.get(f"/api/books/{book_id}/chunks").status_code == 409


def test_static_app_and_cross_origin_guard(client):
    response = client.get("/")
    assert response.status_code == 200 and "The Archive" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.post("/api/books", headers={"Origin":"https://example.com"}).status_code == 403


def test_chunk_pagination_and_fts_syntax(client):
    text = " ".join(f"entry{i} a dragon guards the portal." for i in range(500))
    book_id = upload(client, text=text.encode()).json()["book"]["id"]
    wait_for_book(client, book_id)
    data = client.get(f"/api/books/{book_id}/chunks?offset=3&limit=2").json()
    assert len(data["chunks"]) == 2 and data["chunks"][0]["ordinal"] == 3
    response = client.post("/api/retrieve", json={"query":'dragon OR " * (NEAR) --'})
    assert response.status_code == 200 and response.json()["matches"]

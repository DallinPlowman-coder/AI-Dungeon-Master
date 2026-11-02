import hashlib
import logging
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .database import ACTIVE_STATUSES
from .library import Library
from .campaign_store import CampaignStore
from .campaign_api import campaign_routes
from .planning import CampaignPlanning
from .alignment import AlignmentTracking


STATIC = Path(__file__).parent / "static"
FORMATS = {".pdf": "pdf", ".epub": "epub", ".txt": "txt", ".md": "md"}
logger = logging.getLogger(__name__)
BookCollection = Literal["rules", "campaign"]


class UploadLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/books":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length and (not length.isdigit() or int(length) > self.max_bytes):
            response = JSONResponse({"detail": "Upload exceeds the maximum file size."}, status_code=413)
            return await response(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                raise StarletteHTTPException(413, "Upload exceeds the maximum file size.")
            return message

        await self.app(scope, limited_receive, send)


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    book_id: uuid.UUID | None = None
    collection: BookCollection | None = None
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        value = value.strip()
        if len(value) < 2 or not any(char.isalnum() for char in value):
            raise ValueError("Enter a question or some search terms.")
        return value


class MoveBookRequest(BaseModel):
    collection: BookCollection


def create_app(settings: Settings | None = None, embeddings=None, planning_provider=None):
    settings = settings or Settings.from_env()
    library = Library(settings, embeddings)
    campaigns = CampaignStore(library.db)
    planning = CampaignPlanning(library, campaigns, planning_provider)
    alignment = AlignmentTracking(campaigns.alignment, planning.provider)

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        process_lock = FileLock(settings.data_dir / "server.lock")
        try:
            process_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("This library is already open. Run one server worker per data directory.") from exc
        try:
            library.start()
            campaigns.initialize()
            planning.start()
            alignment.start()
            yield
        finally:
            await run_in_threadpool(alignment.close)
            await run_in_threadpool(planning.close)
            await run_in_threadpool(library.close)
            process_lock.release()

    app = FastAPI(title="AI Dungeon Master · Book Library", version="0.1.0", lifespan=lifespan)
    app.state.library = library
    app.state.campaigns = campaigns
    app.state.planning = planning
    app.state.alignment = alignment
    app.include_router(campaign_routes(library, campaigns, planning, alignment))
    app.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_bytes + 1024 * 1024)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def local_app_headers(request: Request, call_next):
        # Browser writes must originate from this app. Local API clients need no Origin header.
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/campaigns"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        ) if not request.url.path.startswith(("/docs", "/redoc")) else "frame-ancestors 'none'"
        return response

    def find_book(book_id):
        book = library.db.get_book(str(book_id))
        if book is None:
            raise HTTPException(404, "Book not found.")
        return book

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/campaigns", include_in_schema=False)
    def campaign_workspace():
        return FileResponse(STATIC / "campaigns.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "embedding_model": library.embeddings.name,
                "max_upload_mb": settings.max_upload_mb, "formats": list(FORMATS), "storage": "local"}

    @app.get("/api/books")
    def list_books(collection: BookCollection | None = None):
        return {"books": [library.describe(book) for book in library.db.list_books(collection)]}

    @app.get("/api/books/{book_id}")
    def get_book(book_id: uuid.UUID):
        return library.describe(find_book(book_id))

    @app.post("/api/books", status_code=202)
    async def upload_book(file: UploadFile, collection: BookCollection = Form("campaign")):
        filename = re.sub(r"[\x00-\x1f\x7f]", "", (file.filename or "book").replace("\\", "/").split("/")[-1])[:240]
        suffix = Path(filename).suffix.lower()
        if suffix not in FORMATS:
            await file.close()
            raise HTTPException(415, "Choose a PDF, EPUB, TXT, or Markdown file.")
        book_id = str(uuid.uuid4())
        final_path = settings.uploads_dir / f"{book_id}.{FORMATS[suffix]}"
        temporary = final_path.with_suffix(".part")
        size, digest = 0, hashlib.sha256()
        try:
            with temporary.open("wb") as destination:
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, f"Maximum book size is {settings.max_upload_mb} MB.")
                    digest.update(block)
                    destination.write(block)
            if not size:
                raise HTTPException(400, "This file is empty.")
            if existing := library.db.by_hash(digest.hexdigest(), collection):
                return JSONResponse({"book": library.describe(existing), "duplicate": True}, status_code=200)
            temporary.replace(final_path)
            try:
                library.db.add_book(book_id=book_id, title=Path(filename).stem,
                                    filename=filename, file_format=FORMATS[suffix], size_bytes=size,
                                    digest=digest.hexdigest(), collection=collection)
            except sqlite3.IntegrityError:
                final_path.unlink(missing_ok=True)
                existing = library.db.by_hash(digest.hexdigest(), collection)
                if existing is None:
                    raise
                return JSONResponse({"book": library.describe(existing), "duplicate": True}, status_code=200)
            library.submit(book_id)
            return {"book": library.describe(library.db.get_book(book_id)), "duplicate": False}
        except Exception:
            if library.db.get_book(book_id) is None:
                final_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    @app.patch("/api/books/{book_id}")
    def move_book(book_id: uuid.UUID, payload: MoveBookRequest):
        find_book(book_id)
        try:
            if not library.db.move_book(str(book_id), payload.collection):
                raise HTTPException(409, "Wait for this book to finish processing before moving it.")
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "This book already exists in the destination collection.") from exc
        return library.describe(find_book(book_id))

    @app.post("/api/books/{book_id}/reindex", status_code=202)
    def reindex_book(book_id: uuid.UUID):
        find_book(book_id)
        if not library.reindex(str(book_id)):
            raise HTTPException(409, "This book is already processing.")
        return library.describe(find_book(book_id))

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: uuid.UUID):
        book = find_book(book_id)
        if book["status"] in ACTIVE_STATUSES or not library.db.delete_book(str(book_id)):
            raise HTTPException(409, "Wait for this book to finish processing before removing it.")
        library.upload_path(book).unlink(missing_ok=True)
        return {"deleted": True}

    @app.get("/api/books/{book_id}/chunks")
    def preview_chunks(book_id: uuid.UUID, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50)):
        book = find_book(book_id)
        if book["status"] != "ready":
            raise HTTPException(409, "This book is not ready yet.")
        return {"chunks": library.db.preview_chunks(str(book_id), offset, limit),
                "total": book["chunk_count"], "offset": offset}

    @app.post("/api/retrieve")
    def retrieve(payload: RetrievalRequest):
        if payload.book_id:
            book = find_book(payload.book_id)
            if payload.collection and book["collection"] != payload.collection:
                raise HTTPException(400, "The selected book is not in the selected collection.")
            if book["status"] != "ready" or library.describe(book)["needs_reindex"]:
                raise HTTPException(409, "This book must finish indexing before it can be searched.")
        try:
            return library.retrieve(payload.query, str(payload.book_id) if payload.book_id else None,
                                    payload.limit, payload.collection)
        except Exception as exc:
            logger.exception("Retrieval failed")
            raise HTTPException(503, "Search is temporarily unavailable. Check the server log and try again.") from exc

    return app


app = create_app()

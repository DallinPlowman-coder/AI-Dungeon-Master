"""Reusable groups of indexed rulebooks; grouping never duplicates their indexes."""
import sqlite3
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import PIPELINE_VERSION
from .database import now


class RuleSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Annotated[str, Field(min_length=1, max_length=160)]
    book_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_books(self):
        if len(set(self.book_ids)) != len(self.book_ids):
            raise ValueError("Choose each book once")
        return self


class RuleSetEdit(RuleSetInput):
    expected_revision: int = Field(ge=1, strict=True)


class RuleSetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_set_id: uuid.UUID | None
    expected_rule_set_id: uuid.UUID | None


class RuleSetError(ValueError):
    pass


class RuleSetConflict(RuleSetError):
    pass


class RuleSetStore:
    def __init__(self, database, embedding_model):
        self.db, self.embedding_model = database, embedding_model

    def get(self, rule_set_id, db=None):
        if db is None:
            with self.db.connect() as connection:
                return self.get(rule_set_id, connection)
        row = db.execute("SELECT * FROM rule_sets WHERE id = ?", (rule_set_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["books"] = []
        for book in db.execute("""SELECT b.id, b.title, b.collection, b.status, b.embedding_model, b.pipeline_version
                FROM rule_set_books m JOIN books b ON b.id = m.book_id
                WHERE m.rule_set_id = ? ORDER BY m.position""", (rule_set_id,)):
            available = (book["collection"] == "rules" and book["status"] == "ready"
                         and book["embedding_model"] == self.embedding_model and book["pipeline_version"] == PIPELINE_VERSION)
            result["books"].append({key:book[key] for key in ("id", "title", "collection", "status")} | {"available":available})
        result["book_ids"] = [book["id"] for book in result["books"]]
        result["ready"] = bool(result["books"]) and all(book["available"] for book in result["books"])
        return result

    def list(self):
        with self.db.connect() as db:
            return [self.get(row[0], db) for row in db.execute("SELECT id FROM rule_sets ORDER BY name COLLATE NOCASE, id")]

    def require_ready(self, rule_set_id, db):
        group = self.get(rule_set_id, db)
        if group is None:
            raise RuleSetError("That rulebook set no longer exists")
        if not group["ready"]:
            raise RuleSetConflict("This set needs attention. Keep at least one book, and finish indexing all its books in Rules.")
        return group

    def save(self, payload, rule_set_id=None):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if rule_set_id is not None:
                existing = self.get(rule_set_id, db)
                if existing is None:
                    raise RuleSetError("That rulebook set no longer exists")
                if existing["revision"] != payload.expected_revision:
                    raise RuleSetConflict("This set changed. Reopen its editor before saving your changes.")
            for book_id in payload.book_ids:
                book = db.execute("SELECT * FROM books WHERE id = ?", (str(book_id),)).fetchone()
                if (book is None or book["collection"] != "rules" or book["status"] != "ready"
                        or book["embedding_model"] != self.embedding_model or book["pipeline_version"] != PIPELINE_VERSION):
                    raise RuleSetError("Choose fully indexed books from Rules. Adventures belong in Campaign Books.")
            stamp = now()
            try:
                if rule_set_id is None:
                    rule_set_id = str(uuid.uuid4())
                    db.execute("INSERT INTO rule_sets VALUES (?, ?, 1, ?, ?)", (rule_set_id, payload.name, stamp, stamp))
                else:
                    db.execute("UPDATE rule_sets SET name = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                               (payload.name, stamp, rule_set_id))
                    db.execute("DELETE FROM rule_set_books WHERE rule_set_id = ?", (rule_set_id,))
                db.executemany("INSERT INTO rule_set_books VALUES (?, ?, ?)",
                               [(rule_set_id, str(book_id), position) for position, book_id in enumerate(payload.book_ids)])
            except sqlite3.IntegrityError as exc:
                raise RuleSetConflict("A rulebook set with that name already exists. Choose a different name.") from exc
            return self.get(rule_set_id, db)

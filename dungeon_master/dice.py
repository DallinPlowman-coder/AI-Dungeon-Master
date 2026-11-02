"""Durable player-triggered d20 requests and authoritative, single-use results."""
import json
import secrets
import uuid

from .database import now
from .records import StoryEvent


class RollConflict(ValueError):
    pass


class RollNotFound(LookupError):
    pass


class CampaignDice:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def initialize(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_rolls (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                request_key TEXT NOT NULL, request_json TEXT NOT NULL,
                source_event_id TEXT REFERENCES campaign_events(id) ON DELETE SET NULL,
                result_json TEXT, created_at TEXT NOT NULL,
                UNIQUE(campaign_id, request_key)
            );
            CREATE INDEX IF NOT EXISTS rolls_campaign ON campaign_rolls(campaign_id, created_at);
        """)

    @staticmethod
    def decode(row, dm=False):
        result = {"id":row["id"], **json.loads(row["request_json"]), "created_at":row["created_at"],
                  "status":"rolled" if row["result_json"] else "pending", "result":json.loads(row["result_json"] or "null")}
        if dm:
            result["source_event_id"] = row["source_event_id"]
        return result

    def queue(self, db, campaign_id, request, source_event_id=None):
        data = request.model_dump(mode="json", exclude={"idempotency_key"})
        existing = db.execute("SELECT * FROM campaign_rolls WHERE campaign_id = ? AND request_key = ?",
                              (campaign_id, str(request.idempotency_key))).fetchone()
        if existing:
            if json.loads(existing["request_json"]) != data:
                raise RollConflict("This roll request key already belongs to a different check.")
            return self.decode(existing), True
        roll_id = str(uuid.uuid4())
        db.execute("""INSERT INTO campaign_rolls
            (id, campaign_id, request_key, request_json, source_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (roll_id, campaign_id, str(request.idempotency_key), json.dumps(data), source_event_id, now()))
        return self.decode(db.execute("SELECT * FROM campaign_rolls WHERE id = ?", (roll_id,)).fetchone()), False

    def request(self, campaign_id, request):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            roll, duplicate = self.queue(db, campaign_id, request)
            if not duplicate:
                db.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now(), campaign_id))
            return {"roll":roll, "duplicate":duplicate}

    def list(self, campaign_id, dm=False):
        with self.db.connect() as db:
            return [self.decode(row, dm) for row in db.execute(
                "SELECT * FROM campaign_rolls WHERE campaign_id = ? ORDER BY created_at, id", (campaign_id,))]

    def roll(self, campaign_id, roll_id):
        with self.db.connect() as db:
            # Random generation and event recording share the lock: retries and concurrent clicks
            # return the original result, even when a response is lost after committing it.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM campaign_rolls WHERE campaign_id = ? AND id = ?", (campaign_id, roll_id)).fetchone()
            if row is None:
                raise RollNotFound("Roll request not found in this campaign.")
            if row["result_json"]:
                return {"roll":self.decode(row), "duplicate":True}
            request = json.loads(row["request_json"])
            value = secrets.randbelow(20) + 1
            total, stamp = value + request["modifier"], now()
            event_id = str(uuid.uuid4())
            context = {}
            for event in db.execute("SELECT event_json FROM campaign_events WHERE campaign_id = ? ORDER BY sequence DESC", (campaign_id,)):
                data = json.loads(event["event_json"])
                if data["player_visible"]:
                    context = data
                    break
            calculation = f"d20 {value}" + (f" {request['modifier']:+d}" if request["modifier"] else "") + f" = {total}"
            event = StoryEvent(title=f"Roll: {request['label']}", summary=f"{request['label']}: {calculation}.",
                kind="roll", details=request["reason"], session=context.get("session", 1), world_time=context.get("world_time", ""))
            db.execute("INSERT INTO campaign_events (id, campaign_id, idempotency_key, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                       (event_id, campaign_id, f"d20:{roll_id}", event.model_dump_json(), stamp))
            result = {"value":value, "modifier":request["modifier"], "total":total, "event_id":event_id, "rolled_at":stamp}
            db.execute("UPDATE campaign_rolls SET result_json = ? WHERE id = ?", (json.dumps(result), roll_id))
            db.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (stamp, campaign_id))
            return {"roll":self.decode(db.execute("SELECT * FROM campaign_rolls WHERE id = ?", (roll_id,)).fetchone()), "duplicate":False}

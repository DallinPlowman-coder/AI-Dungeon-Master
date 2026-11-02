import json
import uuid

from .database import now
from .records import CharacterSheet, NpcMemory, TurnUpdate


class CampaignStore:
    def __init__(self, database):
        self.db = database

    def initialize(self):
        with self.db.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    source_book_id TEXT REFERENCES books(id) ON DELETE SET NULL,
                    source_title TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                    character_json TEXT NOT NULL, character_revision INTEGER NOT NULL DEFAULT 0, plan_json TEXT,
                    plan_status TEXT NOT NULL DEFAULT 'queued', plan_progress INTEGER NOT NULL DEFAULT 0,
                    plan_detail TEXT NOT NULL DEFAULT 'Waiting to read the adventure', plan_error TEXT,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL, event_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(campaign_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS npc_memories (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    memory_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_campaign ON campaign_events(campaign_id, sequence);
                CREATE INDEX IF NOT EXISTS npcs_campaign ON npc_memories(campaign_id);
                CREATE TABLE IF NOT EXISTS campaign_readings (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    batch_hash TEXT NOT NULL, digest_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id, batch_hash)
                );
                CREATE TABLE IF NOT EXISTS campaign_plan_history (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(campaign_id, version)
                );
            """)
            if "character_revision" not in {row["name"] for row in db.execute("PRAGMA table_info(campaigns)")}:
                db.execute("ALTER TABLE campaigns ADD COLUMN character_revision INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["character"] = json.loads(result.pop("character_json"))
        result["plan"] = json.loads(result.pop("plan_json") or "null")
        return result

    def list(self):
        with self.db.connect() as db:
            return [{key:row[key] for key in ("id", "name", "source_title", "plan_status", "plan_progress", "created_at", "updated_at")}
                    for row in db.execute("SELECT * FROM campaigns ORDER BY created_at DESC")]

    def get(self, campaign_id):
        with self.db.connect() as db:
            return self.decode(db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone())

    def create(self, name, book):
        campaign_id, stamp = str(uuid.uuid4()), now()
        with self.db.connect() as db:
            db.execute("""INSERT INTO campaigns (id, name, source_book_id, source_title, source_sha256,
                character_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (campaign_id, name, book["id"], book["title"], book["sha256"],
                 CharacterSheet().model_dump_json(), stamp, stamp))
        return campaign_id

    def character(self, campaign_id, sheet, expected_revision=None):
        with self.db.connect() as db:
            return db.execute("""UPDATE campaigns SET character_json = ?, character_revision = character_revision + 1,
                updated_at = ? WHERE id = ? AND (? IS NULL OR character_revision = ?)""",
                (sheet.model_dump_json(), now(), campaign_id, expected_revision, expected_revision)).rowcount == 1

    def events(self, campaign_id, player=False):
        with self.db.connect() as db:
            records = [{**json.loads(row["event_json"]), "id":row["id"], "sequence":row["sequence"],
                        "created_at":row["created_at"]} for row in db.execute(
                "SELECT * FROM campaign_events WHERE campaign_id = ? ORDER BY sequence", (campaign_id,))]
        return [record for record in records if record["player_visible"] or not player]

    def npcs(self, campaign_id, player=False):
        with self.db.connect() as db:
            records = [json.loads(row["memory_json"]) for row in db.execute(
                "SELECT * FROM npc_memories WHERE campaign_id = ? ORDER BY rowid", (campaign_id,))]
        if not player:
            return records
        visible = []
        for npc in records:
            if not npc["player_visible"]:
                continue
            item = {key:npc[key] for key in ("id", "name", "role", "public_notes")}
            if npc["relationship_revealed"]:
                item.update({key:npc[key] for key in ("disposition", "trust", "feelings")})
            item["knowledge"] = [{key:fact[key] for key in ("fact", "learned_from", "evidence_event_id")}
                                 for fact in npc["knowledge"] if fact["player_visible"]]
            visible.append(item)
        return visible

    def _save_npc(self, db, campaign_id, memory):
        npc_id = str(memory.id or uuid.uuid4())
        existing = db.execute("SELECT campaign_id FROM npc_memories WHERE id = ?", (npc_id,)).fetchone()
        if existing and existing["campaign_id"] != campaign_id:
            raise ValueError("This NPC belongs to another campaign")
        for fact in memory.knowledge:
            if fact.evidence_event_id:
                event = db.execute("SELECT campaign_id FROM campaign_events WHERE id = ?",
                                   (str(fact.evidence_event_id),)).fetchone()
                if not event or event["campaign_id"] != campaign_id:
                    raise ValueError("NPC knowledge must reference an event from this campaign")
        data = memory.model_dump(mode="json")
        data["id"] = npc_id
        db.execute("""INSERT INTO npc_memories (id, campaign_id, memory_json, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET memory_json = excluded.memory_json, updated_at = excluded.updated_at""",
            (npc_id, campaign_id, json.dumps(data), now()))
        return data

    def npc(self, campaign_id, memory):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._save_npc(db, campaign_id, memory)

    def turn(self, campaign_id, update: TurnUpdate):
        # The log, stats, NPC memory, and milestone changes commit together.
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id FROM campaign_events WHERE campaign_id = ? AND idempotency_key = ?",
                                  (campaign_id, str(update.idempotency_key))).fetchone()
            if previous:
                return {"event_id":previous["id"], "duplicate":True}
            row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if row is None:
                raise ValueError("Campaign not found")
            sheet = json.loads(row["character_json"])
            changes = dict(update.character_changes)
            if "abilities" in changes and isinstance(changes["abilities"], dict):
                changes["abilities"] = {**sheet["abilities"], **changes["abilities"]}
            sheet = CharacterSheet.model_validate({**sheet, **changes})
            event_id = str(uuid.uuid4())
            db.execute("""INSERT INTO campaign_events (id, campaign_id, idempotency_key, event_json, created_at)
                VALUES (?, ?, ?, ?, ?)""", (event_id, campaign_id, str(update.idempotency_key), update.event.model_dump_json(), now()))
            for memory in update.npc_updates:
                # Facts without a supplied reference are learned during this turn.
                memory = memory.model_copy(deep=True)
                for fact in memory.knowledge:
                    if fact.evidence_event_id is None:
                        fact.evidence_event_id = uuid.UUID(event_id)
                if memory.id:
                    existing = db.execute("SELECT memory_json FROM npc_memories WHERE id = ? AND campaign_id = ?",
                                          (str(memory.id), campaign_id)).fetchone()
                    if existing:
                        previous = json.loads(existing["memory_json"])
                        changes = memory.model_dump(mode="json", exclude_unset=True)
                        if "knowledge" in changes:
                            known = {fact["fact"].casefold():fact for fact in previous["knowledge"]}
                            for fact in changes["knowledge"]:
                                key = fact["fact"].casefold()
                                if key in known:
                                    known[key]["player_visible"] |= fact["player_visible"]
                                else:
                                    known[key] = fact
                            changes["knowledge"] = list(known.values())
                        memory = NpcMemory.model_validate({**previous, **changes})
                self._save_npc(db, campaign_id, memory)
            plan = json.loads(row["plan_json"] or "null")
            for phase in update.phase_updates:
                target = next((item for item in (plan or {}).get("phases", []) if item["id"] == phase.phase_id), None)
                if target is None:
                    raise ValueError("Campaign phase not found")
                if phase.status == "active":
                    for other in plan["phases"]:
                        if other["status"] == "active":
                            other["status"] = "planned"
                target["status"] = phase.status
            db.execute("""UPDATE campaigns SET character_json = ?, plan_json = ?, updated_at = ?,
                character_revision = character_revision + ? WHERE id = ?""",
                (sheet.model_dump_json(), json.dumps(plan) if plan else None, now(), int(bool(update.character_changes)), campaign_id))
            return {"event_id":event_id, "duplicate":False}

    def planning(self, campaign_id, **values):
        allowed = {"plan_status", "plan_progress", "plan_detail", "plan_error"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid planning update")
        with self.db.connect() as db:
            db.execute(f"UPDATE campaigns SET {', '.join(key + ' = ?' for key in values)}, updated_at = ? WHERE id = ?",
                       (*values.values(), now(), campaign_id))

    def reading(self, campaign_id, batch_hash, digest=None):
        with self.db.connect() as db:
            if digest is not None:
                db.execute("INSERT OR REPLACE INTO campaign_readings VALUES (?, ?, ?)",
                           (campaign_id, batch_hash, json.dumps(digest)))
            row = db.execute("SELECT digest_json FROM campaign_readings WHERE campaign_id = ? AND batch_hash = ?",
                             (campaign_id, batch_hash)).fetchone()
            return json.loads(row["digest_json"]) if row else None

    def save_plan(self, campaign_id, document):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT plan_version FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()[0] + 1
            document = {**document, "version":version, "created_at":now()}
            encoded = json.dumps(document)
            db.execute("INSERT INTO campaign_plan_history VALUES (?, ?, ?, ?)", (campaign_id, version, encoded, now()))
            db.execute("""UPDATE campaigns SET plan_json = ?, plan_version = ?, plan_status = 'ready',
                plan_progress = 100, plan_error = NULL, plan_detail = 'Campaign outline ready', updated_at = ? WHERE id = ?""",
                (encoded, version, now(), campaign_id))

    def snapshot(self, campaign_id, dm=False):
        campaign = self.get(campaign_id)
        if campaign is None:
            return None
        campaign["events"] = self.events(campaign_id, player=not dm)
        campaign["npcs"] = self.npcs(campaign_id, player=not dm)
        campaign["derived"] = CharacterSheet.model_validate(campaign["character"]).derived()
        if not dm:
            campaign["character"].pop("dm_notes", None)
            campaign.pop("plan", None)
            # Failures can contain internal planning details; expose only the generic status.
            campaign.pop("plan_error", None)
            campaign.pop("plan_detail", None)
        return campaign

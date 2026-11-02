"""Campaign-owned tactical state, independent of the SVG renderer."""
import json

from pydantic import ValidationError

from .battle_models import BattleChange, BattleState
from .database import now


class BattleConflict(ValueError):
    pass


class BattleError(ValueError):
    pass


class CampaignBattle:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def initialize(db):
        db.execute("""CREATE TABLE IF NOT EXISTS campaign_battles (
            campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
            state_json TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
        )""")

    def snapshot(self, campaign_id, character=None, db=None):
        if db is None:
            with self.db.connect() as connection:
                return self.snapshot(campaign_id, character, connection)
        row = db.execute("SELECT * FROM campaign_battles WHERE campaign_id = ?", (campaign_id,)).fetchone()
        if row is None:
            return {"state":None, "revision":0, "updated_at":None}
        state = json.loads(row["state_json"])
        if character is None:
            character = json.loads(db.execute("SELECT character_json FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()[0])
        # Linked character resources have one owner: the canonical character sheet.
        for token in state["tokens"]:
            if token.get("character_ref") == "player":
                token.update(name=character["name"] or "Adventurer", hp_current=character["hp_current"],
                             hp_max=character["hp_max"], conditions=character["conditions"])
        return {"state":state, "revision":row["revision"], "updated_at":row["updated_at"]}

    def apply(self, campaign_id, change: BattleChange, db=None):
        if db is None:
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.apply(campaign_id, change, connection)
        row = db.execute("SELECT * FROM campaign_battles WHERE campaign_id = ?", (campaign_id,)).fetchone()
        revision = row["revision"] if row else 0
        if revision != change.expected_revision:
            raise BattleConflict("The battle map changed. Reload its latest state before trying again.")
        if change.state is not None:
            state = change.state
        else:
            if row is None:
                raise BattleError("Create a battle map before editing tokens")
            data = json.loads(row["state_json"])
            tokens = {token["id"]:token for token in data["tokens"]}
            for token_id in change.remove_tokens:
                if token_id not in tokens:
                    raise BattleError("The token to remove no longer exists")
                del tokens[token_id]
            for token in change.spawn_tokens:
                if token.id in tokens:
                    raise BattleError("Choose a new unique token ID")
                tokens[token.id] = token.model_dump(mode="json")
            for update in [*change.token_updates, *change.move_tokens]:
                if update.id not in tokens:
                    raise BattleError("The token to update no longer exists")
                tokens[update.id].update(update.model_dump(mode="json", exclude_unset=True, exclude={"id"}))
            data["tokens"] = list(tokens.values())
            if change.map is not None:
                data["map"] = change.map.model_dump(mode="json")
            doors = {door["id"]:door for door in data["map"]["doors"]}
            for update in change.door_updates:
                if update.id not in doors:
                    raise BattleError("The door to update no longer exists")
                doors[update.id]["open"] = update.open
            if change.combat is not None:
                data["combat"] = change.combat.model_dump(mode="json")
            elif data["combat"]["active_token_id"] in change.remove_tokens:
                data["combat"]["active_token_id"] = None
            try:
                state = BattleState.model_validate(data)
            except ValidationError as exc:
                raise BattleError(exc.errors()[0]["msg"]) from exc
        stamp = now()
        db.execute("""INSERT INTO campaign_battles VALUES (?, ?, ?, ?)
            ON CONFLICT(campaign_id) DO UPDATE SET state_json = excluded.state_json,
            revision = excluded.revision, updated_at = excluded.updated_at""",
            (campaign_id, state.model_dump_json(), revision + 1, stamp))
        db.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (stamp, campaign_id))
        return self.snapshot(campaign_id, db=db)


def starter_state(settings):
    """A named starter template, never represented as source-book or AI-generated canon."""
    w, h = settings.width, settings.height
    tokens = [{"id":"pc_1", "name":"Adventurer", "faction":"player", "character_ref":"player", "x":2, "y":h - 3}]
    grid = {"width":w, "height":h}
    if settings.preset == "room":
        grid.update(walls=[{"x1":1,"y1":1,"x2":w-1,"y2":1}, {"x1":w-1,"y1":1,"x2":w-1,"y2":h-1},
            {"x1":w-1,"y1":h-1,"x2":1,"y2":h-1}, {"x1":1,"y1":h-1,"x2":1,"y2":1},
            {"x1":w//2,"y1":1,"x2":w//2,"y2":h-3}],
            doors=[{"id":"door_1","x1":w//2,"y1":4,"x2":w//2,"y2":5,"open":False}],
            terrain=[{"x":x,"y":y,"kind":kind} for x,y,kind in [
                (2,2,"obstacle"),(3,2,"obstacle"),(w-4,h-4,"obstacle"),(3,3,"difficult"),(3,4,"difficult"),
                (w-3,3,"hazard"),(w-3,h-4,"water"),(w-2,h-4,"water"),(w-3,h-3,"water"),(w-2,h-3,"water")]])
        tokens += [{"id":"ally_1","name":"Guard","faction":"ally","x":3,"y":h-3,"hp_current":9,"hp_max":9},
                   {"id":"enemy_1","name":"Goblin 1","faction":"enemy","x":w-4,"y":2,"hp_current":7,"hp_max":7},
                   {"id":"enemy_2","name":"Goblin 2","faction":"enemy","x":w-3,"y":4,"hp_current":7,"hp_max":7},
                   {"id":"neutral_1","name":"Traveler","faction":"neutral","x":w-2,"y":h-2}]
    return BattleState.model_validate({"title":"Ruined outpost · starter" if settings.preset == "room" else "Open ground",
        "map":grid, "tokens":tokens, "combat":{"active":settings.preset == "room", "round":1, "active_token_id":"pc_1"}})

"""Structured, public tactical state. Coordinates are zero-based grid cells."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Cell = Annotated[int, Field(ge=0, le=40, strict=True)]
EntityId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Label = Annotated[str, Field(min_length=1, max_length=80)]
TokenName = Annotated[str, Field(min_length=1, max_length=160)]
Condition = Annotated[str, Field(max_length=160)]
Faction = Literal["player", "ally", "enemy", "neutral"]


class BattleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TerrainTile(BattleRecord):
    x: Cell
    y: Cell
    kind: Literal["floor", "difficult", "water", "hazard", "obstacle"]


class Wall(BattleRecord):
    x1: Cell
    y1: Cell
    x2: Cell
    y2: Cell

    @model_validator(mode="after")
    def straight(self):
        if (self.x1 == self.x2) == (self.y1 == self.y2):
            raise ValueError("Walls must be nonzero horizontal or vertical grid segments")
        return self


class Door(Wall):
    id: EntityId
    open: bool = False

    @model_validator(mode="after")
    def one_edge(self):
        if abs(self.x1 - self.x2) + abs(self.y1 - self.y2) != 1:
            raise ValueError("A door occupies one grid edge")
        return self


class BattleGrid(BattleRecord):
    width: int = Field(10, ge=3, le=40, strict=True)
    height: int = Field(10, ge=3, le=40, strict=True)
    feet_per_cell: Literal[5] = 5
    terrain: list[TerrainTile] = Field(default_factory=list, max_length=1600)
    walls: list[Wall] = Field(default_factory=list, max_length=400)
    doors: list[Door] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def coherent_grid(self):
        cells = set()
        for tile in self.terrain:
            if tile.x >= self.width or tile.y >= self.height:
                raise ValueError("Terrain must be inside the grid")
            if (tile.x, tile.y) in cells:
                raise ValueError("Use one terrain type per cell")
            cells.add((tile.x, tile.y))
        for segment in [*self.walls, *self.doors]:
            if max(segment.x1, segment.x2) > self.width or max(segment.y1, segment.y2) > self.height:
                raise ValueError("Walls and doors must lie on grid boundaries")
        if len({door.id for door in self.doors}) != len(self.doors):
            raise ValueError("Door IDs must be unique")
        edges = [tuple(sorted(((door.x1, door.y1), (door.x2, door.y2)))) for door in self.doors]
        if len(set(edges)) != len(edges):
            raise ValueError("Only one door can occupy an edge")
        return self


class BattleToken(BattleRecord):
    id: EntityId
    name: TokenName
    faction: Faction = "neutral"
    x: Cell
    y: Cell
    character_ref: Literal["player"] | None = None
    hp_current: int | None = Field(None, ge=0, le=10000, strict=True)
    hp_max: int | None = Field(None, ge=1, le=10000, strict=True)
    conditions: list[Condition] = Field(default_factory=list, max_length=30)
    movement_remaining: int | None = Field(None, ge=0, le=1000, strict=True)
    notes: str = Field("", max_length=800)

    @model_validator(mode="after")
    def coherent_token(self):
        if self.hp_current is not None and self.hp_max is not None and self.hp_current > self.hp_max:
            raise ValueError("Token HP cannot exceed maximum HP")
        if self.character_ref and self.faction != "player":
            raise ValueError("A linked player character must use the player faction")
        return self


class CombatState(BattleRecord):
    active: bool = False
    round: int = Field(1, ge=1, le=100000, strict=True)
    active_token_id: EntityId | None = None
    notes: str = Field("", max_length=1200)


class BattleState(BattleRecord):
    title: Label = "Encounter"
    map: BattleGrid = Field(default_factory=BattleGrid)
    tokens: list[BattleToken] = Field(default_factory=list, max_length=100)
    combat: CombatState = Field(default_factory=CombatState)

    @model_validator(mode="after")
    def coherent_state(self):
        ids, cells = set(), set()
        blocked = {(tile.x, tile.y) for tile in self.map.terrain if tile.kind == "obstacle"}
        for token in self.tokens:
            if token.id in ids:
                raise ValueError("Token IDs must be unique; displayed names may repeat")
            if token.x >= self.map.width or token.y >= self.map.height:
                raise ValueError("Tokens must be inside the grid")
            if (token.x, token.y) in blocked:
                raise ValueError("A token cannot occupy an obstacle")
            if (token.x, token.y) in cells:
                raise ValueError("That square is already occupied")
            ids.add(token.id)
            cells.add((token.x, token.y))
        if sum(token.character_ref == "player" for token in self.tokens) > 1:
            raise ValueError("Only one token may link to the current player character")
        if self.combat.active_token_id is not None and self.combat.active_token_id not in ids:
            raise ValueError("The active combat token must exist")
        return self


class TokenMove(BattleRecord):
    id: EntityId
    x: Cell
    y: Cell


class TokenUpdate(BattleRecord):
    id: EntityId
    name: TokenName | None = None
    faction: Faction | None = None
    hp_current: int | None = Field(None, ge=0, le=10000, strict=True)
    hp_max: int | None = Field(None, ge=1, le=10000, strict=True)
    conditions: list[Condition] | None = Field(None, max_length=30)
    movement_remaining: int | None = Field(None, ge=0, le=1000, strict=True)
    notes: str | None = Field(None, max_length=800)


class DoorUpdate(BattleRecord):
    id: EntityId
    open: bool


class BattleChange(BattleRecord):
    expected_revision: int = Field(ge=0, strict=True)
    state: BattleState | None = None
    map: BattleGrid | None = None
    spawn_tokens: list[BattleToken] = Field(default_factory=list, max_length=100)
    move_tokens: list[TokenMove] = Field(default_factory=list, max_length=100)
    remove_tokens: list[EntityId] = Field(default_factory=list, max_length=100)
    token_updates: list[TokenUpdate] = Field(default_factory=list, max_length=100)
    door_updates: list[DoorUpdate] = Field(default_factory=list, max_length=100)
    combat: CombatState | None = None

    @model_validator(mode="after")
    def coherent_change(self):
        edits = any((self.map, self.spawn_tokens, self.move_tokens, self.remove_tokens, self.token_updates, self.door_updates, self.combat))
        if self.state is not None and edits:
            raise ValueError("Replace the state or patch it, not both")
        if self.state is None and not edits:
            raise ValueError("Provide a battle state or at least one change")
        for entries in (self.spawn_tokens, self.move_tokens, self.token_updates, self.door_updates):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("Do not repeat an ID in the same operation")
        if len(set(self.remove_tokens)) != len(self.remove_tokens):
            raise ValueError("Do not remove the same token twice")
        if set(self.remove_tokens) & {entry.id for entry in [*self.spawn_tokens, *self.move_tokens, *self.token_updates]}:
            raise ValueError("A removed token cannot also be changed or spawned")
        return self


class BattleStarter(BattleRecord):
    expected_revision: int = Field(ge=0, strict=True)
    preset: Literal["room", "empty"] = "room"
    width: int = Field(10, ge=10, le=30, strict=True)
    height: int = Field(10, ge=10, le=30, strict=True)

"""Canonical campaign memory shared by the UI, exports, and future DM turns."""
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .battle_models import BattleChange


Text = Annotated[str, Field(max_length=12000)]
Name = Annotated[str, Field(max_length=160)]
Ability = Literal["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]
ABILITIES = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
SKILLS = {"Acrobatics":"dexterity", "Animal Handling":"wisdom", "Arcana":"intelligence",
          "Athletics":"strength", "Deception":"charisma", "History":"intelligence",
          "Insight":"wisdom", "Intimidation":"charisma", "Investigation":"intelligence",
          "Medicine":"wisdom", "Nature":"intelligence", "Perception":"wisdom",
          "Performance":"charisma", "Persuasion":"charisma", "Religion":"intelligence",
          "Sleight of Hand":"dexterity", "Stealth":"dexterity", "Survival":"wisdom"}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Abilities(Record):
    strength: int = Field(10, ge=1, le=30)
    dexterity: int = Field(10, ge=1, le=30)
    constitution: int = Field(10, ge=1, le=30)
    intelligence: int = Field(10, ge=1, le=30)
    wisdom: int = Field(10, ge=1, le=30)
    charisma: int = Field(10, ge=1, le=30)


class SpellSlots(Record):
    level: int = Field(ge=1, le=9)
    total: int = Field(ge=0, le=30)
    used: int = Field(ge=0, le=30)

    @model_validator(mode="after")
    def available(self):
        if self.used > self.total:
            raise ValueError("Used spell slots cannot exceed total slots")
        return self


class CharacterSheet(Record):
    name: Name = ""
    player_name: Name = ""
    pronouns: Name = ""
    ancestry: Name = ""
    class_name: Name = ""
    subclass: Name = ""
    background: Name = ""
    level: int = Field(1, ge=1, le=20)
    experience: int = Field(0, ge=0)
    abilities: Abilities = Field(default_factory=Abilities)
    proficiency_bonus: int = Field(2, ge=0, le=12)
    armor_class: int = Field(10, ge=0, le=100)
    initiative_bonus: int | None = Field(None, ge=-30, le=50)
    speed: int = Field(30, ge=0, le=1000)
    hp_max: int = Field(1, ge=1, le=10000)
    hp_current: int = Field(1, ge=0, le=10000)
    temp_hp: int = Field(0, ge=0, le=10000)
    hit_dice: Name = ""
    death_save_successes: int = Field(0, ge=0, le=3)
    death_save_failures: int = Field(0, ge=0, le=3)
    inspiration: bool = False
    saving_throw_proficiencies: list[Ability] = Field(default_factory=list, max_length=6)
    skill_proficiencies: list[Name] = Field(default_factory=list, max_length=18)
    skill_expertise: list[Name] = Field(default_factory=list, max_length=18)
    conditions: list[Name] = Field(default_factory=list, max_length=30)
    attacks: Text = ""
    inventory: Text = ""
    currency: Text = ""
    features: Text = ""
    languages: Text = ""
    spells: Text = ""
    spellcasting_ability: Ability | None = None
    spell_slots: list[SpellSlots] = Field(default_factory=list, max_length=9)
    appearance: Text = ""
    backstory: Text = ""
    personality: Text = ""
    ideals: Text = ""
    bonds: Text = ""
    flaws: Text = ""
    goals: Text = ""
    notes: Text = ""
    dm_notes: Text = ""

    @model_validator(mode="after")
    def coherent_stats(self):
        if self.hp_current > self.hp_max:
            raise ValueError("Current HP cannot exceed maximum HP; use temporary HP separately")
        if any(skill not in SKILLS for skill in self.skill_proficiencies + self.skill_expertise):
            raise ValueError("Choose a recognized skill")
        if len({slot.level for slot in self.spell_slots}) != len(self.spell_slots):
            raise ValueError("Each spell slot level must be listed once")
        return self

    def derived(self):
        modifiers = {key:(value - 10) // 2 for key, value in self.abilities.model_dump().items()}
        skills = {skill:modifiers[ability] + self.proficiency_bonus * (
            2 if skill in self.skill_expertise else 1 if skill in self.skill_proficiencies else 0)
            for skill, ability in SKILLS.items()}
        casting = modifiers[self.spellcasting_ability] if self.spellcasting_ability else None
        return {"modifiers":modifiers, "skills":skills,
                "saving_throws":{key:value + (self.proficiency_bonus if key in self.saving_throw_proficiencies else 0)
                                 for key, value in modifiers.items()},
                "initiative":self.initiative_bonus if self.initiative_bonus is not None else modifiers["dexterity"],
                "passive_perception":10 + skills["Perception"],
                "spell_save_dc":8 + self.proficiency_bonus + casting if casting is not None else None,
                "spell_attack_bonus":self.proficiency_bonus + casting if casting is not None else None}


class StoryEvent(Record):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    summary: Annotated[str, Field(min_length=1, max_length=12000)]
    kind: Literal["story", "decision", "discovery", "combat", "loot", "rest", "note", "roll"] = "story"
    session: int = Field(1, ge=1, le=100000)
    world_time: Name = ""
    details: Text = ""
    player_visible: bool = True


class NpcKnowledge(Record):
    fact: Annotated[str, Field(min_length=1, max_length=2000)]
    learned_from: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_event_id: UUID | None = None
    player_visible: bool = False


class NpcMemory(Record):
    id: UUID | None = None
    name: Annotated[str, Field(min_length=1, max_length=160)]
    role: Name = ""
    public_notes: Text = ""
    disposition: Literal["unknown", "friendly", "neutral", "wary", "hostile"] = "unknown"
    trust: int = Field(0, ge=-100, le=100)
    feelings: Text = ""
    private_notes: Text = ""
    knowledge: list[NpcKnowledge] = Field(default_factory=list, max_length=100)
    player_visible: bool = True
    relationship_revealed: bool = False


class PhaseUpdate(Record):
    phase_id: Name
    status: Literal["planned", "active", "completed", "skipped"]


class RollRequest(Record):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(max_length=1200)] = ""
    modifier: int = Field(0, ge=-30, le=30, strict=True)
    idempotency_key: UUID = Field(default_factory=uuid4)


class RollPress(Record):
    """A button press supplies no die value or modifier."""


class TurnUpdate(Record):
    event: StoryEvent
    idempotency_key: UUID = Field(default_factory=uuid4)
    character_changes: dict = Field(default_factory=dict)
    npc_updates: list[NpcMemory] = Field(default_factory=list, max_length=50)
    phase_updates: list[PhaseUpdate] = Field(default_factory=list, max_length=50)
    roll_requests: list[RollRequest] = Field(default_factory=list, max_length=8)
    battle_update: BattleChange | None = None

    @model_validator(mode="after")
    def one_current_phase(self):
        if sum(phase.status == "active" for phase in self.phase_updates) > 1:
            raise ValueError("Only one campaign phase can be made current in a turn")
        return self


class CreateCampaign(Record):
    name: Annotated[str, Field(min_length=1, max_length=160)]
    source_book_id: UUID
    rule_set_id: UUID | None = None


class ReadingDigest(Record):
    summary: Annotated[str, Field(max_length=5000)]
    facts: list[Name] = Field(max_length=30)
    hooks: list[Name] = Field(max_length=20)
    npc_agendas: list[Name] = Field(max_length=20)


class PlannedPhase(Record):
    title: Name
    summary: Annotated[str, Field(max_length=2500)]
    goal: Annotated[str, Field(max_length=1200)]
    scenes: list[Annotated[str, Field(max_length=800)]] = Field(max_length=12)
    branches: list[Annotated[str, Field(max_length=800)]] = Field(max_length=8)
    completion_condition: Annotated[str, Field(max_length=1200)]
    source_ids: list[Name] = Field(max_length=30)


class GeneratedPlan(Record):
    overview: Annotated[str, Field(max_length=5000)]
    phases: list[PlannedPhase] = Field(min_length=1, max_length=20)
    endings: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=8)
    npc_agendas: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=30)
    unresolved_questions: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=20)

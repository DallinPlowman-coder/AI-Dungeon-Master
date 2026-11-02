import re
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response

from .document_exports import json_document, markdown_document, pdf_document
from .dice import RollConflict, RollNotFound
from .battle import BattleConflict, BattleError, starter_state
from .battle_models import BattleChange, BattleStarter
from .rulesets import RuleSetSelection, RuleSetError, RuleSetConflict
from .records import CharacterSheet, CreateCampaign, NpcMemory, RollPress, RollRequest, StoryEvent, TurnUpdate


def campaign_routes(library, store, planning, alignment):
    router = APIRouter(prefix="/api/campaigns", tags=["Campaign documents"])

    def require(campaign_id):
        campaign = store.get(str(campaign_id))
        if campaign is None:
            raise HTTPException(404, "Campaign not found.")
        return campaign

    @router.get("")
    def list_campaigns():
        return {"campaigns":store.list()}

    @router.get("/planner")
    def planner_status():
        return planning.provider.status()

    @router.post("", status_code=201)
    def create_campaign(payload: CreateCampaign):
        book = library.db.get_book(str(payload.source_book_id))
        if not book or book["collection"] != "campaign" or book["status"] != "ready" or library.describe(book)["needs_reindex"]:
            raise HTTPException(409, "Choose a fully indexed book from the Campaigns library.")
        try:
            campaign_id = store.create(payload.name.strip(), book, str(payload.rule_set_id) if payload.rule_set_id else None)
        except RuleSetConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuleSetError as exc:
            raise HTTPException(422, str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "The source book changed. Choose an indexed adventure again.") from exc
        planning.submit(campaign_id)
        return store.snapshot(campaign_id)

    @router.get("/{campaign_id}")
    def get_campaign(campaign_id: uuid.UUID):
        require(campaign_id)
        return store.snapshot(str(campaign_id))

    @router.delete("/{campaign_id}")
    def delete_campaign(campaign_id: uuid.UUID):
        if not store.delete(str(campaign_id)):
            raise HTTPException(404, "Campaign not found.")
        return {"deleted":True, "id":str(campaign_id)}

    @router.put("/{campaign_id}/rules")
    def select_rule_set(campaign_id: uuid.UUID, payload: RuleSetSelection):
        require(campaign_id)
        try:
            store.select_rules(str(campaign_id), payload)
        except RuleSetConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuleSetError as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.snapshot(str(campaign_id))

    @router.put("/{campaign_id}/character")
    def save_character(campaign_id: uuid.UUID, payload: CharacterSheet, expected_revision: int | None = Query(None, ge=0)):
        campaign = require(campaign_id)
        if "dm_notes" not in payload.model_fields_set:
            payload.dm_notes = campaign["character"]["dm_notes"]
        if not store.character(str(campaign_id), payload, expected_revision):
            raise HTTPException(409, "This character changed while you were editing. Reload the saved sheet before applying your changes.")
        alignment.submit(str(campaign_id))
        return store.snapshot(str(campaign_id))

    @router.post("/{campaign_id}/events", status_code=201)
    def add_event(campaign_id: uuid.UUID, payload: StoryEvent):
        require(campaign_id)
        return store.turn(str(campaign_id), TurnUpdate(event=payload))

    @router.put("/{campaign_id}/npcs")
    def save_npc(campaign_id: uuid.UUID, payload: NpcMemory):
        require(campaign_id)
        try:
            return store.npc(str(campaign_id), payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/{campaign_id}/npc-memories")
    def npc_memories(campaign_id: uuid.UUID):
        require(campaign_id)
        return {"npcs":store.npcs(str(campaign_id)), "audience":"dm"}

    @router.post("/{campaign_id}/turns")
    def record_turn(campaign_id: uuid.UUID, payload: TurnUpdate):
        require(campaign_id)
        try:
            result = store.turn(str(campaign_id), payload)
        except BattleConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid character, NPC evidence, phase, roll request, or battle update; no changes were saved.") from exc
        alignment.submit(str(campaign_id))
        return result

    @router.post("/{campaign_id}/alignment/retry", status_code=202)
    def retry_alignment(campaign_id: uuid.UUID):
        require(campaign_id)
        return {"queued":alignment.submit(str(campaign_id), retry=True)}

    @router.post("/{campaign_id}/rolls", status_code=201)
    def request_roll(campaign_id: uuid.UUID, payload: RollRequest):
        require(campaign_id)
        try:
            return store.dice.request(str(campaign_id), payload)
        except RollConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/{campaign_id}/rolls/{roll_id}/roll")
    def press_roll(campaign_id: uuid.UUID, roll_id: uuid.UUID, payload: RollPress | None = None):
        require(campaign_id)
        try:
            return store.dice.roll(str(campaign_id), str(roll_id))
        except RollNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/{campaign_id}/plan", status_code=202)
    def generate_plan(campaign_id: uuid.UUID):
        require(campaign_id)
        if not planning.submit(str(campaign_id)):
            raise HTTPException(409, "This campaign is already being planned.")
        return {"queued":True}

    @router.get("/{campaign_id}/battle")
    def get_battle(campaign_id: uuid.UUID):
        require(campaign_id)
        return store.battle.snapshot(str(campaign_id))

    @router.put("/{campaign_id}/battle")
    @router.patch("/{campaign_id}/battle")
    def update_battle(campaign_id: uuid.UUID, payload: BattleChange):
        require(campaign_id)
        try:
            return store.battle.apply(str(campaign_id), payload)
        except BattleConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except BattleError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/{campaign_id}/battle/starter")
    def create_starter(campaign_id: uuid.UUID, payload: BattleStarter):
        require(campaign_id)
        return update_battle(campaign_id, BattleChange(expected_revision=payload.expected_revision, state=starter_state(payload)))

    @router.get("/{campaign_id}/dm-context")
    def dm_context(campaign_id: uuid.UUID):
        require(campaign_id)
        context = store.snapshot(str(campaign_id), dm=True)
        phases = (context.get("plan") or {}).get("phases", [])
        context["next_phases"] = [phase for phase in phases if phase["status"] == "active"] or [
            phase for phase in phases if phase["status"] == "planned"][:1]
        context["ready_for_play"] = context["plan"] is not None
        context["retrieval_scope"] = {"campaign_id":str(campaign_id)}
        context["memory_instruction"] = (
            "If the plan is missing, finish campaign planning before starting play. "
            "Review character, full campaign log, and current plan before choosing the next scene. "
            "The rule_set field identifies this campaign's chosen rulebook set and its member books. "
            "Retrieve references through /api/retrieve with this campaign_id so only its adventure and selected "
            "rules are searched. Never substitute other uploaded rulebooks. If no set is selected, ask the player "
            "to choose one before resolving rules-dependent actions. If a selected set is not ready, ask for its books to be repaired. "
            "Treat the plan as possibilities, and the log as events that actually happened. "
            "Verify established facts against the source adventure; generated alternatives are proposals, not book canon. "
            "NPCs may use only their own recorded knowledge and witnessed events. "
            "The battle field contains the current public tactical state and revision. Create maps as structured "
            "battle_update.state data, never images or freeform frontend commands. Use battle_update with its "
            "expected_revision to move, spawn, remove, or update named tokens, doors, terrain, and combat state. "
            "Keep tokens within the grid and give each a stable unique ID. All map data is player-visible. "
            "Linked player token identity, HP, and conditions come from the character sheet; change those through "
            "character_changes. Position updates do not calculate paths, spend movement, or resolve terrain effects. "
            "Persist each resolved turn through /turns, including stats, NPC evidence, and phase updates. "
            "When a player d20 roll is needed, add roll_requests with a public label, public reason, and modifier. "
            "Pause that action until the player presses its roll button; never invent or submit dice values. "
            "Use the matching stored roll result to continue, and do not treat an unrelated free roll as that check. "
            "A raw 1 or 20 is not by itself an instruction to change stats or decide an outcome. "
            "Alignment is private descriptive background memory, never a player choice or a restriction on actions. "
            "It is reviewed automatically at new level milestones from logged actions; do not set it in character updates. "
            "Never reveal alignment or its reviews, DM notes, hidden NPC motives, or future campaign phases to the player."
        )
        return context

    @router.get("/{campaign_id}/documents/{kind}.{file_format}")
    def download(campaign_id: uuid.UUID, kind: Literal["character", "log", "relationships", "plan"],
                 file_format: Literal["md", "json", "pdf"], audience: Literal["player", "dm"] = "player"):
        require(campaign_id)
        if kind == "plan" and audience != "dm":
            raise HTTPException(403, "The full campaign plan contains spoilers. Use the explicit DM export.")
        campaign = store.snapshot(str(campaign_id), dm=audience == "dm")
        if kind == "plan" and not campaign.get("plan"):
            raise HTTPException(409, "The campaign plan is not ready yet.")
        filename = re.sub(r"[^A-Za-z0-9_-]+", "-", campaign["name"]).strip("-")[:60] or "campaign"
        if file_format == "json":
            content, media = json_document(campaign, kind), "application/json"
        else:
            markdown = markdown_document(campaign, kind, dm=audience == "dm")
            content, media = (pdf_document(markdown), "application/pdf") if file_format == "pdf" else (markdown.encode(), "text/markdown; charset=utf-8")
        return Response(content, media_type=media, headers={
            "Content-Disposition":f'attachment; filename="{filename}-{kind}-{audience}.{file_format}"',
            "Cache-Control":"no-store",
        })

    return router

import io
import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from dungeon_master.config import Settings
from dungeon_master.main import create_app
from dungeon_master.planning import OllamaPlanner, source_batches
from dungeon_master.records import ReadingDigest
from conftest import TestEmbeddings, wait_for_book


class TestPlanner:
    __test__ = False

    def __init__(self, ready=True):
        self.calls = []
        self.ready = ready

    def status(self):
        return {"ready":self.ready, "provider":"ollama", "model":"test-model", "message":"Test planner"}

    def generate(self, instruction, material, schema):
        self.calls.append((schema, material))
        if schema is ReadingDigest:
            return {"summary":"The lantern guards a hidden shrine.", "facts":["Mara owns the inn."],
                    "hooks":["Explore the aqueduct."], "npc_agendas":["Mara wants clean water."]}
        source = re.findall(r'"(C\d+)"', material)[0]
        return {"overview":"A proposed adventure from the inn to the shrine.",
                "phases":[{"title":title, "summary":"A possible scene.", "goal":"Find the source of the water.",
                           "scenes":["Speak to Mara."], "branches":["Investigate quietly or ask for help."],
                           "completion_condition":"Resolve the scene.", "source_ids":[source]}
                          for title in ("The invitation", "The hidden shrine")],
                "endings":["Restore the seal or negotiate another solution."],
                "npc_agendas":["SECRET_VILLAIN_MOTIVE"], "unresolved_questions":["Who first moved the seal?"]}


@pytest.fixture
def campaign_client(tmp_path):
    planner = TestPlanner()
    app = create_app(Settings(data_dir=tmp_path), TestEmbeddings(), planner)
    with TestClient(app) as client:
        client.test_planner = planner
        yield client


def wait_plan(client, campaign_id):
    for _ in range(300):
        context = client.get(f"/api/campaigns/{campaign_id}/dm-context").json()
        if context["plan_status"] in {"ready", "failed", "needs_setup"}:
            return context
        time.sleep(.01)
    raise AssertionError("Planning did not finish")


def campaign(client, text=b"Mara sends the travelers to a shrine beneath the city."):
    book = client.post("/api/books", data={"collection":"campaign"}, files={"file":("adventure.txt", text)}).json()["book"]
    assert wait_for_book(client, book["id"])["status"] == "ready"
    result = client.post("/api/campaigns", json={"name":"Briarwell", "source_book_id":book["id"]})
    assert result.status_code == 201, result.text
    campaign_id = result.json()["id"]
    context = wait_plan(client, campaign_id)
    assert context["plan_status"] == "ready", context.get("plan_error")
    return campaign_id


def test_create_plans_the_whole_book_and_keeps_plan_out_of_player_state(campaign_client):
    client = campaign_client
    campaign_id = campaign(client, ("A lantern guards the shrine. " * 2000).encode())
    public = client.get(f"/api/campaigns/{campaign_id}").json()
    assert "plan" not in public and "dm_notes" not in public["character"]
    context = client.get(f"/api/campaigns/{campaign_id}/dm-context").json()
    plan = context["plan"]
    assert plan["coverage"]["complete"]
    total = client.app.state.library.db.get_book(context["source_book_id"])["chunk_count"]
    assert plan["coverage"]["passages_read"] == total
    material = "\n".join(value for schema, value in client.test_planner.calls if schema is ReadingDigest)
    for chunk in client.app.state.library.db.preview_chunks(context["source_book_id"], 0, total):
        assert f"[C{chunk['id']}]" in material
    assert "SECRET_VILLAIN_MOTIVE" not in json.dumps(public)
    assert context["next_phases"][0]["id"] == "phase-1"


def test_character_stats_validation_private_notes_and_revision_conflicts(campaign_client):
    client = campaign_client
    cid = campaign(client)
    endpoint = f"/api/campaigns/{cid}/character"
    sheet = {"name":"Éowyn", "hp_max":20, "hp_current":15, "abilities":{"dexterity":16, "wisdom":14},
             "skill_proficiencies":["Perception"], "skill_expertise":["Stealth"], "dm_notes":"SECRET_BACKSTORY"}
    saved = client.put(endpoint + "?expected_revision=0", json=sheet)
    assert saved.status_code == 200
    assert saved.json()["derived"]["modifiers"]["dexterity"] == 3
    assert saved.json()["derived"]["skills"]["Stealth"] == 7
    assert saved.json()["derived"]["passive_perception"] == 14
    assert "SECRET_BACKSTORY" not in saved.text
    revision = saved.json()["character_revision"]
    assert client.put(endpoint + "?expected_revision=0", json=sheet).status_code == 409
    player_sheet = saved.json()["character"]
    player_sheet["name"] = "Changed name"
    assert client.put(endpoint + f"?expected_revision={revision}", json=player_sheet).status_code == 200
    assert client.get(f"/api/campaigns/{cid}/dm-context").json()["character"]["dm_notes"] == "SECRET_BACKSTORY"
    assert client.put(endpoint, json={"hp_current":21, "hp_max":20}).status_code == 422
    assert client.put(endpoint, json={"skill_proficiencies":["Invalid skill"]}).status_code == 422
    assert client.put(endpoint, json={"spell_slots":[{"level":1,"total":1,"used":2}]}).status_code == 422


def test_one_turn_commits_log_character_npc_evidence_and_phase_together(campaign_client):
    client = campaign_client
    cid = campaign(client)
    payload = {"idempotency_key":str(uuid.uuid4()), "event":{"title":"Mara learns the secret", "summary":"The traveler speaks to Mara."},
               "character_changes":{"name":"Elara", "hp_max":10, "hp_current":8, "abilities":{"wisdom":16}},
               "npc_updates":[{"name":"Mara", "disposition":"friendly", "knowledge":[{"fact":"Elara seeks the seal", "learned_from":"Elara said so during this conversation"}]}],
               "phase_updates":[{"phase_id":"phase-1", "status":"completed"}]}
    result = client.post(f"/api/campaigns/{cid}/turns", json=payload)
    assert result.status_code == 200
    event_id = result.json()["event_id"]
    context = client.get(f"/api/campaigns/{cid}/dm-context").json()
    assert len(context["events"]) == 1
    assert context["character"]["hp_current"] == 8 and context["character"]["abilities"]["strength"] == 10
    assert context["npcs"][0]["knowledge"][0]["evidence_event_id"] == event_id
    assert context["next_phases"][0]["id"] == "phase-2"
    assert client.post(f"/api/campaigns/{cid}/turns", json=payload).json()["duplicate"]
    assert len(client.get(f"/api/campaigns/{cid}").json()["events"]) == 1


def test_invalid_turn_rolls_back_all_changes(campaign_client):
    client = campaign_client
    cid = campaign(client)
    result = client.post(f"/api/campaigns/{cid}/turns", json={"event":{"title":"Should not persist", "summary":"Invalid change"},
        "character_changes":{"name":"Should not persist"}, "phase_updates":[{"phase_id":"missing", "status":"completed"}]})
    assert result.status_code == 422
    state = client.get(f"/api/campaigns/{cid}").json()
    assert state["events"] == [] and state["character"]["name"] == ""


def test_npc_knowledge_is_accumulated_and_isolated_from_other_campaigns(campaign_client):
    client = campaign_client
    first, second = campaign(client), campaign(client)
    npc = client.put(f"/api/campaigns/{first}/npcs", json={"name":"Mara", "feelings":"Grateful", "private_notes":"SECRET_MOTIVE",
        "knowledge":[{"fact":"Elara owns a compass", "learned_from":"Saw it at the inn"}]}).json()
    turn = {"event":{"title":"A conversation", "summary":"Elara names the shrine."}, "npc_updates":[{"id":npc["id"], "name":"Mara",
        "knowledge":[{"fact":"Elara seeks the shrine", "learned_from":"Elara told Mara"}]}]}
    assert client.post(f"/api/campaigns/{first}/turns", json=turn).status_code == 200
    stored = client.get(f"/api/campaigns/{first}/npc-memories").json()["npcs"][0]
    assert len(stored["knowledge"]) == 2 and stored["feelings"] == "Grateful" and stored["private_notes"] == "SECRET_MOTIVE"
    assert client.put(f"/api/campaigns/{second}/npcs", json=npc).status_code == 422
    event_id = client.get(f"/api/campaigns/{first}").json()["events"][0]["id"]
    assert client.put(f"/api/campaigns/{second}/npcs", json={"name":"Other NPC", "knowledge":[{"fact":"Cross campaign", "learned_from":"Invalid", "evidence_event_id":event_id}]}).status_code == 422


@pytest.mark.parametrize("file_format", ["md", "json", "pdf"])
def test_player_downloads_exclude_spoilers_and_hidden_npc_memory(campaign_client, file_format):
    client = campaign_client
    cid = campaign(client)
    client.put(f"/api/campaigns/{cid}/character", json={"name":"Elara", "dm_notes":"SECRET_CHARACTER"})
    client.post(f"/api/campaigns/{cid}/events", json={"title":"A friendly arrival", "summary":"Elara meets Mara."})
    client.post(f"/api/campaigns/{cid}/events", json={"title":"SECRET_EVENT", "summary":"SECRET_PLOT", "player_visible":False})
    client.put(f"/api/campaigns/{cid}/npcs", json={"name":"Mara", "public_notes":"Owns the inn", "feelings":"SECRET_FEELING", "private_notes":"SECRET_NPC",
        "knowledge":[{"fact":"SECRET_FACT", "learned_from":"A hidden scene"}, {"fact":"Elara has a compass", "learned_from":"Saw it", "player_visible":True}]})
    client.put(f"/api/campaigns/{cid}/npcs", json={"name":"SECRET_UNKNOWN_NPC", "player_visible":False})
    for kind in ("character", "log", "relationships"):
        response = client.get(f"/api/campaigns/{cid}/documents/{kind}.{file_format}")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        content = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages) if file_format == "pdf" else response.text
        assert "SECRET_" not in content
        if kind == "relationships":
            assert "Elara has a compass" in content
    assert client.get(f"/api/campaigns/{cid}/documents/plan.{file_format}").status_code == 403
    dm = client.get(f"/api/campaigns/{cid}/documents/plan.{file_format}?audience=dm")
    assert dm.status_code == 200


def test_revealed_feelings_are_in_player_records(campaign_client):
    client = campaign_client
    cid = campaign(client)
    client.put(f"/api/campaigns/{cid}/npcs", json={"name":"Mara", "disposition":"friendly", "trust":40, "feelings":"Grateful for the rescue", "relationship_revealed":True})
    npc = client.get(f"/api/campaigns/{cid}").json()["npcs"][0]
    assert npc["trust"] == 40 and npc["feelings"] == "Grateful for the rescue"


def test_replanning_uses_checkpoints_and_preserves_character_and_log(campaign_client):
    client = campaign_client
    cid = campaign(client)
    client.put(f"/api/campaigns/{cid}/character", json={"name":"Elara"})
    client.post(f"/api/campaigns/{cid}/events", json={"title":"Arrival", "summary":"Elara arrives."})
    client.test_planner.calls.clear()
    assert client.post(f"/api/campaigns/{cid}/plan").status_code == 202
    assert wait_plan(client, cid)["plan"]["version"] == 2
    assert not any(schema is ReadingDigest for schema, material in client.test_planner.calls)
    state = client.get(f"/api/campaigns/{cid}").json()
    assert state["character"]["name"] == "Elara" and len(state["events"]) == 1


def test_only_one_phase_is_current_and_hidden_progress_events_stay_private(campaign_client):
    client = campaign_client
    cid = campaign(client)
    for phase in ("phase-1", "phase-2"):
        assert client.post(f"/api/campaigns/{cid}/turns", json={"event":{"title":"Progress", "summary":"Hidden milestone", "player_visible":False},
            "phase_updates":[{"phase_id":phase, "status":"active"}]}).status_code == 200
    context = client.get(f"/api/campaigns/{cid}/dm-context").json()
    assert [phase["id"] for phase in context["next_phases"]] == ["phase-2"]
    assert client.get(f"/api/campaigns/{cid}").json()["events"] == []


def test_missing_local_model_does_not_block_player_records(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), TestEmbeddings(), TestPlanner(ready=False))) as client:
        book = client.post("/api/books", files={"file":("book.txt", b"A shrine below the city.")}).json()["book"]
        wait_for_book(client, book["id"])
        response = client.post("/api/campaigns", json={"name":"A campaign", "source_book_id":book["id"]})
        cid = response.json()["id"]
        assert wait_plan(client, cid)["plan_status"] == "needs_setup"
        assert client.get(f"/api/campaigns/{cid}/documents/character.pdf").status_code == 200


def test_campaign_records_survive_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        cid = campaign(client)
        client.put(f"/api/campaigns/{cid}/character", json={"name":"Elara"})
        client.post(f"/api/campaigns/{cid}/events", json={"title":"Arrival", "summary":"A lasting event."})
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        state = client.get(f"/api/campaigns/{cid}").json()
        assert state["character"]["name"] == "Elara" and state["events"][0]["summary"] == "A lasting event."
        assert client.get(f"/api/campaigns/{cid}/dm-context").json()["plan"]["coverage"]["complete"]


def test_campaign_validation_and_local_provider_boundary(campaign_client, tmp_path):
    client = campaign_client
    rules = client.post("/api/books", data={"collection":"rules"}, files={"file":("rules.txt", b"An original rule.")}).json()["book"]
    wait_for_book(client, rules["id"])
    assert client.post("/api/campaigns", json={"name":"Wrong collection", "source_book_id":rules["id"]}).status_code == 409
    assert client.post("/api/campaigns", json={"name":"   ", "source_book_id":rules["id"]}).status_code == 422
    assert client.get(f"/api/campaigns/{uuid.uuid4()}").status_code == 404
    assert client.get("/campaigns").status_code == 200
    with pytest.raises(ValueError, match="local HTTP"):
        OllamaPlanner(Settings(data_dir=tmp_path, ollama_url="https://example.com"))


def test_source_batching_never_omits_passages():
    chunks = [{"id":i, "text":"x" * 70} for i in range(20)]
    assert [item["id"] for batch in source_batches(chunks, max_characters=200) for item in batch] == list(range(20))


def test_repeated_known_fact_can_be_revealed_without_losing_original_evidence(campaign_client):
    client = campaign_client
    cid = campaign(client)
    npc = client.put(f"/api/campaigns/{cid}/npcs", json={"name":"Mara", "knowledge":[
        {"fact":"Elara owns a compass", "learned_from":"Saw it at the inn"}]}).json()
    response = client.post(f"/api/campaigns/{cid}/turns", json={
        "event":{"title":"Mara mentions the compass", "summary":"Elara learns that Mara noticed it."},
        "npc_updates":[{"id":npc["id"], "name":"Mara", "knowledge":[
            {"fact":"Elara owns a compass", "learned_from":"Mentioned it in conversation", "player_visible":True}]}]})
    assert response.status_code == 200
    knowledge = client.get(f"/api/campaigns/{cid}").json()["npcs"][0]["knowledge"]
    assert len(knowledge) == 1 and knowledge[0]["learned_from"] == "Saw it at the inn"


def test_failed_replanning_keeps_the_previous_plan(campaign_client):
    client = campaign_client
    cid = campaign(client)
    original = client.test_planner.generate
    def invalid_sources(instruction, material, schema):
        result = original(instruction, material, schema)
        if schema is not ReadingDigest:
            result["phases"][0]["source_ids"] = ["C99999999"]
        return result
    client.test_planner.generate = invalid_sources
    client.post(f"/api/campaigns/{cid}/plan")
    state = wait_plan(client, cid)
    assert state["plan_status"] == "failed" and state["plan"]["version"] == 1
    assert client.get(f"/api/campaigns/{cid}/documents/plan.pdf?audience=dm").status_code == 200


def test_hierarchical_reading_reduces_long_notes_without_losing_source_coverage(tmp_path):
    class LongNotes(TestPlanner):
        def generate(self, instruction, material, schema):
            result = super().generate(instruction, material, schema)
            if schema is ReadingDigest and instruction.startswith("Read this complete"):
                result["summary"] = "An important source fact. " * 160
                result["facts"] = ["Specific fact " + str(i) + "x" * 125 for i in range(20)]
            return result
    provider = LongNotes()
    with TestClient(create_app(Settings(data_dir=tmp_path), TestEmbeddings(), provider)) as client:
        cid = campaign(client, ("A lantern guards the old shrine. " * 4000).encode())
        plan = client.get(f"/api/campaigns/{cid}/dm-context").json()["plan"]
        assert plan["coverage"]["complete"]
        reading = [material for schema, material in provider.calls if schema is ReadingDigest]
        assert any(material.startswith("[") and not material.startswith("[C") for material in reading)
        assert all(len(material) < 24000 for material in reading)

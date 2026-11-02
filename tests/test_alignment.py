import io
import json
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from dungeon_master.alignment import AlignmentReading
from dungeon_master.config import Settings
from dungeon_master.main import create_app
from dungeon_master.planning import OllamaPlanner
from conftest import TestEmbeddings
from test_campaign_records import TestPlanner, campaign


class ActionPlanner(TestPlanner):
    __test__ = False

    def __init__(self):
        super().__init__()
        self.action_calls = []
        self.invalid = None
        self.care = 2
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def generate(self, instruction, material, schema):
        if schema is not AlignmentReading:
            return super().generate(instruction, material, schema)
        data = json.loads(material)
        self.action_calls.append(data)
        self.entered.set()
        assert self.release.wait(5), "Test model was never released"
        observations = [{"event_ref":event["event_ref"], "order":0, "care":self.care,
                         "reason":"The character voluntarily helped the villagers."} for event in data["events"]]
        if self.invalid == "foreign" or (self.invalid == "second_batch" and len(self.action_calls) == 2):
            observations[0]["event_ref"] = "E16"
        if self.invalid == "duplicate":
            observations = [observations[0], observations[0]]
        if self.invalid == "score":
            observations[0]["care"] = 999
        if self.invalid == "none":
            observations = []
        return {"observations":observations}


@pytest.fixture
def aligned_client(tmp_path):
    provider = ActionPlanner()
    app = create_app(Settings(data_dir=tmp_path), TestEmbeddings(), provider)
    with TestClient(app) as client:
        client.actions = provider
        yield client


def private(client, cid):
    return client.get(f"/api/campaigns/{cid}/dm-context").json()["alignment"]


def wait_review(client, cid, level=2):
    for _ in range(300):
        state = private(client, cid)
        if state["history"] and state["history"][-1]["level"] == level and state["history"][-1]["status"] in {"ready", "failed", "needs_setup"}:
            return state
        time.sleep(.01)
    raise AssertionError("Action review did not finish")


def event(client, cid, title="A good deed"):
    result = client.post(f"/api/campaigns/{cid}/events", json={"title":title, "kind":"decision",
        "summary":"Elara donates her own supplies to feed the stranded villagers."})
    assert result.status_code == 201
    return result.json()["event_id"]


def level(client, cid, number, **kwargs):
    return client.put(f"/api/campaigns/{cid}/character", json={"name":"Elara", "level":number, **kwargs})


@pytest.mark.parametrize("file_format", ["md", "json", "pdf"])
def test_alignment_changes_only_at_level_up_and_is_private(aligned_client, file_format):
    client = aligned_client
    cid = campaign(client)
    evidence = event(client, cid)
    assert private(client, cid)["label"] == "undetermined"
    assert not client.actions.action_calls
    response = level(client, cid, 2)
    assert response.status_code == 200 and "alignment" not in response.text
    state = wait_review(client, cid)
    assert state["label"] == "neutral good"
    assert state["history"][0]["result"]["observations"][0]["event_id"] == evidence
    assert client.get(f"/api/campaigns/{cid}").json()["character_revision"] == 1
    for kind in ("character", "log", "relationships"):
        response = client.get(f"/api/campaigns/{cid}/documents/{kind}.{file_format}")
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages) if file_format == "pdf" else response.text
        assert "alignment" not in text.lower() and "neutral good" not in text.lower()
    dm = client.get(f"/api/campaigns/{cid}/documents/character.{file_format}?audience=dm")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(dm.content)).pages) if file_format == "pdf" else dm.text
    assert "neutral good" in text and evidence in text
    assert "alignment" not in client.get("/api/campaigns").text


def test_repeat_saves_and_level_down_cannot_rescore_a_milestone(aligned_client):
    client = aligned_client
    cid = campaign(client)
    event(client, cid)
    level(client, cid, 2)
    wait_review(client, cid)
    for value in (2, 1, 2):
        assert level(client, cid, value).status_code == 200
    assert len(private(client, cid)["history"]) == 1
    client.actions.care = -2
    event(client, cid, "A harmful choice")
    level(client, cid, 3)
    state = wait_review(client, cid, 3)
    assert state["action_count"] == 2 and state["label"] == "true neutral"
    assert len(client.actions.action_calls) == 2


def test_level_up_turn_includes_its_actions_and_retry_is_idempotent(aligned_client):
    client = aligned_client
    cid = campaign(client)
    turn = {"idempotency_key":str(uuid.uuid4()), "event":{"title":"Rescue", "summary":"Elara risks her life to rescue the children."},
            "character_changes":{"name":"Elara", "level":2}}
    first = client.post(f"/api/campaigns/{cid}/turns", json=turn)
    assert first.status_code == 200
    state = wait_review(client, cid)
    assert state["history"][0]["result"]["observations"][0]["event_id"] == first.json()["event_id"]
    assert client.post(f"/api/campaigns/{cid}/turns", json=turn).json()["duplicate"]
    assert len(private(client, cid)["history"]) == 1
    assert len(client.actions.action_calls) == 1


def test_player_cannot_choose_alignment_and_bad_turns_do_not_queue_review(aligned_client):
    client = aligned_client
    cid = campaign(client)
    assert level(client, cid, 2, alignment="lawful evil").status_code == 422
    for changes, phases in (({"alignment":"lawful evil", "level":2}, []), ({"level":2}, [{"phase_id":"missing", "status":"active"}])):
        response = client.post(f"/api/campaigns/{cid}/turns", json={"event":{"title":"Invalid turn", "summary":"No changes should persist."},
            "character_changes":changes, "phase_updates":phases})
        assert response.status_code == 422
    response = client.put(f"/api/campaigns/{cid}/character?expected_revision=999", json={"level":2})
    assert response.status_code == 409
    assert not private(client, cid)["history"]
    public = client.get(f"/api/campaigns/{cid}").json()
    assert public["character"]["level"] == 1 and not public["events"]


def test_milestones_capture_action_cutoffs_and_finish_in_order(aligned_client):
    client = aligned_client
    cid = campaign(client)
    first = event(client, cid, "Before level two")
    client.actions.release.clear()
    try:
        level(client, cid, 2)
        assert client.actions.entered.wait(3)
        second = event(client, cid, "After level two")
        level(client, cid, 3)
        assert len(client.actions.action_calls) == 1
    finally:
        client.actions.release.set()
    state = wait_review(client, cid, 3)
    assert [item["level"] for item in state["history"]] == [2, 3]
    assert [item["result"]["observations"][0]["event_id"] for item in state["history"]] == [first, second]
    assert state["action_count"] == 2


@pytest.mark.parametrize("invalid", ["foreign", "duplicate", "score", "second_batch"])
def test_invalid_evidence_retains_state_and_can_retry_without_double_counting(aligned_client, invalid):
    client = aligned_client
    cid = campaign(client)
    client.actions.invalid = invalid
    for number in range(17 if invalid == "second_batch" else 1):
        event(client, cid, f"Deed {number}")
    level(client, cid, 2)
    failed = wait_review(client, cid)
    assert failed["history"][0]["status"] == "failed"
    assert failed["label"] == "undetermined" and failed["action_count"] == 0
    assert client.get(f"/api/campaigns/{cid}").json()["character"]["level"] == 2
    client.actions.invalid = None
    assert client.post(f"/api/campaigns/{cid}/alignment/retry").json()["queued"]
    state = wait_review(client, cid)
    assert state["history"][0]["status"] == "ready"
    assert state["action_count"] == (17 if invalid == "second_batch" else 1)
    assert len(state["history"]) == 1


def test_all_events_are_reviewed_without_books_or_identity_traits(aligned_client):
    client = aligned_client
    cid = campaign(client, b"SECRET_FUTURE_PLAN: a dragon will attack the city.")
    expected = {f"Action {number}" for number in range(35)}
    for title in sorted(expected):
        event(client, cid, title)
    level(client, cid, 2, ancestry="SECRET_ANCESTRY", class_name="SECRET_CLASS", backstory="SECRET_BACKSTORY")
    state = wait_review(client, cid)
    assert state["action_count"] == 35 and len(client.actions.action_calls) == 3
    reviewed = {item["title"] for call in client.actions.action_calls for item in call["events"]}
    assert expected == reviewed
    assert "SECRET_" not in json.dumps(client.actions.action_calls)


def test_no_action_evidence_preserves_previous_tendency(aligned_client):
    client = aligned_client
    cid = campaign(client)
    event(client, cid)
    level(client, cid, 2)
    wait_review(client, cid)
    client.actions.invalid = "none"
    event(client, cid, "Ambiguous event")
    level(client, cid, 3)
    state = wait_review(client, cid, 3)
    assert state["label"] == "neutral good" and state["action_count"] == 1
    assert not state["history"][-1]["result"]["observations"]


def test_empty_log_needs_no_model_and_remains_undetermined(aligned_client):
    client = aligned_client
    cid = campaign(client)
    client.actions.ready = False
    level(client, cid, 5)
    state = wait_review(client, cid, 5)
    assert state["label"] == "undetermined" and not client.actions.action_calls
    assert state["reviewed_level"] == 5 and state["history"][0]["status"] == "ready"


def test_unfinished_review_resumes_after_restart(tmp_path):
    settings, provider = Settings(data_dir=tmp_path), ActionPlanner()
    with TestClient(create_app(settings, TestEmbeddings(), provider)) as client:
        cid = campaign(client)
        event(client, cid)
        provider.ready = False
        level(client, cid, 2)
        assert wait_review(client, cid)["history"][0]["status"] == "needs_setup"
        event(client, cid, "Not part of the earlier level-up")
    with TestClient(create_app(settings, TestEmbeddings(), ActionPlanner())) as client:
        state = wait_review(client, cid)
        assert state["label"] == "neutral good" and state["action_count"] == 1
        assert len(state["history"]) == 1


def test_legacy_alignment_migrates_privately_without_creating_a_level_up(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings, TestEmbeddings(), ActionPlanner())) as client:
        cid = campaign(client)
        with client.app.state.library.db.connect() as db:
            db.execute("DELETE FROM character_alignment WHERE campaign_id = ?", (cid,))
            sheet = {**client.get(f"/api/campaigns/{cid}").json()["character"], "level":7, "alignment":"Chaotic Neutral", "dm_notes":""}
            db.execute("UPDATE campaigns SET character_json = ? WHERE id = ?", (json.dumps(sheet), cid))
    with TestClient(create_app(settings, TestEmbeddings(), ActionPlanner())) as client:
        state = private(client, cid)
        assert state["label"] == "chaotic neutral" and state["highest_level"] == 7
        assert not state["history"]
        assert "alignment" not in client.get(f"/api/campaigns/{cid}").text
        assert client.get(f"/api/campaigns/{cid}").json()["character_revision"] == 0
        client.app.state.campaigns.initialize()
        assert private(client, cid) == state


@pytest.mark.skipif(os.getenv("DM_TEST_OLLAMA") != "1", reason="Set DM_TEST_OLLAMA=1 with the configured local model running")
def test_real_local_model_reviews_character_actions(tmp_path):
    provider = OllamaPlanner(Settings.from_env())
    assert provider.status()["ready"], "Start the configured local Ollama model first"
    # Plan generation is already covered separately; this test runs the real action reviewer.
    with TestClient(create_app(Settings(data_dir=tmp_path), TestEmbeddings(), TestPlanner())) as client:
        cid = campaign(client)
        client.app.state.alignment.provider = provider
        deed = event(client, cid)
        npc = client.post(f"/api/campaigns/{cid}/events", json={"title":"A distant villain",
            "summary":"The villain burns an empty outpost. Elara is elsewhere and takes no action.", "player_visible":False}).json()["event_id"]
        assert level(client, cid, 2).status_code == 200
        until = time.monotonic() + 180
        while time.monotonic() < until:
            state = private(client, cid)
            if state["history"][0]["status"] in {"ready", "failed", "needs_setup"}:
                break
            time.sleep(.1)
        review = state["history"][0]
        assert review["status"] == "ready", review
        observations = review["result"]["observations"]
        assert deed in {item["event_id"] for item in observations}
        assert npc not in {item["event_id"] for item in observations}
        assert state["label"].endswith("good"), state
        assert "alignment" not in client.get(f"/api/campaigns/{cid}").text

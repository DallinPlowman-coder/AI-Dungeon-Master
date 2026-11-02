import io
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from conftest import TestEmbeddings
from dungeon_master.config import Settings
from dungeon_master.main import create_app
from test_campaign_records import TestPlanner, campaign, campaign_client


def request_roll(client, cid, **values):
    response = client.post(f"/api/campaigns/{cid}/rolls", json={"label":"Dexterity check", "reason":"Cross the narrow bridge.", **values})
    assert response.status_code == 201, response.text
    return response.json()["roll"]


def press(client, cid, rid):
    return client.post(f"/api/campaigns/{cid}/rolls/{rid}/roll", json={})


def test_roll_waits_for_player_press_and_cannot_accept_client_values(campaign_client, monkeypatch):
    client = campaign_client
    cid = campaign(client)
    calls = []
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: calls.append(n) or 12)
    roll = request_roll(client, cid, modifier=3)
    assert roll["status"] == "pending" and roll["result"] is None
    for _ in range(3):
        assert client.get(f"/api/campaigns/{cid}").json()["rolls"][0]["result"] is None
        client.get(f"/api/campaigns/{cid}/dm-context")
    assert not calls
    assert not client.get(f"/api/campaigns/{cid}").json()["events"]
    endpoint = f"/api/campaigns/{cid}/rolls/{roll['id']}/roll"
    assert client.post(endpoint, json={"value":20}).status_code == 422
    assert client.post(endpoint, json={"modifier":30}).status_code == 422
    assert not calls
    result = press(client, cid, roll["id"])
    assert result.status_code == 200
    assert result.json()["roll"]["result"]["value"] == 13
    assert result.json()["roll"]["result"]["total"] == 16
    assert calls == [20]


@pytest.mark.parametrize("random_value,modifier,expected", [(0, 3, 4), (19, 3, 23), (0, -5, -4)])
def test_d20_boundaries_and_modifier_do_not_adjudicate_character_stats(campaign_client, monkeypatch, random_value, modifier, expected):
    client = campaign_client
    cid = campaign(client)
    before = client.get(f"/api/campaigns/{cid}").json()
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: random_value)
    roll = request_roll(client, cid, modifier=modifier)
    data = press(client, cid, roll["id"]).json()["roll"]
    assert data["result"]["value"] == random_value + 1
    assert data["result"]["total"] == expected
    after = client.get(f"/api/campaigns/{cid}").json()
    assert after["character"] == before["character"]
    assert after["character_revision"] == before["character_revision"]
    assert after["events"][0]["id"] == data["result"]["event_id"]
    assert after["events"][0]["kind"] == "roll"


def test_concurrent_presses_and_network_retries_return_one_result(campaign_client, monkeypatch):
    client = campaign_client
    cid = campaign(client)
    calls = []
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: calls.append(n) or 19)
    roll = request_roll(client, cid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: press(client, cid, roll["id"]), range(8)))
    assert all(item.status_code == 200 for item in responses)
    results = [item.json() for item in responses]
    assert sum(not item["duplicate"] for item in results) == 1
    assert all(item["roll"]["result"] == results[0]["roll"]["result"] for item in results)
    assert calls == [20]
    # A later request with no body is the same player action, not a fresh die.
    later = client.post(f"/api/campaigns/{cid}/rolls/{roll['id']}/roll")
    assert later.json()["duplicate"] and calls == [20]
    assert len(client.get(f"/api/campaigns/{cid}").json()["events"]) == 1


def test_request_retries_are_deduplicated_and_conflicting_reuse_is_rejected(campaign_client):
    client = campaign_client
    cid = campaign(client)
    key = str(uuid.uuid4())
    first = request_roll(client, cid, idempotency_key=key)
    again = request_roll(client, cid, idempotency_key=key)
    assert again["id"] == first["id"]
    assert client.post(f"/api/campaigns/{cid}/rolls", json={"label":"Different check", "idempotency_key":key}).status_code == 409
    for bad in ({"modifier":31}, {"modifier":True}, {"modifier":2.5}, {"label":" "}, {"sides":100}):
        assert client.post(f"/api/campaigns/{cid}/rolls", json={"label":"Check", **bad}).status_code == 422
    assert len(client.get(f"/api/campaigns/{cid}").json()["rolls"]) == 1


def test_dm_turn_queues_roll_buttons_atomically_without_rolling(campaign_client, monkeypatch):
    client = campaign_client
    cid = campaign(client)
    calls = []
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: calls.append(n) or 1)
    key = str(uuid.uuid4())
    payload = {"idempotency_key":key, "event":{"title":"A narrow bridge", "summary":"A bridge crosses the chasm."},
               "roll_requests":[{"label":"Dexterity check", "modifier":3}, {"label":"Perception check", "modifier":2}]}
    first = client.post(f"/api/campaigns/{cid}/turns", json=payload)
    assert first.status_code == 200
    assert client.post(f"/api/campaigns/{cid}/turns", json=payload).json()["duplicate"]
    public = client.get(f"/api/campaigns/{cid}").json()
    assert len(public["rolls"]) == 2 and len(public["events"]) == 1 and not calls
    assert all(item["status"] == "pending" and "source_event_id" not in item for item in public["rolls"])
    private = client.get(f"/api/campaigns/{cid}/dm-context").json()
    assert all(item["source_event_id"] == first.json()["event_id"] for item in private["rolls"])
    assert "roll_requests" in private["memory_instruction"]
    press(client, cid, public["rolls"][0]["id"])
    assert client.get(f"/api/campaigns/{cid}").json()["rolls"][1]["status"] == "pending"


def test_invalid_turn_or_conflicting_roll_request_rolls_back_everything(campaign_client):
    client = campaign_client
    cid = campaign(client)
    key = str(uuid.uuid4())
    request_roll(client, cid, idempotency_key=key)
    for extra in ({"phase_updates":[{"phase_id":"missing", "status":"active"}]},
                  {"character_changes":{"hp_current":999}},
                  {"roll_requests":[{"label":"Would be new"}, {"label":"Conflict", "idempotency_key":key}]}):
        payload = {"event":{"title":"Bad turn", "summary":"This must not be saved."}, "roll_requests":[{"label":"Pending check"}], **extra}
        assert client.post(f"/api/campaigns/{cid}/turns", json=payload).status_code == 422
    public = client.get(f"/api/campaigns/{cid}").json()
    assert not public["events"] and len(public["rolls"]) == 1
    assert public["character_revision"] == 0


def test_rolls_cannot_be_resolved_from_another_campaign(campaign_client, monkeypatch):
    client = campaign_client
    first, second = campaign(client), campaign(client)
    roll = request_roll(client, first)
    calls = []
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: calls.append(n) or 0)
    assert press(client, second, roll["id"]).status_code == 404
    assert press(client, first, str(uuid.uuid4())).status_code == 404
    assert not calls
    assert not client.get(f"/api/campaigns/{second}").json()["rolls"]


def test_failed_log_write_cannot_commit_an_unlogged_roll(campaign_client, monkeypatch):
    client = campaign_client
    cid = campaign(client)
    roll = request_roll(client, cid)
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: 11)
    with client.app.state.library.db.connect() as db:
        db.execute("""CREATE TRIGGER reject_dice_event BEFORE INSERT ON campaign_events
            WHEN NEW.idempotency_key LIKE 'd20:%' BEGIN SELECT RAISE(ABORT, 'Test log failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        press(client, cid, roll["id"])
    state = client.get(f"/api/campaigns/{cid}").json()
    assert state["rolls"][0]["status"] == "pending" and not state["events"]
    with client.app.state.library.db.connect() as db:
        db.execute("DROP TRIGGER reject_dice_event")
    assert press(client, cid, roll["id"]).json()["roll"]["result"]["value"] == 12
    assert len(client.get(f"/api/campaigns/{cid}").json()["events"]) == 1


@pytest.mark.parametrize("file_format", ["md", "json", "pdf"])
def test_roll_results_are_in_player_logs_without_hidden_context(campaign_client, monkeypatch, file_format):
    client = campaign_client
    cid = campaign(client)
    client.post(f"/api/campaigns/{cid}/events", json={"title":"Arrival", "summary":"At the bridge.", "session":4, "world_time":"Evening"})
    client.post(f"/api/campaigns/{cid}/turns", json={"event":{"title":"SECRET_PLOT", "summary":"SECRET_MOTIVE", "world_time":"SECRET_TIME", "player_visible":False},
        "roll_requests":[{"label":"Balance check", "reason":"Cross the bridge.", "modifier":3}]})
    roll = client.get(f"/api/campaigns/{cid}").json()["rolls"][0]
    assert "SECRET_" not in json.dumps(roll)
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: 9)
    press(client, cid, roll["id"])
    state = client.get(f"/api/campaigns/{cid}").json()
    assert state["events"][-1]["session"] == 4 and state["events"][-1]["world_time"] == "Evening"
    response = client.get(f"/api/campaigns/{cid}/documents/log.{file_format}")
    assert response.status_code == 200
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages) if file_format == "pdf" else response.text
    assert "d20 10 +3 = 13" in text and "SECRET_" not in text


def test_pending_and_completed_rolls_survive_restart(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: 6)
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        cid = campaign(client)
        done = request_roll(client, cid)
        pending = request_roll(client, cid)
        result = press(client, cid, done["id"]).json()["roll"]["result"]
    calls = []
    monkeypatch.setattr("dungeon_master.dice.secrets.randbelow", lambda n: calls.append(n) or 2)
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        state = client.get(f"/api/campaigns/{cid}").json()
        assert state["rolls"][0]["result"] == result and state["rolls"][1]["status"] == "pending"
        assert press(client, cid, done["id"]).json()["roll"]["result"] == result and not calls
        assert press(client, cid, pending["id"]).json()["roll"]["result"]["value"] == 3
        assert calls == [20]

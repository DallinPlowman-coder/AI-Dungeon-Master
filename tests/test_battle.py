import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import TestEmbeddings
from dungeon_master.config import Settings
from dungeon_master.battle_models import BattleState
from dungeon_master.main import create_app
from test_campaign_records import TestPlanner, campaign, campaign_client


def board():
    return {"title":"A simple encounter", "map":{"width":10,"height":10,
        "terrain":[{"x":3,"y":3,"kind":"obstacle"}, {"x":4,"y":4,"kind":"water"}],
        "walls":[{"x1":5,"y1":0,"x2":5,"y2":10}],
        "doors":[{"id":"door_1","x1":5,"y1":4,"x2":5,"y2":5}]},
        "tokens":[{"id":"pc_1","name":"Kanedias","faction":"player","x":4,"y":8},
                  {"id":"enemy_1","name":"Goblin 1","faction":"enemy","x":3,"y":2},
                  {"id":"enemy_2","name":"Goblin 1","faction":"enemy","x":6,"y":2}],
        "combat":{"active":True,"round":1,"active_token_id":"pc_1"}}


def create_board(client, cid, state=None):
    response = client.put(f"/api/campaigns/{cid}/battle", json={"expected_revision":0,"state":state or board()})
    assert response.status_code == 200, response.text
    return response.json()


def test_structured_board_and_duplicate_display_names_are_preserved(campaign_client):
    client = campaign_client
    cid = campaign(client)
    assert client.get(f"/api/campaigns/{cid}/battle").json() == {"state":None,"revision":0,"updated_at":None}
    state = create_board(client, cid)
    assert state["revision"] == 1 and state["state"]["map"]["feet_per_cell"] == 5
    assert state["state"]["tokens"][1]["name"] == state["state"]["tokens"][2]["name"]
    assert state["state"]["tokens"][1]["id"] != state["state"]["tokens"][2]["id"]
    assert client.get(f"/api/campaigns/{cid}").json()["battle"] == state
    assert client.get(f"/api/campaigns/{cid}/dm-context").json()["battle"] == state


def test_backend_can_spawn_move_remove_update_doors_and_combat(campaign_client):
    client = campaign_client
    cid = campaign(client)
    create_board(client, cid)
    response = client.patch(f"/api/campaigns/{cid}/battle", json={"expected_revision":1,
        "move_tokens":[{"id":"pc_1","x":4,"y":7}],
        "spawn_tokens":[{"id":"guard","name":"Guard","faction":"ally","x":2,"y":7}],
        "remove_tokens":["enemy_2"], "door_updates":[{"id":"door_1","open":True}],
        "token_updates":[{"id":"enemy_1","hp_current":2,"hp_max":7,"conditions":["Prone"],"movement_remaining":10}],
        "combat":{"active":True,"round":2,"active_token_id":"guard"}})
    assert response.status_code == 200, response.text
    data = response.json()
    tokens = {item["id"]:item for item in data["state"]["tokens"]}
    assert data["revision"] == 2 and (tokens["pc_1"]["x"],tokens["pc_1"]["y"]) == (4,7)
    assert "enemy_2" not in tokens and tokens["guard"]["faction"] == "ally"
    assert tokens["enemy_1"]["conditions"] == ["Prone"]
    assert data["state"]["map"]["doors"][0]["open"]
    assert data["state"]["combat"]["active_token_id"] == "guard"
    removed = client.patch(f"/api/campaigns/{cid}/battle", json={"expected_revision":2,"remove_tokens":["guard"]})
    assert removed.status_code == 200 and removed.json()["state"]["combat"]["active_token_id"] is None


@pytest.mark.parametrize("changes", [
    {"move_tokens":[{"id":"pc_1","x":10,"y":0}]},
    {"move_tokens":[{"id":"pc_1","x":3,"y":3}]},
    {"move_tokens":[{"id":"pc_1","x":3,"y":2}]},
    {"move_tokens":[{"id":"missing","x":1,"y":1}]},
    {"move_tokens":[{"id":"pc_1","x":1.5,"y":1}]},
    {"spawn_tokens":[{"id":"pc_1","name":"Other","x":1,"y":1}]},
    {"remove_tokens":["missing"]},
    {"door_updates":[{"id":"missing","open":True}]},
    {"combat":{"active_token_id":"missing"}},
    {"token_updates":[{"id":"enemy_1","hp_current":10,"hp_max":7}]},
    {"map":{"width":3,"height":3}},
])
def test_invalid_edits_do_not_modify_positions_or_revision(campaign_client, changes):
    client = campaign_client
    cid = campaign(client)
    before = create_board(client, cid)
    response = client.patch(f"/api/campaigns/{cid}/battle", json={"expected_revision":1,**changes})
    assert response.status_code == 422, response.text
    assert client.get(f"/api/campaigns/{cid}/battle").json() == before


@pytest.mark.parametrize("mutate", [
    lambda data:data["tokens"].append(copy.deepcopy(data["tokens"][0])),
    lambda data:data["map"].update(walls=[{"x1":1,"y1":1,"x2":2,"y2":2}]),
    lambda data:data["map"].update(doors=[{"id":"wide","x1":0,"y1":0,"x2":2,"y2":0}]),
    lambda data:data["map"].update(terrain=[{"x":10,"y":0,"kind":"water"}]),
    lambda data:data["tokens"][0].update(id='bad" id'),
    lambda data:data["tokens"][0].update(portrait="https://example.com/token.png"),
])
def test_invalid_state_is_rejected_before_creation(campaign_client, mutate):
    client = campaign_client
    cid = campaign(client)
    state = board(); mutate(state)
    assert client.put(f"/api/campaigns/{cid}/battle", json={"expected_revision":0,"state":state}).status_code == 422
    assert client.get(f"/api/campaigns/{cid}/battle").json()["state"] is None


def test_concurrent_updates_use_revision_checks(campaign_client):
    client = campaign_client
    cid = campaign(client)
    create_board(client, cid)
    def move(y):
        return client.patch(f"/api/campaigns/{cid}/battle", json={"expected_revision":1,"move_tokens":[{"id":"pc_1","x":4,"y":y}]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(move, [6,7]))
    assert sorted(response.status_code for response in results) == [200,409]
    assert client.get(f"/api/campaigns/{cid}/battle").json()["revision"] == 2


def test_turn_updates_are_atomic_and_retries_do_not_repeat_moves(campaign_client):
    client = campaign_client
    cid = campaign(client)
    create_board(client, cid)
    payload = {"idempotency_key":"3231bf47-385b-4ff4-a149-360199b49600",
        "event":{"title":"Reposition", "summary":"Kanedias steps back."},
        "character_changes":{"hp_max":10,"hp_current":8},
        "battle_update":{"expected_revision":1,"move_tokens":[{"id":"pc_1","x":4,"y":7}]}}
    assert client.post(f"/api/campaigns/{cid}/turns", json=payload).status_code == 200
    assert client.post(f"/api/campaigns/{cid}/turns", json=payload).json()["duplicate"]
    before = client.get(f"/api/campaigns/{cid}").json()
    assert before["battle"]["revision"] == 2 and before["character"]["hp_current"] == 8
    bad = {"event":{"title":"Invalid move", "summary":"Must roll back."}, "character_changes":{"hp_current":7},
           "roll_requests":[{"label":"Should not appear"}],
           "battle_update":{"expected_revision":1,"remove_tokens":["enemy_1"]}}
    assert client.post(f"/api/campaigns/{cid}/turns", json=bad).status_code == 409
    assert client.get(f"/api/campaigns/{cid}").json() == before


def test_linked_character_details_use_canonical_sheet_and_never_private_notes(campaign_client):
    client = campaign_client
    cid = campaign(client)
    client.put(f"/api/campaigns/{cid}/character", json={"name":"Kanedias","hp_max":20,"hp_current":12,"conditions":["Prone"],"dm_notes":"SECRET_BACKGROUND"})
    data = board(); data["tokens"][0].update(character_ref="player",name="Stale name",hp_current=1,hp_max=2)
    state = create_board(client, cid, data)
    token = state["state"]["tokens"][0]
    assert token["name"] == "Kanedias" and token["hp_current"] == 12 and token["hp_max"] == 20 and token["conditions"] == ["Prone"]
    assert "SECRET_" not in str(state)
    client.put(f"/api/campaigns/{cid}/character", json={"name":"Renamed","hp_max":20,"hp_current":10})
    updated = client.get(f"/api/campaigns/{cid}/battle").json()
    assert updated["revision"] == 1 and updated["state"]["tokens"][0]["name"] == "Renamed"
    assert updated["state"]["tokens"][0]["hp_current"] == 10


def test_linked_sheet_limits_can_round_trip_as_structured_state(campaign_client):
    client = campaign_client
    cid = campaign(client)
    sheet = {"name":"K" * 160, "conditions":["C" * 160 for _ in range(30)]}
    assert client.put(f"/api/campaigns/{cid}/character", json=sheet).status_code == 200
    data = board(); data["tokens"][0]["character_ref"] = "player"
    saved = create_board(client, cid, data)
    BattleState.model_validate(saved["state"])
    assert client.put(f"/api/campaigns/{cid}/battle", json={"expected_revision":1,"state":saved["state"]}).status_code == 200


@pytest.mark.parametrize("width,height", [(10,10),(15,12),(30,30)])
def test_starter_encounter_requires_no_manual_map_building(campaign_client, width, height):
    client = campaign_client
    cid = campaign(client)
    response = client.post(f"/api/campaigns/{cid}/battle/starter", json={"expected_revision":0,"width":width,"height":height})
    assert response.status_code == 200, response.text
    data = response.json()["state"]
    assert data["map"]["width"] == width and data["map"]["height"] == height
    assert {token["faction"] for token in data["tokens"]} == {"player","ally","enemy","neutral"}
    assert {tile["kind"] for tile in data["map"]["terrain"]} == {"obstacle","water","hazard","difficult"}


def test_campaign_isolation_and_restart_preserve_battle(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        first, second = campaign(client), campaign(client)
        expected = create_board(client, first)
        assert client.get(f"/api/campaigns/{second}/battle").json()["state"] is None
        assert client.patch(f"/api/campaigns/{second}/battle", json={"expected_revision":0,"remove_tokens":["pc_1"]}).status_code == 422
    with TestClient(create_app(settings, TestEmbeddings(), TestPlanner())) as client:
        assert client.get(f"/api/campaigns/{first}/battle").json() == expected
        assert client.get(f"/api/campaigns/{second}/battle").json()["state"] is None

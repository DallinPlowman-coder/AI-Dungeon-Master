import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import TestEmbeddings, wait_for_book
from dungeon_master.config import Settings
from dungeon_master.main import create_app
from test_campaign_records import TestPlanner, campaign, campaign_client


def rulebook(client, title, text=None, collection="rules"):
    response = client.post("/api/books", data={"collection":collection}, files={"file":(f"{title}.txt", text or f"{title}: a shield protects the traveler from danger.".encode())})
    book = wait_for_book(client, response.json()["book"]["id"])
    assert book["status"] == "ready"
    return book["id"]


def group(client, ids, name="Core rules"):
    response = client.post("/api/rule-sets", json={"name":name,"book_ids":ids})
    assert response.status_code == 201, response.text
    return response.json()


def test_named_sets_share_books_without_reindexing_and_can_be_edited(campaign_client):
    client = campaign_client
    first, second = rulebook(client,"Player handbook"), rulebook(client,"Monster book")
    chunks = [client.get(f"/api/books/{bid}/chunks").json() for bid in [first,second]]
    saved = group(client,[first,second])
    another = group(client,[second],"Monster reference")
    assert saved["book_ids"] == [first,second] and saved["ready"] and saved["revision"] == 1
    assert another["book_ids"] == [second]
    assert len(client.get("/api/rule-sets").json()["rule_sets"]) == 2
    edited = client.put(f"/api/rule-sets/{saved['id']}", json={"expected_revision":1,"name":"Travel rules","book_ids":[first]})
    assert edited.status_code == 200 and edited.json()["revision"] == 2 and edited.json()["book_ids"] == [first]
    assert [client.get(f"/api/books/{bid}/chunks").json() for bid in [first,second]] == chunks
    assert client.put(f"/api/rule-sets/{saved['id']}", json={"expected_revision":1,"name":"Stale edit","book_ids":[second]}).status_code == 409
    assert client.get(f"/api/rule-sets/{saved['id']}").json() == edited.json()


def test_invalid_members_names_duplicates_and_cross_origin_writes_are_rejected(campaign_client):
    client = campaign_client
    first = rulebook(client,"Player handbook")
    adventure = rulebook(client,"Adventure",collection="campaign")
    for payload in [{"name":"", "book_ids":[first]}, {"name":"   ","book_ids":[first]},
                    {"name":"Empty", "book_ids":[]}, {"name":"Repeated", "book_ids":[first,first]},
                    {"name":"Wrong collection", "book_ids":[adventure]},
                    {"name":"Missing", "book_ids":[str(uuid.uuid4())]}]:
        assert client.post("/api/rule-sets", json=payload).status_code == 422
    saved = group(client,[first])
    assert client.post("/api/rule-sets", json={"name":"CORE RULES","book_ids":[first]}).status_code == 409
    assert client.post("/api/rule-sets", json={"name":"Other","book_ids":[first]},headers={"Origin":"https://example.com"}).status_code == 403
    assert client.get(f"/api/rule-sets/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/api/rule-sets/{saved['id']}").json()["revision"] == 1


def test_set_search_filters_semantic_and_keyword_candidates_before_ranking(campaign_client):
    client = campaign_client
    first = rulebook(client,"Player handbook",b"Movement costs five feet. Shield armor grants protection.")
    second = rulebook(client,"Monster book",b"A monster attacks the shield and moves across the bridge.")
    excluded = rulebook(client,"Other edition",b"Shield shield shield: excluded rules and different movement costs.")
    rulebook(client,"Adventure",b"Shield shield shield: campaign secret.","campaign")
    saved = group(client,[first,second])
    for query in ["shield", "movement across the bridge"]:
        result = client.post("/api/retrieve", json={"query":query,"rule_set_id":saved["id"],"limit":10})
        assert result.status_code == 200
        matches = result.json()["matches"]
        assert {item["book_id"] for item in matches} == {first,second}
        assert all(item["collection"] == "rules" for item in matches)
        assert "excluded rules" not in result.json()["context"] and "campaign secret" not in result.json()["context"]
    db = client.app.state.library.db
    semantic_ids = {row["book_id"] for batch in db.vector_batches(TestEmbeddings.name, book_ids=[first,second]) for row in batch}
    lexical_ids = {row["book_id"] for row in db.lexical_search('"shield"',TestEmbeddings.name,book_ids=[first,second])}
    assert semantic_ids == lexical_ids == {first,second}
    assert client.post("/api/retrieve", json={"query":"shield", "rule_set_id":saved["id"],"book_id":excluded}).status_code == 400
    assert client.post("/api/retrieve", json={"query":"shield", "rule_set_id":saved["id"],"collection":"campaign"}).status_code == 400


def test_campaign_selects_set_and_retrieves_only_its_rules_and_own_adventure(campaign_client):
    client = campaign_client
    cid = campaign(client)
    first = rulebook(client,"Player handbook")
    second = rulebook(client,"Other edition")
    rulebook(client,"Different adventure",collection="campaign")
    saved, other = group(client,[first]), group(client,[second],"Other rules")
    assert client.get(f"/api/campaigns/{cid}").json()["rule_set"] is None
    assert client.post("/api/retrieve", json={"query":"shield","campaign_id":cid,"collection":"rules"}).json()["matches"] == []
    selected = client.put(f"/api/campaigns/{cid}/rules", json={"rule_set_id":saved["id"],"expected_rule_set_id":None})
    assert selected.status_code == 200 and selected.json()["rule_set"]["book_ids"] == [first]
    before = selected.json()
    assert client.put(f"/api/campaigns/{cid}/rules", json={"rule_set_id":other["id"],"expected_rule_set_id":None}).status_code == 409
    assert client.get(f"/api/campaigns/{cid}").json() == before
    context = client.get(f"/api/campaigns/{cid}/dm-context").json()
    assert context["rule_set"]["id"] == saved["id"] and context["retrieval_scope"] == {"campaign_id":cid}
    result = client.post("/api/retrieve", json={"query":"shield shrine","campaign_id":cid,"limit":10}).json()
    assert {item["book_id"] for item in result["matches"]} == {first,before["source_book_id"]}
    assert client.post("/api/retrieve",json={"query":"shield","campaign_id":cid,"rule_set_id":saved["id"]}).status_code == 400
    assert client.put(f"/api/campaigns/{cid}/rules", json={"rule_set_id":None,"expected_rule_set_id":saved["id"]}).status_code == 200
    assert client.post("/api/retrieve", json={"query":"shield","campaign_id":cid,"collection":"rules"}).json()["matches"] == []


def test_new_campaign_can_select_a_set_and_existing_selection_tracks_set_edits(campaign_client):
    client = campaign_client
    cid = campaign(client)
    source = client.get(f"/api/campaigns/{cid}").json()["source_book_id"]
    first, second = rulebook(client,"Player handbook"), rulebook(client,"House rules")
    saved = group(client,[first])
    created = client.post("/api/campaigns", json={"name":"Grouped rules campaign", "source_book_id":source,"rule_set_id":saved["id"]})
    assert created.status_code == 201 and created.json()["rule_set_id"] == saved["id"]
    new_id = created.json()["id"]
    edited = client.put(f"/api/rule-sets/{saved['id']}",json={"expected_revision":1,"name":"Core + homebrew","book_ids":[first,second]})
    assert edited.status_code == 200
    assert client.get(f"/api/campaigns/{new_id}").json()["rule_set"]["book_ids"] == [first,second]
    summary = next(item for item in client.get("/api/campaigns").json()["campaigns"] if item["id"] == new_id)
    assert summary["rule_set_name"] == "Core + homebrew" and summary["rule_set_revision"] == 2
    assert client.delete(f"/api/campaigns/{new_id}").status_code == 200
    assert client.get(f"/api/rule-sets/{saved['id']}").status_code == 200


@pytest.mark.parametrize("change", ["delete","move","reindex"])
def test_unavailable_or_empty_set_never_falls_back_to_other_rules(campaign_client, change):
    client = campaign_client
    cid = campaign(client)
    first = rulebook(client,"Player handbook")
    rulebook(client,"Unselected rulebook")
    saved = group(client,[first])
    assert client.put(f"/api/campaigns/{cid}/rules",json={"expected_rule_set_id":None,"rule_set_id":saved["id"]}).status_code == 200
    if change == "delete":
        assert client.delete(f"/api/books/{first}").status_code == 200
    elif change == "move":
        assert client.patch(f"/api/books/{first}",json={"collection":"campaign"}).status_code == 200
    else:
        client.app.state.library.db.update_book(first,status="failed")
    result = client.get(f"/api/rule-sets/{saved['id']}").json()
    assert result["ready"] is False
    for scope in [{"rule_set_id":saved["id"]},{"campaign_id":cid,"collection":"rules"}]:
        assert client.post("/api/retrieve",json={"query":"shield",**scope}).status_code == 409
    assert client.put(f"/api/campaigns/{cid}/rules",json={"expected_rule_set_id":saved["id"],"rule_set_id":saved["id"]}).status_code == 409


def test_sets_and_campaign_choice_survive_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings,TestEmbeddings(),TestPlanner())) as client:
        cid = campaign(client)
        first = rulebook(client,"Player handbook")
        saved = group(client,[first])
        assert client.put(f"/api/campaigns/{cid}/rules",json={"expected_rule_set_id":None,"rule_set_id":saved["id"]}).status_code == 200
    with TestClient(create_app(settings,TestEmbeddings(),TestPlanner())) as client:
        assert client.get(f"/api/campaigns/{cid}").json()["rule_set"]["id"] == saved["id"]
        assert client.get(f"/api/rule-sets/{saved['id']}").json() == saved
        assert client.post("/api/retrieve",json={"query":"shield","campaign_id":cid,"collection":"rules"}).json()["matches"][0]["book_id"] == first

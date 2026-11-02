import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import TestEmbeddings, wait_for_book
from dungeon_master.config import Settings
from dungeon_master.main import create_app
from dungeon_master.records import CharacterSheet, GeneratedPlan, ReadingDigest
from test_campaign_records import TestPlanner, campaign, campaign_client


CAMPAIGN_TABLES = ("campaign_events", "npc_memories", "campaign_readings", "campaign_plan_history",
                   "character_alignment", "alignment_reviews", "campaign_rolls", "campaign_battles")


def test_deleting_save_removes_all_owned_records_and_keeps_books_and_other_saves(campaign_client):
    client = campaign_client
    removed, retained = campaign(client), campaign(client)
    store = client.app.state.campaigns
    store.character(removed, CharacterSheet(name="Delete me", level=2))
    client.put(f"/api/campaigns/{removed}/npcs", json={"name":"Guard"})
    roll = client.post(f"/api/campaigns/{removed}/rolls", json={"label":"Test check"}).json()
    client.post(f"/api/campaigns/{removed}/rolls/{roll['roll']['id']}/roll")
    client.post(f"/api/campaigns/{removed}/battle/starter", json={"expected_revision":0})
    other = client.get(f"/api/campaigns/{retained}").json()
    books = client.get("/api/books").json()
    with store.db.connect() as db:
        for table in CAMPAIGN_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE campaign_id = ?", (removed,)).fetchone()[0] > 0, table
    response = client.delete(f"/api/campaigns/{removed}")
    assert response.status_code == 200 and response.json() == {"deleted":True,"id":removed}
    assert client.get(f"/api/campaigns/{removed}").status_code == 404
    assert client.get(f"/api/campaigns/{removed}/dm-context").status_code == 404
    assert client.get(f"/api/campaigns/{removed}/documents/character.pdf").status_code == 404
    assert [save["id"] for save in client.get("/api/campaigns").json()["campaigns"]] == [retained]
    assert client.get(f"/api/campaigns/{retained}").json() == other
    assert client.get("/api/books").json() == books
    for book in books["books"]:
        assert client.get(f"/api/books/{book['id']}/chunks").json()["chunks"]
        assert client.app.state.library.upload_path(book).exists()
    with store.db.connect() as db:
        for table in CAMPAIGN_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE campaign_id = ?", (removed,)).fetchone()[0] == 0, table
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert client.delete(f"/api/campaigns/{removed}").status_code == 404


def test_delete_validation_and_origin_guard(campaign_client):
    client = campaign_client
    cid = campaign(client)
    assert client.delete(f"/api/campaigns/{cid}", headers={"Origin":"https://example.com"}).status_code == 403
    assert client.get(f"/api/campaigns/{cid}").status_code == 200
    assert client.delete("/api/campaigns/not-a-campaign-id").status_code == 422
    assert client.delete(f"/api/campaigns/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize("blocked_schema", [ReadingDigest, GeneratedPlan])
def test_in_progress_planning_cannot_restore_deleted_records(tmp_path, blocked_schema, caplog):
    entered, release = threading.Event(), threading.Event()

    class BlockingPlanner(TestPlanner):
        def generate(self, instruction, material, schema):
            if schema is blocked_schema:
                entered.set()
                assert release.wait(5)
            return super().generate(instruction, material, schema)

    with TestClient(create_app(Settings(data_dir=tmp_path), TestEmbeddings(), BlockingPlanner())) as client:
        book = client.post("/api/books", files={"file":("adventure.txt", b"Mara guards a shrine.")}).json()["book"]
        wait_for_book(client, book["id"])
        cid = client.post("/api/campaigns", json={"name":"In progress", "source_book_id":book["id"]}).json()["id"]
        try:
            assert entered.wait(3)
            assert client.delete(f"/api/campaigns/{cid}").status_code == 200
        finally:
            release.set()
        for _ in range(300):
            with client.app.state.planning.lock:
                pending = cid in client.app.state.planning.pending
            if not pending:
                break
            time.sleep(.01)
        assert not pending
        assert client.get("/api/campaigns").json()["campaigns"] == []
        with client.app.state.campaigns.db.connect() as db:
            for table in CAMPAIGN_TABLES:
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE campaign_id = ?", (cid,)).fetchone()[0] == 0
        assert "Campaign planning failed" not in caplog.text


def test_late_alignment_review_does_not_restore_deleted_state(campaign_client):
    client = campaign_client
    cid = campaign(client)
    store = client.app.state.campaigns
    store.character(cid, CharacterSheet(name="Traveler", level=2))
    job = store.alignment.next(cid)
    assert job is not None
    assert client.delete(f"/api/campaigns/{cid}").status_code == 200
    store.alignment.finish(job, [], "test-model")
    assert store.alignment.next(cid) is None
    assert store.get(cid) is None

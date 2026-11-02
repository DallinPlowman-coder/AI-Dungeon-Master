from test_campaign_records import campaign, campaign_client


def test_save_file_summaries_are_public_and_sorted_by_last_saved(campaign_client):
    client = campaign_client
    first, second = campaign(client), campaign(client)
    client.put(f"/api/campaigns/{first}/character", json={"name":"Kanedias", "class_name":"Wizard", "level":1,
        "notes":"PRIVATE_CHARACTER_DETAIL", "dm_notes":"SECRET_CHARACTER_DETAIL"})
    client.post(f"/api/campaigns/{first}/events", json={"title":"SECRET_EVENT", "summary":"SECRET_HISTORY", "player_visible":False})
    response = client.get("/api/campaigns")
    saves = response.json()["campaigns"]
    assert saves[0]["id"] == first and saves[1]["id"] == second
    assert saves[0]["character"] == {"name":"Kanedias", "class_name":"Wizard", "level":1}
    assert saves[1]["character"]["name"] == ""
    assert "SECRET_" not in response.text and "PRIVATE_" not in response.text
    assert "plan" not in saves[0] and "alignment" not in saves[0]

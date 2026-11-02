"""Opt-in test: DM_TEST_MODEL=1 python -m pytest tests/test_real_model.py -q

Uses the real cached model in ./data/models; downloads it if missing.
"""
import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from dungeon_master.config import Settings
from dungeon_master.embeddings import LocalEmbeddings
from dungeon_master.main import create_app
from conftest import make_pdf, wait_for_book


@pytest.mark.skipif(os.getenv("DM_TEST_MODEL") != "1", reason="Opt-in local model integration test")
def test_real_embeddings_pdf_index_and_paraphrased_retrieval(tmp_path):
    embeddings = LocalEmbeddings(Settings(data_dir=Path("data").resolve()))
    embeddings.prepare()
    passages = ["A shield can block a sword strike during combat.",
                "Moonwort tea cures fever. The herbalist Pell grows the medicinal plant in a rooftop garden.",
                "The old map leads to a chamber full of gold beneath the castle."]
    with TestClient(create_app(Settings(data_dir=tmp_path), embeddings)) as client:
        uploaded = client.post("/api/books", files={"file":("original-lore.pdf", make_pdf(passages))})
        book_id = uploaded.json()["book"]["id"]
        book = wait_for_book(client, book_id, timeout=60)
        assert book["status"] == "ready", book["error"]
        assert book["chunk_count"] == 3
        with client.app.state.library.db.connect() as db:
            blob = db.execute("SELECT embedding FROM chunks LIMIT 1").fetchone()[0]
        assert len(np.frombuffer(blob, dtype="<f4")) == 384
        result = client.post("/api/retrieve", json={"query":"What remedy will help someone who is ill?", "limit":1}).json()
        assert result["matches"][0]["source_label"] == "Page 2"
        assert "Moonwort" in result["matches"][0]["text"]

import hashlib
import io
import re
import time
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from dungeon_master.config import Settings
from dungeon_master.embeddings import normalize
from dungeon_master.main import create_app


class TestEmbeddings:
    """Deterministic offline vectors for lifecycle tests; real-model tests are separate."""
    __test__ = False
    name = "test-embedding-v1"

    def prepare(self):
        pass

    def count_tokens(self, text):
        return len(re.findall(r"\w+|[^\w\s]", text)) + 2

    def query(self, text):
        vector = np.ones(32, dtype=np.float32) * .01
        for word in re.findall(r"\w+", text.lower()):
            vector[hashlib.sha256(word.encode()).digest()[0] % 32] += 1
        return normalize(vector)

    def passages(self, texts):
        return [self.query(text) for text in texts]


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), TestEmbeddings())
    with TestClient(app) as client:
        yield client


def wait_for_book(client, book_id, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        book = client.get(f"/api/books/{book_id}").json()
        if book["status"] in {"ready", "failed"}:
            return book
        time.sleep(.02)
    raise AssertionError("Book did not finish processing")


def make_pdf(pages, password=None):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 40 700 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_epub():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/content.opf"/></rootfiles></container>')
        archive.writestr("EPUB/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="two" href="two.xhtml"/><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
        archive.writestr("EPUB/one.xhtml", '<html><head><title>Introduction</title><script>BAD SCRIPT</script></head><body><h1>The Old Gate</h1><p>A silver dragon guards the northern gate.</p></body></html>')
        archive.writestr("EPUB/two.xhtml", '<html><body><h1>The Deep Well</h1><p>The well contains a hidden portal.</p></body></html>')
    return output.getvalue()

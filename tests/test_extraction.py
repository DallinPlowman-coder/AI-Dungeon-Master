import pytest

from dungeon_master.chunking import chunk_sections
from dungeon_master.extraction import BookError, Section, clean_text, extract_book
from conftest import TestEmbeddings, make_epub, make_pdf


def test_pdf_retains_physical_page_numbers_and_warns_about_scans(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(make_pdf([None, "A dragon guards the silver bridge."]))
    result = extract_book(path, "pdf", 10000)
    assert result.page_count == 2
    assert result.sections[0].source_label == "Page 2"
    assert "dragon" in result.sections[0].text
    assert "1 page(s)" in result.warnings[0]


@pytest.mark.parametrize("content,message", [
    (make_pdf([None]), "OCR"),
    (make_pdf(["Secret"], "password"), "password protected"),
    (b"not a pdf", "could not be read"),
])
def test_unusable_pdfs_have_actionable_errors(tmp_path, content, message):
    path = tmp_path / "bad.pdf"
    path.write_bytes(content)
    with pytest.raises(BookError, match=message):
        extract_book(path, "pdf", 10000)


def test_epub_uses_spine_order_and_excludes_scripts(tmp_path):
    path = tmp_path / "source.epub"
    path.write_bytes(make_epub())
    result = extract_book(path, "epub", 10000)
    assert result.page_count == 2
    assert "northern gate" in result.sections[0].text
    assert "hidden portal" in result.sections[1].text
    assert result.sections[0].source_label.startswith("Section 1")
    assert "BAD SCRIPT" not in result.sections[0].text


def test_text_encoding_and_extracted_size_limit(tmp_path):
    path = tmp_path / "source.txt"
    path.write_bytes("Moonwort heals a fever.".encode("utf-16"))
    assert "Moonwort" in extract_book(path, "txt", 1000).sections[0].text
    with pytest.raises(BookError, match="too much text"):
        extract_book(path, "txt", 5)


def test_chunking_preserves_every_word_and_each_source():
    count_tokens = TestEmbeddings().count_tokens
    words = [f"word{i}" for i in range(500)]
    chunks = chunk_sections([Section(" ".join(words), "Page 1"), Section("Secret ending.", "Page 2")],
                            count_tokens, token_limit=40, overlap_characters=30)
    assert len(chunks) > 10
    assert all(chunk.token_count <= 40 for chunk in chunks)
    assert all(word in " ".join(chunk.text for chunk in chunks).split() for word in words)
    assert chunks[-1].source_label == "Page 2"
    assert "word" not in chunks[-1].text
    assert set(chunks[0].text.split()) & set(chunks[1].text.split())


def test_long_unbroken_text_terminates_and_obeys_token_limit():
    chunks = chunk_sections([Section("x" * 2000, "Page 1")], len, token_limit=40, max_chunks=100)
    assert len(chunks) < 100
    assert all(len(chunk.text) <= 40 for chunk in chunks)


def test_chunk_limit_and_normalization():
    assert clean_text("An en-\nchanted\r\n  forest.\x00") == "An enchanted\n forest."
    with pytest.raises(BookError, match="too many passages"):
        chunk_sections([Section("x" * 3000, "Page 1")], len, token_limit=20, max_chunks=2)

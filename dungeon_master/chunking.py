import re
from dataclasses import dataclass
from typing import Callable

from .extraction import BookError, Section


@dataclass(frozen=True)
class Chunk:
    text: str
    source_label: str
    token_count: int


def chunk_sections(sections: list[Section], count_tokens: Callable[[str], int],
                   token_limit=360, overlap_characters=180, max_chunks=30_000) -> list[Chunk]:
    """Source-bounded overlapping passages that fit the actual embedding tokenizer."""
    chunks = []
    for section in sections:
        text = section.text
        start = 0
        while start < len(text):
            end = min(start + 1600, len(text))
            if count_tokens(text[start:end]) > token_limit:
                lo, hi = start + 1, end
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if count_tokens(text[start:mid]) <= token_limit:
                        lo = mid
                    else:
                        hi = mid - 1
                end = lo
            # Prefer a paragraph, sentence, or word boundary in the last third.
            if end < len(text):
                window_start = start + (end - start) * 2 // 3
                tail = text[window_start:end]
                boundaries = list(re.finditer(r"\n\n|[.!?][ \n]|\s+", tail))
                paragraphs = [match for match in boundaries if match.group().startswith("\n\n")]
                sentences = [match for match in boundaries if match.group()[0] in ".!?"]
                choices = paragraphs or sentences or boundaries
                if choices:
                    end = window_start + choices[-1].end()
            passage = text[start:end].strip()
            if passage:
                tokens = count_tokens(passage)
                if tokens > token_limit:
                    raise BookError("A passage exceeded the embedding model's token limit.")
                chunks.append(Chunk(passage, section.source_label, tokens))
                if len(chunks) > max_chunks:
                    raise BookError("This book produces too many passages. Split it into smaller volumes.")
            if end >= len(text):
                break
            # Keep overlap on a word boundary, while always making forward progress.
            overlap = min(overlap_characters, (end - start) // 5)
            next_start = max(start + 1, end - overlap)
            if next_start > start + 1:
                boundary = re.search(r"\s", text[next_start:end])
                if boundary:
                    next_start += boundary.end()
            start = next_start
    return chunks

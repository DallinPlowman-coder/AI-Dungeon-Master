import threading

import numpy as np

from .config import Settings


class LocalEmbeddings:
    """Lazy CPU embeddings. Book contents never leave this process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.name = settings.embedding_model
        self._model = None
        self._lock = threading.RLock()

    def prepare(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                cache = self.settings.data_dir / "models"
                cache.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(
                    model_name=self.name, cache_dir=str(cache), threads=2,
                )

    def count_tokens(self, text: str) -> int:
        with self._lock:
            self.prepare()
            return self._model.token_count(text)

    def passages(self, texts: list[str]) -> list[np.ndarray]:
        with self._lock:
            self.prepare()
            return [normalize(vector) for vector in self._model.passage_embed(texts, batch_size=24)]

    def query(self, text: str) -> np.ndarray:
        with self._lock:
            self.prepare()
            return normalize(next(iter(self._model.query_embed(text))))


def normalize(vector) -> np.ndarray:
    vector = np.asarray(vector, dtype="<f4")
    norm = np.linalg.norm(vector)
    if vector.ndim != 1 or not np.isfinite(vector).all() or not np.isfinite(norm) or norm == 0:
        raise ValueError("The embedding model produced an invalid vector")
    return np.asarray(vector / norm, dtype="<f4")

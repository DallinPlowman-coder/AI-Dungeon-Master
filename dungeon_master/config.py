import os
from dataclasses import dataclass
from pathlib import Path


PIPELINE_VERSION = "1"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    max_upload_mb: int = 100
    max_characters: int = 20_000_000
    max_chunks: int = 30_000
    chunk_tokens: int = 360
    overlap_characters: int = 180

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("DM_DATA_DIR", "data")).resolve(),
            embedding_model=os.getenv("DM_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            max_upload_mb=max(1, int(os.getenv("DM_MAX_UPLOAD_MB", "100"))),
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

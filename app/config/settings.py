import math
import os
from functools import lru_cache

import msgspec
from dotenv import load_dotenv

DEFAULT_GENERATION_MODEL = "gpt-5.4-mini-2026-03-17"


class Settings(msgspec.Struct, frozen=True):
    database_url: str
    openai_api_key: str

    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    generation_model: str = DEFAULT_GENERATION_MODEL

    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.generation_model.strip():
            raise ValueError("OPENAI_GENERATION_MODEL must not be empty.")

        if self.embedding_model != "text-embedding-3-small":
            raise ValueError("Only text-embedding-3-small embeddings are supported.")

        if self.embedding_dimensions != 1536:
            raise ValueError("Embedding dimensions must be 1536 for the current index.")

        if (
            not math.isfinite(self.openai_timeout_seconds)
            or self.openai_timeout_seconds <= 0
        ):
            raise ValueError(
                "OPENAI_TIMEOUT_SECONDS must be a finite number greater than 0."
            )

        if not 0 <= self.openai_max_retries <= 3:
            raise ValueError("OPENAI_MAX_RETRIES must be between 0 and 3.")


@lru_cache
def get_settings() -> Settings:
    load_dotenv()

    try:
        timeout_seconds = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30"))
    except ValueError:
        raise ValueError("OPENAI_TIMEOUT_SECONDS must be a number.") from None

    try:
        max_retries = int(os.environ.get("OPENAI_MAX_RETRIES", "2"))
    except ValueError:
        raise ValueError("OPENAI_MAX_RETRIES must be an integer.") from None

    return Settings(
        database_url=os.environ["DATABASE_URL"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        generation_model=os.environ.get(
            "OPENAI_GENERATION_MODEL",
            DEFAULT_GENERATION_MODEL,
        ).strip(),
        openai_timeout_seconds=timeout_seconds,
        openai_max_retries=max_retries,
    )

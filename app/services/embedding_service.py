import asyncio
import math

import msgspec
import tiktoken
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse

from app.config.settings import Settings

MAX_INPUT_TOKENS = 8000
MAX_BATCH_TEXTS = 64
MAX_BATCH_TOKENS = 24000
EMBEDDING_DEADLINE_SECONDS = 120.0


class EmbeddingInputError(ValueError):
    pass


class EmbeddingResponseError(RuntimeError):
    pass


class EmbeddingResult(msgspec.Struct, frozen=True):
    vectors: list[list[float]]
    model: str
    dimensions: int
    total_tokens: int


class EmbeddingService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
    ) -> None:
        self._client = client
        self._settings = settings
        self._encoding = tiktoken.encoding_for_model(settings.embedding_model)

    def _prepare_batches(
        self,
        texts: list[str],
    ) -> list[list[str]]:
        if not texts:
            raise EmbeddingInputError("At least one text is required.")

        batches: list[list[str]] = []
        batch: list[str] = []
        batch_tokens = 0

        for index, text in enumerate(texts):
            if not text.strip():
                raise EmbeddingInputError(f"Text at index {index} is empty.")

            token_count = len(self._encoding.encode_ordinary(text))

            if token_count > MAX_INPUT_TOKENS:
                raise EmbeddingInputError(
                    f"Text at index {index} exceeds {MAX_INPUT_TOKENS} tokens."
                )

            if batch and (
                len(batch) >= MAX_BATCH_TEXTS
                or batch_tokens + token_count > MAX_BATCH_TOKENS
            ):
                batches.append(batch)
                batch = []
                batch_tokens = 0

            batch.append(text)
            batch_tokens += token_count

        if batch:
            batches.append(batch)

        return batches

    def _read_vectors(
        self,
        response: CreateEmbeddingResponse,
        expected_count: int,
    ) -> list[list[float]]:
        if response.model != self._settings.embedding_model:
            raise EmbeddingResponseError("Unexpected embedding model.")

        items = sorted(
            response.data,
            key=lambda item: item.index,
        )

        if [item.index for item in items] != list(range(expected_count)):
            raise EmbeddingResponseError("Unexpected embedding response indices.")

        vectors: list[list[float]] = []

        for item in items:
            vector = item.embedding

            if len(vector) != self._settings.embedding_dimensions:
                raise EmbeddingResponseError("Unexpected embedding dimensions.")

            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingResponseError("Embedding contains non-finite values.")

            if not any(vector):
                raise EmbeddingResponseError("Embedding must not be a zero vector.")

            vectors.append(vector)

        return vectors

    async def embed_texts(
        self,
        texts: list[str],
    ) -> EmbeddingResult:
        batches = self._prepare_batches(texts)

        vectors: list[list[float]] = []
        total_tokens = 0

        async with asyncio.timeout(EMBEDDING_DEADLINE_SECONDS):
            for batch in batches:
                response = await self._client.embeddings.create(
                    model=self._settings.embedding_model,
                    dimensions=self._settings.embedding_dimensions,
                    input=batch,
                    encoding_format="float",
                )

                vectors.extend(
                    self._read_vectors(
                        response,
                        len(batch),
                    )
                )
                total_tokens += response.usage.total_tokens

        return EmbeddingResult(
            vectors=vectors,
            model=self._settings.embedding_model,
            dimensions=self._settings.embedding_dimensions,
            total_tokens=total_tokens,
        )

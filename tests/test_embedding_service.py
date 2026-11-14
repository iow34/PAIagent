import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

import app.services.embedding_service as module
from app.config.settings import Settings


class EmbeddingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(database_url="test", openai_api_key="test")
        self.client = AsyncOpenAI(api_key="test")
        self.service = module.EmbeddingService(self.client, self.settings)
        self.calls = []
        self.mock = AsyncMock(side_effect=self.respond)
        self.patcher = patch.object(self.client.embeddings, "create", self.mock)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.client.close()

    async def respond(self, **kwargs):
        self.calls.append(kwargs)
        texts = kwargs["input"]
        tokens = sum(len(self.service._encoding.encode_ordinary(t)) for t in texts)
        return CreateEmbeddingResponse(
            data=[
                Embedding(
                    index=i, embedding=[float(i + 1)] + [0.0] * 1535, object="embedding"
                )
                for i in reversed(range(len(texts)))
            ],
            model=self.settings.embedding_model,
            object="list",
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def test_order_metadata_and_request_options(self):
        result = await self.service.embed_texts(["Привіт", "World"])
        self.assertEqual([v[0] for v in result.vectors], [1.0, 2.0])
        self.assertEqual(result.model, self.settings.embedding_model)
        self.assertEqual(result.dimensions, 1536)
        self.assertEqual(
            result.total_tokens,
            sum(
                len(self.service._encoding.encode_ordinary(t))
                for t in ["Привіт", "World"]
            ),
        )
        self.assertEqual(self.calls[0]["encoding_format"], "float")
        self.assertEqual(self.calls[0]["dimensions"], 1536)
        self.assertFalse(self.client.is_closed())

    async def test_count_batches(self):
        result = await self.service.embed_texts(["Hello"] * 130)
        self.assertEqual([len(c["input"]) for c in self.calls], [64, 64, 2])
        self.assertEqual(len(result.vectors), 130)
        self.assertEqual(result.total_tokens, 130)

    async def test_token_limit_and_batch_boundary(self):
        text = "a " * 7999 + "a"
        self.assertEqual(len(self.service._encoding.encode_ordinary(text)), 8000)
        result = await self.service.embed_texts([text] * 4)
        self.assertEqual([len(c["input"]) for c in self.calls], [3, 1])
        self.assertEqual(result.total_tokens, 32000)

    async def test_prevalidate_all_texts(self):
        for texts in [[], [""], [" \n\t"], ["Hello"] * 64 + ["a " * 8000 + "a"]]:
            with (
                self.subTest(size=len(texts)),
                self.assertRaises(module.EmbeddingInputError),
            ):
                await self.service.embed_texts(texts)
        self.mock.assert_not_awaited()

    async def test_special_token_spelling_is_plain_text(self):
        result = await self.service.embed_texts(["Текст <|endoftext|> документа"])
        self.assertEqual(len(result.vectors), 1)

    async def test_invalid_response(self):
        response = await self.respond(input=["Hello"])
        bad_responses = [
            response.model_copy(update={"model": "other"}),
            response.model_copy(update={"data": []}),
            response.model_copy(update={"data": response.data * 2}),
        ]
        for updates in [
            dict(index=1),
            dict(embedding=[1.0]),
            dict(embedding=[float("nan")] * 1536),
            dict(embedding=[float("inf")] * 1536),
            dict(embedding=[0.0] * 1536),
        ]:
            bad_responses.append(
                response.model_copy(
                    update={"data": [response.data[0].model_copy(update=updates)]}
                )
            )
        for bad in bad_responses:
            with (
                self.subTest(bad_model=bad.model),
                self.assertRaises(module.EmbeddingResponseError),
            ):
                self.service._read_vectors(bad, 1)

    async def test_failure_after_first_batch(self):
        first = await self.respond(input=["Hello"] * 64)
        self.mock.side_effect = [first, RuntimeError("Provider failure")]
        with self.assertRaisesRegex(RuntimeError, "Provider failure"):
            await self.service.embed_texts(["Hello"] * 65)
        self.assertEqual(self.mock.await_count, 2)

    async def test_shared_deadline(self):
        async def slow(**kwargs):
            await asyncio.sleep(0.04)
            return await self.respond(**kwargs)

        self.mock.side_effect = slow
        with patch.object(module, "EMBEDDING_DEADLINE_SECONDS", 0.06):
            with self.assertRaises(TimeoutError):
                await self.service.embed_texts(["Hello"] * 65)
        self.assertEqual(self.mock.await_count, 2)

    async def test_cancellation_propagates(self):
        self.mock.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.service.embed_texts(["Hello"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Opt-in check of the local embedding model. The normal suite does not call it."""

import os

import pytest

from app.core.config import get_settings
from app.knowledge.identity import OllamaEmbeddingClient

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSISTANT_LIVE_KNOWLEDGE") != "1",
    reason="Set ASSISTANT_LIVE_KNOWLEDGE=1 to call the local embedding model.",
)


@pytest.mark.asyncio
async def test_local_embedding_digest_is_accepted():
    client = OllamaEmbeddingClient()
    digest = client.resolve(get_settings().knowledge_embedding_model)
    assert len(digest) == 64
    vectors = await client.embed(digest, ["dimension probe"])
    assert len(vectors) == 1
    assert len(vectors[0]) > 0
    assert all(isinstance(item, float) for item in vectors[0])

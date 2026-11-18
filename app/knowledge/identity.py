"""Resolve an Ollama embedding identity and pin requests to its digest.

GET /api/tags supplies the digest. The embedding request uses that digest
as the model identifier. A response that echoes a different model is refused.
"""

import math
from string import hexdigits

import httpx

from app.core.config import get_settings

_UNAVAILABLE = "The embedding model identity is unavailable."
_CHANGED = "The embedding model identity changed."
_INVALID = "An embedding vector is not valid."


def _refuse(message: str, cause: BaseException | None = None):
    from app.tools.exceptions import ToolExecutionError

    raise ToolExecutionError(message) from cause


class EmbeddingClient:
    def resolve(self, model_name: str) -> str:
        raise NotImplementedError

    async def embed(self, digest: str, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OllamaEmbeddingClient(EmbeddingClient):
    def resolve(self, model_name: str) -> str:
        settings = get_settings()
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _refuse(_UNAVAILABLE, exc)
        return digest_from_tags(payload, model_name)

    async def embed(self, digest: str, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        try:
            async with httpx.AsyncClient(timeout=settings.knowledge_timeout_seconds) as client:
                response = await client.post(
                    f"{settings.ollama_base_url.rstrip('/')}/api/embed",
                    json={"model": digest, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _refuse(_UNAVAILABLE, exc)
        echoed = payload.get("model")
        accepted = {digest, f"sha256:{digest}"}
        if not isinstance(echoed, str) or echoed.lower() not in accepted:
            _refuse(_CHANGED)
        rows = payload.get("embeddings")
        if not isinstance(rows, list) or len(rows) != len(texts):
            _refuse(_INVALID)
        vectors = [validate_vector(row) for row in rows]
        return vectors


_CLIENT: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OllamaEmbeddingClient()
    return _CLIENT


def set_embedding_client(client: EmbeddingClient | None) -> None:
    global _CLIENT
    _CLIENT = client


def digest_from_tags(payload: object, model_name: str) -> str:
    if not isinstance(payload, dict):
        _refuse(_UNAVAILABLE)
    models = payload.get("models")
    if not isinstance(models, list):
        _refuse(_UNAVAILABLE)
    for item in models:
        if not isinstance(item, dict) or item.get("name") != model_name:
            continue
        digest = item.get("digest")
        if isinstance(digest, str) and digest.lower().startswith("sha256:"):
            digest = digest[7:]
        if isinstance(digest, str) and len(digest) == 64 and all(character in hexdigits for character in digest):
            return digest.lower()
    _refuse(_UNAVAILABLE)


def validate_vector(value: object) -> list[float]:
    if not isinstance(value, list) or not value:
        _refuse(_INVALID)
    limit = get_settings().knowledge_max_dimensions
    if len(value) > limit:
        _refuse(_INVALID)
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            _refuse(_INVALID)
        number = float(item)
        if not math.isfinite(number):
            _refuse(_INVALID)
        vector.append(number)
    return vector
